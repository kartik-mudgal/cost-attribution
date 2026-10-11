"""Charts drawn from report tables: shape detection, values, escaping and placement."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_charts import chart_for_table, fmt, parse_number
from report_html import render_report

DAILY = [["day", "cost"], ["10-01", "97.47"], ["10-02", "112.96"], ["10-03", "172.74"],
         ["10-04", "259.15"], ["10-05", "224.15"]]
OPS = [["Operation", "Requests", "Cost"], ["GetQueueUrl", "127,034,206", "$50.81"],
       ["Send", "126,977,216", "$50.79"], ["Receive", "96,993,659", "$38.80"],
       ["GetQueueAttributes", "330,840", "$0.13"], ["Total", "351,336,921", "$140.53"]]
COMPARE = [["Service", "7-day baseline", "Oct 8 cost"], ["GPT-6.1 Sol", "$1,144.03", "$4,345.66"],
           ["EC2", "$3,203.87", "$3,188.83"], ["GPT-6 Astra", "$4,276.24", "$1,940.61"]]


class TestParsing(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(parse_number("$1,234.50"), (1234.5, True))
        self.assertEqual(parse_number("~$0.00"), (0.0, True))
        self.assertEqual(parse_number("**259.15**"), (259.15, False))
        self.assertEqual(parse_number("12.3M"), (12_300_000.0, False))
        self.assertIsNone(parse_number("GetQueueUrl"))

    def test_format(self):
        self.assertEqual(fmt(4345.66, True), "$4,345.66")
        self.assertEqual(fmt(1_500_000, True, compact=True), "$1.5M")


class TestShapes(unittest.TestCase):
    def test_daily_table_becomes_trend_with_peak(self):
        svg = chart_for_table(DAILY)
        self.assertIn('class="line s0"', svg)
        self.assertIn('class="dot peak"', svg)
        self.assertIn("Peak 10-04: $259.15", svg)
        self.assertIn("<title>10-04: cost $259.15</title>", svg)

    def test_money_table_becomes_sorted_breakdown_without_total_row(self):
        svg = chart_for_table(OPS)
        self.assertIn('class="bar top"', svg)
        self.assertLess(svg.index("GetQueueUrl"), svg.index("Receive"))
        self.assertNotIn("Total", svg)
        self.assertIn("$0.13 · &lt;1%", svg)
        self.assertIn("$50.81 · 36%", svg)

    def test_baseline_vs_current_becomes_comparison(self):
        svg = chart_for_table(COMPARE)
        self.assertIn('class="bar before"', svg)
        self.assertIn("+$3,201.63 (+280%)", svg)
        self.assertIn("−$15.04 (0%)", svg)
        self.assertIn("−$2,335.63 (−55%)", svg)
        self.assertLess(svg.index("GPT-6.1 Sol"), svg.index("GPT-6 Astra"))

    def test_unchartable_tables(self):
        self.assertEqual(chart_for_table([["Check", "Result"], ["PITR", "on"], ["TTL", "off"]]), "")
        self.assertEqual(chart_for_table([["day", "cost"], ["10-01", "1"]]), "")
        self.assertEqual(chart_for_table([["Item", "Change"], ["a", "-$5"], ["b", "$3"]]), "")

    def test_labels_are_escaped(self):
        svg = chart_for_table([["Queue", "Cost"], ["<script>x</script>", "$5"], ["b", "$3"]])
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)


class TestPlacement(unittest.TestCase):
    def test_chart_sits_above_its_table(self):
        md = "# R\n\n| day | cost |\n|---|---|\n| 10-01 | 1 |\n| 10-02 | 2 |\n| 10-03 | 3 |\n\n" \
             "| Check | Result |\n|---|---|\n| PITR | on |\n"
        html = render_report(md, "r.md")
        self.assertEqual(html.count('<figure class="chart">'), 1)
        self.assertEqual(html.count('<div class="table-wrap"><table>'), 2)
        self.assertLess(html.index('<figure class="chart">'), html.index('<div class="table-wrap"><table>'))
        self.assertNotIn("<script", html)

    def test_report_without_tables_has_no_charts(self):
        self.assertNotIn('<figure class="chart">', render_report("# R\n\nJust prose.\n", "r.md"))


if __name__ == "__main__":
    unittest.main()
