---
name: cur-dynamodb
description: >
  DynamoDB cost analysis from CUR: how DynamoDB is billed (on-demand, provisioned, storage,
  PITR, Streams), how to tell traffic growth from bigger items, and example Athena queries.
  Load after cur-athena when the service in scope is DynamoDB.
---

# DynamoDB in CUR

## Top 5 cost drivers

1. Write request units. One WRU is up to 1 KB. Crossing a 1 KB boundary multiplies write cost. Transactional writes cost double.
2. Read request units. One RRU is 4 KB strongly consistent, half that for eventual consistency. Scans multiply the count.
3. Capacity mode fit. Provisioned bills whether used or not. On-demand is cheaper below roughly 29% steady utilization of the equivalent provisioned capacity.
4. Storage and backups. Standard storage, PITR, and on-demand backups. Index storage counts.
5. Indexes, replication and streams. Each GSI repeats writes on it. Global tables add replicated writes. Streams bill separately and often have no resource ID.

## Top 5 metrics

Namespace `AWS/DynamoDB`, dimensions `TableName` and `GlobalSecondaryIndexName`.

1. `ConsumedWriteCapacityUnits` (Sum). `Source` separates `Customer` from `GlobalTable`.
2. `ConsumedReadCapacityUnits` (Sum)
3. `ProvisionedWriteCapacityUnits` and `ProvisionedReadCapacityUnits`. Utilization is consumed divided by provisioned.
4. `ReadThrottleEvents` and `WriteThrottleEvents`
5. `ReturnedItemCount` against `ConsumedReadCapacityUnits` (scans and filters)

Average item size is `TableSizeBytes` / `ItemCount` from DescribeTable. Table-level metrics exclude indexes.

Filter every query with `line_item_product_code = 'AmazonDynamoDB'`.
`line_item_resource_id` is the table ARN (`arn:aws:dynamodb:<region>:<account>:table/<name>`).
GSI cost is rolled into the table's ARN; CUR can't separate indexes.

## How DynamoDB is billed

Usage types carry a region prefix; match them with `LIKE`.

| Usage type (operation) | Driven by |
| --- | --- |
| `WriteRequestUnits`, `ReadRequestUnits` (`PayPerRequestThroughput`) | On-demand: requests × item size, rounded up per 1 KB written / 4 KB read |
| `WriteCapacityUnit-Hrs`, `ReadCapacityUnit-Hrs` (`CommittedThroughput`) | Provisioned capacity, paid whether used or not |
| `TimedStorage-ByteHrs` | Table size |
| `TimedPITRStorage-ByteHrs` | Point-in-time recovery, billed on table size |
| `Streams-Requests` (`GetRecords`) | Stream consumers. **No resource ID**: report as unattributed |

`line_item_usage_amount` is in billed units (request units, capacity-unit hours, byte-hours),
not request counts.

## Volume or efficiency

`cost = requests × billed units per request × price`

- Volume up, units per request flat: more traffic (expected, or a retry storm / new caller).
- Volume flat, units per request up: bigger items, a new GSI, scans, or strongly consistent
  reads. This is the waste case.

CUR shows only units, so it can tell you **which cost line moved and when**, but not whether
requests or item size changed. That needs CloudWatch, which isn't available to you yet. Name the
metric that would confirm your explanation and lower your confidence:

- Request count per operation: `SuccessfulRequestLatency` SampleCount by `Operation`.
- Units per request: `ConsumedWriteCapacityUnits` ÷ write-request SampleCount. A jump means items
  crossed a 1 KB step or a GSI was added.
- Average item size: `TableSizeBytes` ÷ `ItemCount` from DescribeTable.
- GSI amplification: consumed units with `GlobalSecondaryIndexName` ÷ base table units.
- Provisioned utilisation: consumed ÷ provisioned. Steady below ~30%: lower it or switch to
  on-demand.

Caveat: BatchWriteItem and BatchGetItem count one sample per call, not per item, so "units per
request" overstates item size on batch-heavy tables.

## What CUR alone can show

| Check | How |
| --- | --- |
| Which cost line moved | Daily cost by `line_item_usage_type` for the table |
| Write vs read vs storage share | Cost by usage type over the window |
| Provisioned capacity paid | `CapacityUnit-Hrs` usage and cost; compare with on-demand cost for the same units |
| PITR share | `TimedPITRStorage` cost ÷ `TimedStorage` cost. PITR on a large dev table is a candidate saving |
| Storage growth | Daily `TimedStorage-ByteHrs` trend; steady growth may mean TTL isn't deleting old items |
| Hour of change | Hourly `WriteRequestUnits` around the jump, to match a deploy |

Known pattern in dev: `wisp-indexer-vector-cache` write units swung from 0.55M to 3.86M a day
while batch calls stayed around 100–130k a day; average item size is about 4.2 KB, so about
5 write units per item.

## Estimating savings

Use the table's own cost per unit from the same query (cost ÷ usage amount) and the units you
expect to remove. Express it per month (daily × 30) and state the assumption, e.g. "if PITR is
turned off" or "if items drop below 1 KB".

## Example: one table by cost line per day

```sql
SELECT date(line_item_usage_start_date) AS day,
       line_item_usage_type,
       line_item_operation,
       ROUND(SUM(line_item_usage_amount), 0) AS units,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonDynamoDB'
  AND line_item_line_item_type = 'Usage'
  AND line_item_resource_id LIKE '%table/my-table'
GROUP BY 1, 2, 3
ORDER BY 1, cost DESC
```

## Example: tables by cost category

```sql
SELECT line_item_resource_id AS table_arn,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%WriteRequestUnits%'), 2) AS write_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%ReadRequestUnits%'), 2) AS read_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%CapacityUnit-Hrs%'), 2) AS provisioned_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%TimedStorage-ByteHrs%'), 2) AS storage_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%TimedPITRStorage%'), 2) AS pitr_cost,
       ROUND(SUM(line_item_unblended_cost), 2) AS total_cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonDynamoDB'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1
ORDER BY total_cost DESC
LIMIT 20
```

## Example: hourly writes around a change

```sql
SELECT date_trunc('hour', line_item_usage_start_date) AS hour,
       ROUND(SUM(line_item_usage_amount), 0) AS write_units,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonDynamoDB'
  AND line_item_line_item_type = 'Usage'
  AND line_item_resource_id LIKE '%table/my-table'
  AND line_item_usage_type LIKE '%WriteRequestUnits%'
  AND line_item_usage_start_date >= TIMESTAMP '2026-10-07 00:00:00'
  AND line_item_usage_start_date <  TIMESTAMP '2026-10-09 00:00:00'
GROUP BY 1
ORDER BY 1
```
