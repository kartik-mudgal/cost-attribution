---
name: cur-sns
description: >
  SNS cost analysis from CUR: publish requests, deliveries, data transfer and
  filtering, plus the topic metrics that explain fan-out.
  Load after cur-athena when the service in scope is SNS.
---

# SNS in CUR

Filter every query with `line_item_product_code = 'AmazonSNS'`.
`line_item_resource_id` is the topic ARN. Downstream SQS and Lambda charges are not in this product code.

## Top 5 cost drivers

1. Data transfer out on delivered bytes. Fan-out multiplies bytes. Often larger than the request charge once delivered bytes per publish pass about 5.5 KB.
2. Publish requests. Billed per 64 KB chunk, so large payloads multiply the count.
3. Per-delivery charges for endpoints other than SQS and Lambda. HTTP, mobile push, Firehose and email bill per delivery. SQS and Lambda deliveries are free on the SNS line.
4. Payload-based filtering. Billed per GB scanned per subscription with a payload filter. Filtered messages still count.
5. FIFO and extras. FIFO bills per publish and per subscription message. Archiving, replay, message data protection, KMS and extended-payload S3 are extra lines.

October 2026 in this account: `DeliveryAttempts-GCM` and `Requests-Tier1` were about equal and were the two large SNS lines, then `DataTransfer-Out-Bytes`. Re-query before citing dollars.

## Top 5 metrics

Namespace `AWS/SNS`, dimension `TopicName`.

1. `NumberOfMessagesPublished` (P)
2. `PublishSize` (S). Watch the 64 KB chunk boundary.
3. `NumberOfNotificationsDelivered` (D). D / P is fan-out. D times S drives transfer.
4. `NumberOfNotificationsFilteredOut` (F)
5. `NumberOfNotificationsFailed`

Mobile push has no topic dimension. Attribute it by platform application ARN. Filter-policy count is not a metric. Read it from `list-subscriptions-by-topic`.

## What CUR alone can show

| Check | Usage type |
| --- | --- |
| Publishes | `Requests-Tier1` |
| Mobile push deliveries | `DeliveryAttempts-GCM` and other `DeliveryAttempts-*` |
| Transfer | `DataTransfer-Out-Bytes` |
| HTTP deliveries | `DeliveryAttempts-HTTP` |

SQS and Lambda deliveries have no SNS cost line. Add those services back when judging a topic's true cost. Shared topics cannot be split by CUR. Label any split estimated.

## Example: topics by publish, delivery and transfer

```sql
SELECT line_item_resource_id AS topic,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%Requests-Tier1%'), 2) AS publish_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%DeliveryAttempts%'), 2) AS delivery_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%DataTransfer-Out%'), 2) AS transfer_cost,
       ROUND(SUM(line_item_unblended_cost), 2) AS total_cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonSNS'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1
ORDER BY total_cost DESC
LIMIT 20
```
