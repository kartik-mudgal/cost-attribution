---
name: cur-athena
description: >
  How to query AWS Cost and Usage Report (CUR 2.0) data in Athena with the athena_query tool:
  table, key columns, cost rules and the query pattern for an investigation. Load before any
  cost query, then load the matching service skill.
---

# Querying CUR in Athena

Cost data is in the Athena table named in your system prompt (database `hackathon_cur`, table
`cost_report`). The database is set as the query context, so write `FROM cost_report`. The SQL
dialect is Athena engine v3 (Trino).

## Scope: one service per analysis

Work on one service at a time: the one named in the request. Every query must filter
`line_item_product_code` to that service:

| Service | `line_item_product_code` | Skill |
| --- | --- | --- |
| SQS | `AWSQueueService` | cur-sqs |
| DynamoDB | `AmazonDynamoDB` | cur-dynamodb |
| EC2 (also EBS, NAT, public IPv4) | `AmazonEC2` | cur-ec2 |
| S3 | `AmazonS3` | cur-s3 |
| SNS | `AmazonSNS` | cur-sns |
| Lambda | `AWSLambda` | cur-lambda |
| ElastiCache | `AmazonElastiCache` | cur-elasticache |
| Bedrock | `AmazonBedrock`, plus marketplace codes whose `product['product_name']` contains `Bedrock` | cur-bedrock |

These product codes are the ones present in this account's CUR. If a filter returns nothing,
run `SELECT DISTINCT line_item_product_code ... LIMIT 50` before saying the service is absent.
If the request names no service, rank services by cost first, then analyse the top movers one
at a time. Do not mix services in one query, with one exception: the overview's first query,
which ranks every service by cost and change (example below).

## Rules for every query

- **Filter on `billing_period`** (string, `'yyyy-MM'`, e.g. `'2026-10'`). It is the partition
  column; without it Athena reads every month. The tool rejects SELECT queries that don't use it.
  For windows that cross a month boundary use `billing_period IN ('2026-09', '2026-10')`.
- **Cost = `SUM(line_item_unblended_cost)`** over `line_item_line_item_type = 'Usage'` rows.
  Other line item types (Credit, Tax, discounts) are only for reconciling to the invoice.
- **Always aggregate.** Never select raw line items. Results are capped at 200 rows by default
  (500 max); if a result says `truncated`, aggregate further or rank with `ORDER BY ... LIMIT`.
- **Settled data:** the last 24–48 hours fill in late. Treat D-2 as the newest settled day and
  say so when you use anything newer. The data is a static snapshot: 2026-10-01 to 2026-10-10
  (10 Oct partial), so the newest settled day is 2026-10-08 and there is no September.
- **Numbers come from query results only.** Quote the figures as returned, and keep the
  `query_execution_id` of each query you cite as evidence.
- If a filter on a value from these notes returns nothing, check the real values first with
  `SELECT DISTINCT ... LIMIT 50`. Usage types carry a region prefix (e.g. `USE1-`, `USW2-`),
  so match them with `LIKE '%WriteRequestUnits%'` rather than `=`.
- CloudWatch daily values are not in this table. Load `cloudwatch-metrics` and query
  `cloudwatch_metrics` with `billing_period = '2026-10'` when a finding needs a metric.

## Columns that matter

| Column | Type | Use |
| --- | --- | --- |
| `billing_period` | string | Partition, `'2026-10'` |
| `line_item_product_code` | string | Service filter |
| `line_item_resource_id` | string | The table or queue, usually an ARN. Empty for some rows (see unattributed) |
| `line_item_usage_type` | string | What was billed: `WriteRequestUnits`, `Requests-RBP`, `TimedStorage-ByteHrs` |
| `line_item_operation` | string | Which API call or mode: `Receive`, `GetQueueUrl`, `PayPerRequestThroughput` |
| `line_item_usage_start_date` | timestamp | Start of the row's hour. Day: `date(line_item_usage_start_date)` |
| `line_item_usage_amount` | double | Billed quantity (requests, units, byte-hours) |
| `pricing_unit` | string | Unit of the usage amount |
| `line_item_unblended_cost` | double | Cost for analysis |
| `line_item_net_unblended_cost` | double | Cost after discounts (reconciliation only) |
| `line_item_line_item_type` | string | `Usage` for analysis |
| `resource_tags` | map(string,string) | Ownership, e.g. `resource_tags['user_part']` |
| `product_region_code` | string | Region, e.g. `us-east-1` |
| `line_item_usage_account_id` | string | Account |

Run `DESCRIBE cost_report` only if you need a column that isn't listed here.

**Finding a resource:** match the end of the ARN, since the request may give a name or URL:
`line_item_resource_id LIKE '%:my-queue'` (SQS) or `LIKE '%table/my-table'` (DynamoDB).

**Unattributed cost:** rows with `line_item_resource_id` NULL or `''` (e.g. DynamoDB Streams).
Report them as one "unattributed" total so figures still add up to the service total.

## Investigation pattern

Narrow down step by step; each step is one aggregated query.

1. **What data exists:**
   `SELECT billing_period, min(line_item_usage_start_date), max(line_item_usage_start_date) FROM cost_report GROUP BY 1`
2. **Service total per day** for the window (about 30 rows): did anything move?
3. **Resources ranked by increase** against the baseline, `LIMIT 20`: which resource jumped?
   Baseline = the average of up to 7 previous days (there is no earlier month for a
   same-weekday median). Flag increases over 30% and over $5/day, settled days only. Rank by
   dollar increase; decreases are not anomalies. If nothing passes, there is no anomaly.
4. **One resource by usage type and operation per day:** which cost line moved, and did volume
   (`line_item_usage_amount`) or cost per unit (cost ÷ usage amount) change?
5. **The same resource per hour** around the change: when did it move? Use the hour with the
   marina-deployments skill (image.tag on devrev/marina) and application-repo commits at the
   new tag.

Save intermediate results and notes to the workspace (e.g. `notes/<service>-<date>.md`).

## Example: service total per day

```sql
SELECT date(line_item_usage_start_date) AS day,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AWSQueueService'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1
ORDER BY 1
```

## Example: overview, every service ranked by cost and change

The one query that may cover several services. Compares the latest settled day with the
average of the 7 days before it.

```sql
WITH daily AS (
  SELECT line_item_product_code AS product,
         product['product_name'] AS product_name,
         date(line_item_usage_start_date) AS day,
         SUM(line_item_unblended_cost) AS cost
  FROM cost_report
  WHERE billing_period = '2026-10'
    AND line_item_line_item_type = 'Usage'
    AND date(line_item_usage_start_date) <= DATE '2026-10-08'
  GROUP BY 1, 2, 3
)
SELECT product,
       MAX(product_name) AS product_name,
       ROUND(SUM(cost), 2) AS cost_oct1_8,
       ROUND(SUM(cost) FILTER (WHERE day = DATE '2026-10-08'), 2) AS cost_oct8,
       ROUND(AVG(cost) FILTER (WHERE day < DATE '2026-10-08'), 2) AS avg_prior_7d
FROM daily
GROUP BY 1
ORDER BY cost_oct1_8 DESC
LIMIT 20
```

Marketplace products show an opaque code in `product`; `product_name` says what they are (a name
containing `Bedrock` belongs to cur-bedrock). Products that aren't covered by a skill still
appear here; report them but don't analyse them.

## Example: sudden increases per resource (anomaly scan)

Each settled day compared with the average of up to 7 previous days for that resource.
Increases only; needs at least 3 previous days so the first days of the month aren't flagged.

```sql
WITH daily AS (
  SELECT line_item_resource_id AS resource_id,
         date(line_item_usage_start_date) AS day,
         SUM(line_item_unblended_cost) AS cost
  FROM cost_report
  WHERE billing_period = '2026-10'
    AND line_item_product_code = 'AWSQueueService'
    AND line_item_line_item_type = 'Usage'
    AND date(line_item_usage_start_date) <= DATE '2026-10-08'
  GROUP BY 1, 2
),
scored AS (
  SELECT resource_id, day, cost,
         AVG(cost) OVER (PARTITION BY resource_id ORDER BY day ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS baseline,
         COUNT(*) OVER (PARTITION BY resource_id ORDER BY day ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS prior_days
  FROM daily
)
SELECT resource_id, day,
       ROUND(cost, 2) AS cost,
       ROUND(baseline, 2) AS baseline,
       ROUND(cost - baseline, 2) AS increase,
       ROUND(100 * (cost - baseline) / baseline, 0) AS increase_pct
FROM scored
WHERE prior_days >= 3
  AND cost - baseline > 5
  AND cost > 1.3 * baseline
ORDER BY increase DESC
LIMIT 20
```

A resource with no rows on a day had zero cost that day, but has no row in `daily`, so its
baseline averages only the days it had cost. Mention it when a resource was idle before the
jump: the increase from zero is real even though no baseline is shown.

## Example: one resource by cost line per day

```sql
SELECT date(line_item_usage_start_date) AS day,
       line_item_usage_type,
       line_item_operation,
       ROUND(SUM(line_item_usage_amount), 0) AS usage,
       MAX(pricing_unit) AS unit,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AWSQueueService'
  AND line_item_line_item_type = 'Usage'
  AND line_item_resource_id LIKE '%:my-queue'
GROUP BY 1, 2, 3
ORDER BY 1, cost DESC
```
