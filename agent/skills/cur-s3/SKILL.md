---
name: cur-s3
description: >
  S3 cost analysis from CUR: storage class, request tier, data transfer, retrieval
  and early-delete fees, plus the CloudWatch bucket metrics that explain them.
  Load after cur-athena when the service in scope is S3.
---

# S3 in CUR

Filter every query with `line_item_product_code = 'AmazonS3'`.
`line_item_resource_id` is the bucket ARN. Match a name with `LIKE '%bucket-name'`.

## Top 5 cost drivers

1. Storage GB-months by class. Standard is the usual waste when data is rarely read.
2. Requests. Tier1 (PUT, COPY, POST, LIST) is much more expensive per thousand than Tier2 (GET).
3. Data transfer. Internet egress and cross-region. NAT processing of that traffic bills under EC2, not S3.
4. Retrievals, early delete and Intelligent-Tiering monitoring. Appear when colder data is read or deleted before the minimum duration.
5. Hidden bytes. Noncurrent versions, incomplete multipart uploads and replication copies sit inside `BucketSizeBytes` but not in a normal listing.

October 2026 in this account: `Requests-Tier1` was the largest S3 line, then `TimedStorage-ByteHrs`, then `Requests-Tier2`. Re-query before citing dollars.

## Top 5 metrics

Namespace `AWS/S3`, dimension `BucketName`. Storage metrics are daily. Request metrics are opt-in and best-effort. CUR is the source of truth for request cost.

1. `BucketSizeBytes` by `StorageType`
2. `NumberOfObjects` (average size = bytes / objects)
3. `GetRequests` and `PutRequests`
4. `BytesDownloaded` and `BytesUploaded`
5. `ListRequests` and `DeleteRequests` (LIST is priced like PUT)

Lifecycle, versioning and incomplete multipart uploads come from the bucket APIs, not CloudWatch.

## What CUR alone can show

| Check | Usage type match |
| --- | --- |
| Standard storage | `TimedStorage-ByteHrs` and not a class suffix |
| Infrequent / Glacier / IT | `-SIA-`, `-GIR-`, `-Glacier`, `-INT-` |
| PUT/LIST vs GET | `Requests-Tier1` vs `Requests-Tier2` |
| Egress | `DataTransfer-Out-Bytes`, `AWS-Out-Bytes` |
| Early delete | `EarlyDelete-*` |

A shared bucket cannot be split by prefix in CUR. Label any prefix split estimated.

## Example: buckets by storage and request cost

```sql
SELECT line_item_resource_id AS bucket,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%TimedStorage%'), 2) AS storage_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%Requests-Tier1%'), 2) AS tier1_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%Requests-Tier2%'), 2) AS tier2_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%DataTransfer-Out%'), 2) AS egress_cost,
       ROUND(SUM(line_item_unblended_cost), 2) AS total_cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonS3'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1
ORDER BY total_cost DESC
LIMIT 20
```
