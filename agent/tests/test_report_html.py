"""HTML report rendering: title, verdict, escaping, tables and links."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_html import render_report, report_title, report_verdict

REPORT = """# Deep dive: SQS queue `hubble-parquet-ingest-queue`

**Verdict: Action recommended**

## Scope checked
- Service: SQS

## Finding 1: GetQueueUrl on every send
| Operation | Requests | Cost |
|---|---|---|
| GetQueueUrl | 127,034,206 | $50.81 |

See [handler.go](https://github.com/devrev/argus/blob/main/internal/router_lambda/handler.go).

```sql
SELECT 1 FROM cost_report WHERE billing_period = '2026-10'
```

Not an issue: No action needed for blubox.
"""


class TestReportHtml(unittest.TestCase):
    def setUp(self):
        self.html = render_report(REPORT, "sqs-deepdive.md", datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc))

    def test_title_from_first_heading(self):
        self.assertEqual(report_title(REPORT, "x"), "Deep dive: SQS queue `hubble-parquet-ingest-queue`")
        self.assertIn("<title>Deep dive: SQS queue hubble-parquet-ingest-queue</title>", self.html)
        self.assertIn('<h1 class="title">Deep dive: SQS queue <code>hubble-parquet-ingest-queue</code></h1>', self.html)
        self.assertEqual(self.html.count("<h1"), 1)

    def test_title_falls_back_to_file_name(self):
        self.assertIn("<title>Sqs anomaly</title>", render_report("No heading here", "sqs-anomaly.md"))

    def test_first_stated_verdict_wins(self):
        self.assertEqual(report_verdict(REPORT), ("Action recommended", "action"))
        self.assertIn('<p class="verdict action" role="status">Action recommended</p>', self.html)
        self.assertIsNone(report_verdict("nothing to see"))

    def test_verdict_line_is_not_repeated_in_body(self):
        self.assertNotIn("Verdict: Action recommended", self.html)
        self.assertEqual(self.html.count("Action recommended"), 1)
        self.assertIn("No action needed for blubox", self.html)
        html = render_report("# R\n\n## No anomaly\n\nChecked.\n", "r.md")
        self.assertNotIn("<h2>No anomaly</h2>", html)
        self.assertIn('class="verdict ok"', html)

    def test_markdown_structure_renders(self):
        self.assertIn('<div class="table-wrap"><table>', self.html)
        self.assertIn("<td>$50.81</td>", self.html)
        self.assertIn('<code class="language-sql">', self.html)
        self.assertIn('target="_blank"', self.html)
        self.assertIn('rel="noopener noreferrer"', self.html)
        self.assertIn("generated 11 Oct 2026, 16:00 UTC", self.html)

    def test_raw_html_and_scripts_are_escaped(self):
        html = render_report('# R\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))', "r.md")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("default-src 'none'", html)


if __name__ == "__main__":
    unittest.main()
