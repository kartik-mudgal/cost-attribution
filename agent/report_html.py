"""Render a markdown cost report as one self-contained HTML page.

The page has no scripts and no external requests: inline CSS, system fonts. Raw HTML in the
markdown is escaped (the text comes from the model), so only markdown formatting is rendered.
"""
import re
from datetime import datetime, timezone
from html import escape

from markdown_it import MarkdownIt

from report_charts import CHART_CSS, chart_for_table, extract_tables

VERDICTS = {
    "Action recommended": "action",
    "Anomaly confirmed": "alert",
    "No action needed": "ok",
    "No anomaly": "ok",
}

_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def _link_open(self, tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


_MD.add_render_rule("link_open", _link_open)

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)
# A line that only restates the verdict ("**Verdict: No anomaly**", "## Action recommended");
# the header already shows it.
_VERDICT_LINE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*|__)?[ \t]*(?:verdict[ \t]*:[ \t]*)?(?:\*\*|__)?[ \t]*"
    r"(?:" + "|".join(re.escape(v) for v in VERDICTS) + r")[ \t]*\.?[ \t]*(?:\*\*|__)?[ \t]*$\n?",
    re.M | re.I,
)

CSS = """
:root {
  --ink: #1d2733; --paper: #ffffff; --rule: #d9dfe7; --muted: #5a6573; --wash: #f4f6f9;
  --ok: #17785a; --action: #9a5b00; --alert: #b42318; --link: #1d5fb4;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --ink: #e4e8ee; --paper: #14191f; --rule: #2c3540; --muted: #9aa5b3; --wash: #1b222a;
    --ok: #4cc79a; --action: #e0a443; --alert: #f2735f; --link: #7fb0f0;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.6 var(--sans); font-variant-numeric: tabular-nums;
}
.page { max-width: 1040px; margin: 0 auto; padding: 48px 32px 72px; }
header { margin-bottom: 40px; }
.title { font-size: 2rem; line-height: 1.2; font-weight: 700; margin: 0 0 8px; max-width: 40ch; }
.title code { font-size: 0.82em; background: none; padding: 0; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 0.9rem; margin: 0; }
.verdict {
  margin-top: 24px; padding: 14px 18px; border-left: 6px solid var(--muted);
  background: var(--wash); font-size: 1.15rem; font-weight: 650;
}
.verdict.ok { border-color: var(--ok); color: var(--ok); }
.verdict.action { border-color: var(--action); color: var(--action); }
.verdict.alert { border-color: var(--alert); color: var(--alert); }
main > * { max-width: 72ch; }
main > .table-wrap, main > pre, main > .chart { max-width: none; }
h1, h2, h3, h4 { line-height: 1.3; margin: 1.6em 0 0.5em; }
h1 { font-size: 1.6rem; }
h2 { font-size: 1.35rem; margin-top: 1.4em; padding-top: 1em; border-top: 1px solid var(--rule); }
main > h2:first-child { border-top: 0; padding-top: 0; margin-top: 0; }
h3 { font-size: 1.12rem; }
h4 { font-size: 1rem; color: var(--muted); }
p, ul, ol { margin: 0 0 1em; }
li { margin: 0.25em 0; }
li > ul, li > ol { margin: 0.25em 0 0.5em; }
strong { font-weight: 650; }
a { color: var(--link); text-underline-offset: 2px; }
a:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; border-radius: 2px; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 2.5em 0; }
code { font: 0.88em var(--mono); background: var(--wash); padding: 0.1em 0.35em; border-radius: 3px; }
pre {
  font: 0.86rem/1.5 var(--mono); background: var(--wash); border: 1px solid var(--rule);
  padding: 14px 16px; overflow-x: auto; margin: 0 0 1.2em;
}
pre code { background: none; padding: 0; font-size: inherit; }
blockquote { margin: 0 0 1em; padding: 2px 0 2px 16px; border-left: 3px solid var(--rule); color: var(--muted); }
.table-wrap { overflow-x: auto; margin: 0 0 1.4em; }
table { border-collapse: collapse; width: 100%; font-size: 0.93rem; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { font-weight: 650; border-bottom: 2px solid var(--ink); white-space: nowrap; }
tbody tr:hover { background: var(--wash); }
footer { margin-top: 56px; padding-top: 16px; border-top: 1px solid var(--rule); color: var(--muted); font-size: 0.85rem; }
@media (max-width: 640px) {
  .page { padding: 28px 18px 48px; }
  .title { font-size: 1.6rem; }
}
@media print {
  body { font-size: 11pt; }
  .page { padding: 0; max-width: none; }
  a { color: inherit; }
  tbody tr:hover { background: none; }
  h2, h3 { break-after: avoid; }
  pre, tr { break-inside: avoid; }
}
"""


def report_title(markdown_text: str, fallback: str) -> str:
    match = _H1.search(markdown_text)
    return match.group(1).strip() if match else fallback


def report_verdict(markdown_text: str) -> tuple[str, str] | None:
    """Return (verdict text, css class) for the first verdict the report states."""
    found = [(markdown_text.find(text), text) for text in VERDICTS if text in markdown_text]
    if not found:
        return None
    _, text = min(found)
    return text, VERDICTS[text]


def render_report(markdown_text: str, name: str, generated_at: datetime | None = None) -> str:
    """Return the full HTML page for a markdown report."""
    fallback = re.sub(r"[-_]+", " ", name.rsplit(".", 1)[0]).strip().capitalize() or "Cost report"
    title = report_title(markdown_text, fallback)
    body_md = _H1.sub("", markdown_text, count=1) if _H1.search(markdown_text) else markdown_text
    verdict = report_verdict(markdown_text)
    if verdict:
        body_md = _VERDICT_LINE.sub("", body_md, count=1)
    tokens = _MD.parse(body_md)
    charts = [chart_for_table(rows) for rows in extract_tables(tokens)]
    body = _MD.renderer.render(tokens, _MD.options, {})
    pieces = body.split("<table>")
    body = pieces[0] + "".join(
        f'{charts[i] if i < len(charts) else ""}<div class="table-wrap"><table>{piece}'
        for i, piece in enumerate(pieces[1:])
    )
    body = body.replace("</table>", "</table></div>")
    verdict_html = (
        f'<p class="verdict {verdict[1]}" role="status">{escape(verdict[0])}</p>' if verdict else ""
    )
    when = (generated_at or datetime.now(timezone.utc)).strftime("%d %b %Y, %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data: https:">
<title>{escape(title.replace("`", ""))}</title>
<style>{CSS}{CHART_CSS}</style>
</head>
<body>
<div class="page">
<header>
<h1 class="title">{_MD.renderInline(title)}</h1>
<p class="meta">Cost investigator report, generated {escape(when)}</p>
{verdict_html}
</header>
<main>
{body}
</main>
<footer>Generated by the cost investigator agent from AWS CUR data, CloudWatch metrics and code.
Figures come from the queries cited in the report.</footer>
</div>
</body>
</html>
"""
