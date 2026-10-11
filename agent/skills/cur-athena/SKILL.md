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
at a time. Do not mix services in one query.

## Rules for every query

- **Filter on `billing_period`** (string, `'yyyy-MM'`, e.g. `'2026-10'`). It is the partition
  column; without it Athena reads every month. The tool rejects SELECT queries that don't use it.
  For windows that cross a month boundary use `billing_period IN ('2026-09', '2026-10')`.
- **Cost = `SUM(line_item_unblended_cost)`** over `line_item_line_item_type = 'Usage'` rows.
  Other line item types (Credit, Tax, discounts) are only for reconciling to the invoice.
- **Always aggregate.** Never select raw line items. Results are capped at 200 rows by default
  (500 max); if a result says `truncated`, aggregate further or rank with `ORDER BY ... LIMIT`.
- **Settled data:** the last 24–48 hours fill in late. Treat D-2 as the newest settled day and
  say so when you use anything newer.
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
3. **Resources ranked by change** against the baseline, `LIMIT 20`: which resource moved?
   Baseline = same weekday last week (D-7) and the median of the same weekday over the last
   4 weeks. Flag changes over 30% and over $5/day.
4. **One resource by usage type and operation per day:** which cost line moved, and did volume
   (`line_item_usage_amount`) or cost per unit (cost ÷ usage amount) change?
5. **The same resource per hour** around the change: when did it move? Use the hour to look
   for deploys or commits.

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

## Example: resources ranked by change against the baseline

```sql
WITH daily AS (
  SELECT line_item_resource_id AS resource_id,
         date(line_item_usage_start_date) AS day,
         SUM(line_item_unblended_cost) AS cost
  FROM cost_report
  WHERE billing_period IN ('2026-09', '2026-10')
    AND line_item_product_code = 'AWSQueueService'
    AND line_item_line_item_type = 'Usage'
    AND date(line_item_usage_start_date) IN (DATE '2026-10-09', DATE '2026-10-02', DATE '2026-09-25',
                                             DATE '2026-09-18', DATE '2026-09-11')
  GROUP BY 1, 2
)
SELECT resource_id,
       ROUND(SUM(cost) FILTER (WHERE day = DATE '2026-10-09'), 2) AS cost_day,
       ROUND(SUM(cost) FILTER (WHERE day = DATE '2026-10-02'), 2) AS cost_d7,
       ROUND(approx_percentile(cost, 0.5) FILTER (WHERE day < DATE '2026-10-09'), 2) AS median_4wk
FROM daily
GROUP BY 1
ORDER BY ABS(COALESCE(SUM(cost) FILTER (WHERE day = DATE '2026-10-09'), 0)
           - COALESCE(approx_percentile(cost, 0.5) FILTER (WHERE day < DATE '2026-10-09'), 0)) DESC
LIMIT 20
```

A resource with no rows on a day had zero cost that day, so a median over fewer than four
values overstates the baseline; mention it when it matters. If earlier months have no data
(step 1), use the weeks that exist and say the baseline is shorter.

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
