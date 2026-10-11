"""Draw inline SVG charts from the tables in a markdown cost report.

Each table is classified by shape:
- trend: first column is dates or hours, one or more numeric columns -> line chart, peak marked
- comparison: two money columns named like before/after (baseline vs current) -> paired bars
- breakdown: labels plus a money column -> horizontal bars sorted by cost, with share of total
Tables that fit none of these get no chart. Charts use the page's CSS variables, so they follow
the light and dark palettes; hovering a point or bar shows its value via SVG <title>.
"""
import math
import re
from html import escape

_DATE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}([ T]\d{1,2}:\d{2}(:\d{2})?)?|\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|\d{1,2}:\d{2}"
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2})$",
    re.I,
)
_NUMBER = re.compile(r"^[~≈]?\s*(-?)\s*(\$)?\s*(-?[\d,]*\.?\d+)\s*([kmb])?\s*(%)?$", re.I)
_BEFORE = re.compile(r"baseline|before|previous|prior|d-7|avg|average|median|expected", re.I)
_AFTER = re.compile(r"current|after|actual|now|today|day\b|cost_day|latest|oct\s*\d|\d{2}-\d{2}", re.I)
_TOTAL = re.compile(r"^(total|sum|all)\b", re.I)
_MONEY_HEADER = re.compile(r"cost|spend|\$|usd|saving|amount|price", re.I)

WIDTH = 720
MAX_BARS = 10


def clean_cell(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[*_`~]", "", text).strip()


def parse_number(text: str) -> tuple[float, bool] | None:
    """Return (value, is_money) for a cell like "$1,234.5", "~$0.00", "12.3M", or None."""
    match = _NUMBER.match(clean_cell(text).replace(" ", ""))
    if not match:
        return None
    sign, dollar, digits, suffix, _ = match.groups()
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    value *= {"k": 1e3, "m": 1e6, "b": 1e9}.get((suffix or "").lower(), 1)
    return (-value if sign else value), bool(dollar)


def extract_tables(tokens) -> list[list[list[str]]]:
    """Return each table as rows of cell text (first row is the header), in document order."""
    tables, rows, row, in_table = [], [], None, False
    for tok in tokens:
        if tok.type == "table_open":
            in_table, rows = True, []
        elif tok.type == "table_close":
            tables.append(rows)
            in_table = False
        elif in_table and tok.type == "tr_open":
            row = []
        elif in_table and tok.type == "tr_close":
            rows.append(row)
        elif in_table and tok.type == "inline" and row is not None:
            row.append(clean_cell(tok.content))
    return tables


def _numeric_columns(header: list[str], body: list[list[str]]) -> dict[int, bool]:
    """Columns (after the first) where at least 80% of body cells are numbers -> is_money."""
    out = {}
    for col in range(1, len(header)):
        parsed = [parse_number(r[col]) for r in body if col < len(r)]
        hits = [p for p in parsed if p]
        if parsed and len(hits) >= 0.8 * len(parsed):
            out[col] = any(money for _, money in hits)
    return out


def _nice_step(top: float, ticks: int = 4) -> float:
    if top <= 0:
        return 1.0
    raw = top / ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def fmt(value: float, money: bool, compact: bool = False) -> str:
    prefix = "$" if money else ""
    if compact:
        for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if abs(value) >= size:
                return f"{prefix}{value / size:.3g}{unit}"
        return f"{prefix}{value:.3g}" if abs(value) < 1000 else f"{prefix}{value:,.0f}"
    if money:
        return f"${value:,.2f}"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _figure(svg: str, caption: str, label: str) -> str:
    return (
        f'<figure class="chart"><svg viewBox="0 0 {WIDTH} {svg[0]}" role="img" aria-label="{escape(label)}">'
        f"{svg[1]}</svg><figcaption>{escape(caption)}</figcaption></figure>"
    )


def trend_chart(labels: list[str], series: list[tuple[str, list[float]]], money: bool) -> str:
    height, left, right, top, bottom = 260, 64, 16, 16, 36
    plot_w, plot_h = WIDTH - left - right, height - top - bottom
    peak = max(max(values) for _, values in series)
    step = _nice_step(peak)
    y_max = step * math.ceil(peak / step) if peak > 0 else 1
    n = len(labels)
    x = lambda i: left + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)
    y = lambda v: top + plot_h * (1 - v / y_max)
    parts = []
    tick = 0.0
    while tick <= y_max + 1e-9:
        parts.append(f'<line class="grid" x1="{left}" x2="{WIDTH - right}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>')
        parts.append(f'<text class="axis" x="{left - 8}" y="{y(tick) + 4:.1f}" text-anchor="end">{fmt(tick, money, True)}</text>')
        tick += step
    every = max(1, math.ceil(n / 10))
    for i, label in enumerate(labels):
        if i % every == 0 or i == n - 1:
            parts.append(f'<text class="axis" x="{x(i):.1f}" y="{height - 12}" text-anchor="middle">{escape(label)}</text>')
    for s, (name, values) in enumerate(series):
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
        if s == 0 and len(series) == 1:
            area = f"{left},{y(0):.1f} {points} {x(n - 1):.1f},{y(0):.1f}"
            parts.append(f'<polygon class="area" points="{area}"/>')
        parts.append(f'<polyline class="line s{s}" points="{points}"/>')
        hi = max(range(n), key=lambda i: values[i])
        for i, v in enumerate(values):
            cls = "dot peak" if i == hi and len(series) == 1 else f"dot s{s}"
            parts.append(
                f'<circle class="{cls}" cx="{x(i):.1f}" cy="{y(v):.1f}" r="{5 if "peak" in cls else 3.5}">'
                f"<title>{escape(labels[i])}: {escape(name)} {fmt(v, money)}</title></circle>"
            )
        if len(series) == 1:
            anchor = "end" if hi > n * 0.7 else "start"
            dx = -10 if anchor == "end" else 10
            parts.append(
                f'<text class="peak-label" x="{x(hi) + dx:.1f}" y="{y(values[hi]) - 8:.1f}" '
                f'text-anchor="{anchor}">Peak {escape(labels[hi])}: {fmt(values[hi], money)}</text>'
            )
    if len(series) > 1:
        legend = "".join(
            f'<tspan class="legend s{s}" dx="{0 if s == 0 else 16}">■</tspan><tspan dx="4">{escape(name)}</tspan>'
            for s, (name, _) in enumerate(series)
        )
        parts.append(f'<text class="axis" x="{left}" y="{top - 2}">{legend}</text>')
    names = ", ".join(name for name, _ in series)
    return _figure((height, "".join(parts)), f"Trend of {names}, {labels[0]} to {labels[-1]}, from the table below",
                   f"Trend of {names} over {n} periods")


def breakdown_chart(items: list[tuple[str, float]], name: str, money: bool) -> str:
    items = sorted(items, key=lambda kv: kv[1], reverse=True)
    if len(items) > MAX_BARS:
        rest = sum(v for _, v in items[MAX_BARS - 1:])
        items = items[: MAX_BARS - 1] + [("Other", rest)]
    total = sum(v for _, v in items) or 1
    row_h, label_w, value_w = 30, 230, 150
    height = row_h * len(items) + 8
    bar_max = WIDTH - label_w - value_w
    top_value = items[0][1] or 1
    parts = []
    for i, (label, value) in enumerate(items):
        y = 4 + i * row_h
        w = max(1.0, bar_max * value / top_value) if value > 0 else 0
        short = label if len(label) <= 32 else label[:31] + "…"
        share = 100 * value / total
        parts.append(f'<text class="axis label" x="{label_w - 10}" y="{y + 19}" text-anchor="end">{escape(short)}</text>')
        parts.append(
            f'<rect class="bar{" top" if i == 0 else ""}" x="{label_w}" y="{y + 5}" width="{w:.1f}" height="{row_h - 10}" rx="2">'
            f"<title>{escape(label)}: {fmt(value, money)} ({share:.1f}%)</title></rect>"
        )
        share_text = "<1%" if 0 < share < 1 else f"{share:.0f}%"
        parts.append(
            f'<text class="axis value" x="{label_w + w + 8:.1f}" y="{y + 19}">{fmt(value, money)} · {escape(share_text)}</text>'
        )
    return _figure((height, "".join(parts)), f"{name} by share of total, from the table below",
                   f"Breakdown of {name} across {len(items)} items")


def comparison_chart(items: list[tuple[str, float, float]], before: str, after: str, money: bool) -> str:
    items = sorted(items, key=lambda r: r[2] - r[1], reverse=True)[:MAX_BARS]
    row_h, label_w, value_w = 44, 230, 190
    height = row_h * len(items) + 30
    bar_max = WIDTH - label_w - value_w
    top_value = max(max(b, a) for _, b, a in items) or 1
    parts = [
        f'<text class="axis" x="{label_w}" y="14"><tspan class="legend before">■</tspan><tspan dx="4">{escape(before)}</tspan>'
        f'<tspan class="legend after" dx="16">■</tspan><tspan dx="4">{escape(after)}</tspan></text>'
    ]
    for i, (label, b, a) in enumerate(items):
        y = 26 + i * row_h
        short = label if len(label) <= 32 else label[:31] + "…"
        change = a - b
        pct = ""
        if b:
            p = round(100 * change / b)
            pct = f" ({'+' if p > 0 else '−' if p < 0 else ''}{abs(p)}%)"
        parts.append(f'<text class="axis label" x="{label_w - 10}" y="{y + 22}" text-anchor="end">{escape(short)}</text>')
        for j, (cls, v) in enumerate((("before", b), ("after", a))):
            w = max(1.0, bar_max * v / top_value) if v > 0 else 0
            parts.append(
                f'<rect class="bar {cls}" x="{label_w}" y="{y + 4 + j * 17}" width="{w:.1f}" height="14" rx="2">'
                f"<title>{escape(label)}: {escape(before if j == 0 else after)} {fmt(v, money)}</title></rect>"
            )
        sign_cls = "up" if change > 0 else "down"
        parts.append(
            f'<text class="axis value {sign_cls}" x="{label_w + bar_max * max(a, b) / top_value + 8:.1f}" y="{y + 25}">'
            f"{'+' if change >= 0 else '−'}{fmt(abs(change), money)}{pct}</text>"
        )
    return _figure((height, "".join(parts)), f"{before} vs {after}, from the table below",
                   f"Change from {before} to {after} for {len(items)} items")


def chart_for_table(rows: list[list[str]]) -> str:
    """Return a <figure> with an SVG chart for one table, or "" if its shape isn't chartable."""
    if len(rows) < 3 or len(rows[0]) < 2:
        return ""
    header, body = rows[0], [r for r in rows[1:] if r and not _TOTAL.match(r[0])]
    body = [r for r in body if len(r) == len(header)]
    if len(body) < 2:
        return ""
    numeric = _numeric_columns(header, body)
    if not numeric:
        return ""
    money_cols = [c for c, m in numeric.items() if m or _MONEY_HEADER.search(header[c])]

    def column(c: int) -> list[float]:
        return [(parse_number(r[c]) or (0.0, False))[0] for r in body]

    labels = [r[0] for r in body]
    if all(_DATE.match(label) for label in labels) and len(body) >= 3:
        cols = (money_cols or list(numeric))[:3]
        money = bool(money_cols)
        return trend_chart(labels, [(header[c], column(c)) for c in cols], money)

    if len(money_cols) >= 2:
        before = next((c for c in money_cols if _BEFORE.search(header[c])), None)
        after = next((c for c in money_cols if c != before and _AFTER.search(header[c])), None)
        if before is not None and after is not None:
            items = list(zip(labels, column(before), column(after)))
            return comparison_chart(items, header[before], header[after], True)

    col = money_cols[0] if money_cols else None
    if col is None:
        return ""
    values = column(col)
    if any(v < 0 for v in values) or sum(values) <= 0:
        return ""
    return breakdown_chart(list(zip(labels, values)), header[col], True)


CHART_CSS = """
.chart { margin: 0 0 12px; max-width: none; }
.chart svg { width: 100%; height: auto; display: block; overflow: visible; }
.chart figcaption { color: var(--muted); font-size: 0.82rem; margin-top: 4px; }
.chart .grid { stroke: var(--rule); stroke-width: 1; }
.chart .axis { fill: var(--muted); font: 12px var(--sans); font-variant-numeric: tabular-nums; }
.chart .axis.label { fill: var(--ink); }
.chart .axis.value { fill: var(--ink); }
.chart .axis.value.up { fill: var(--alert); }
.chart .axis.value.down { fill: var(--ok); }
.chart .line { fill: none; stroke: var(--link); stroke-width: 2.5; stroke-linejoin: round; }
.chart .line.s1 { stroke: var(--action); }
.chart .line.s2 { stroke: var(--ok); }
.chart .area { fill: var(--link); opacity: 0.08; }
.chart .dot { fill: var(--paper); stroke: var(--link); stroke-width: 2; }
.chart .dot.s1 { stroke: var(--action); }
.chart .dot.s2 { stroke: var(--ok); }
.chart .dot.peak { fill: var(--alert); stroke: var(--alert); }
.chart .peak-label { fill: var(--alert); font: 600 12px var(--sans); }
.chart .bar { fill: var(--link); opacity: 0.55; }
.chart .bar.top { opacity: 1; }
.chart .bar.before { fill: var(--muted); opacity: 0.5; }
.chart .bar.after { fill: var(--link); opacity: 1; }
.chart .legend.s0, .chart .legend.after { fill: var(--link); }
.chart .legend.s1 { fill: var(--action); }
.chart .legend.s2 { fill: var(--ok); }
.chart .legend.before { fill: var(--muted); }
.chart rect:hover, .chart circle:hover { opacity: 1; stroke: var(--ink); stroke-width: 1; }
"""
