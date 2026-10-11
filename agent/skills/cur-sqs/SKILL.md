---
name: cur-sqs
description: >
  SQS cost analysis from CUR: how SQS is billed, which operations drive cost, and the waste
  checks (URL lookups per send, batching, empty receives) with example Athena queries.
  Load after cur-athena when the service in scope is SQS. Product code in this account is AWSQueueService, not AmazonSQS.
---

# SQS in CUR

## Top 5 cost drivers

1. Request count. Send, receive, delete and change-visibility each bill. An unbatched message is at least 3 requests.
2. Empty receives. Short polling and idle pollers can exceed real traffic. Long polling (up to 20s) cuts it.
3. Payload size. Each 64 KB chunk is one request.
4. FIFO and fair queues. FIFO bills higher. Standard queues using message group IDs bill at both rates.
5. Adjacent charges. KMS for encrypted queues, S3 for the extended client, and cross-region transfer. Same-region transfer is free.

## Top 5 metrics

Namespace `AWS/SQS`, dimension `QueueName`.

1. `NumberOfMessagesSent`
2. `NumberOfEmptyReceives` against non-empty receives
3. `NumberOfMessagesReceived` (includes redeliveries)
4. `SentMessageSize` (64 KB chunk boundary)
5. `ApproximateNumberOfMessagesVisible` and `ApproximateAgeOfOldestMessage`

CloudWatch does not give the raw `ReceiveMessage` API count. Use CUR `line_item_operation = 'Receive'` for that.

Filter every query with `line_item_product_code = 'AWSQueueService'`.
`line_item_resource_id` is the queue ARN (`arn:aws:sqs:<region>:<account>:<queue-name>`).

## How SQS is billed

Every API call is a billed request, including receives that return nothing. Each 64 KB chunk of
a payload counts as one request. Usage types are `Requests-RBP`, `Requests-FIFO-RBP` and
`Requests-Fair-RBP` (with a region prefix), and `line_item_operation` says which call it was:

| Operation | Call |
| --- | --- |
| `Send`, `SendMessageBatch` | Producers sending |
| `Receive` | Consumers polling (empty receives included) |
| `DeleteMessage`, `DeleteMessageBatch` | Consumers acknowledging |
| `ChgVis` | Changing visibility timeout |
| `GetQueueUrl` | Looking up the queue URL |
| `GetQueueAttributes` | Reading queue depth or settings |

For SQS, `line_item_usage_amount` is the request count, so request volume per operation comes
straight from CUR.

KMS-encrypted queues also make KMS calls. Those are billed under product code `awskms`, not
SQS, so they won't appear in an SQS-scoped query. Mention it if a queue is encrypted and the
finding involves request volume.

## What to check

Split each change into volume (more requests) and waste (requests that do no useful work).

| Signal (from CUR) | Formula | If it is high |
| --- | --- | --- |
| URL lookups per send | `GetQueueUrl` requests ÷ (`Send` + `SendMessageBatch`) requests | Close to 1: the client looks up the URL on every send instead of caching it |
| Send batching | `Send` requests ÷ (`Send` + `SendMessageBatch`) requests | Close to 1: producers aren't batching |
| Delete batching | `DeleteMessage` ÷ (`DeleteMessage` + `DeleteMessageBatch`) requests | Close to 1: deletes aren't batched |
| Attribute polling | `GetQueueAttributes` requests per hour | Autoscalers or health checks polling queue depth too often |
| Receive share | `Receive` cost ÷ queue cost | Receives dominate: likely short polling or idle consumers |

Load `cloudwatch-metrics` and read these from `cloudwatch_metrics`. If a metric returns no
rows, it is absent from the October snapshot: say so and lower your confidence:

- Empty-receive ratio: `NumberOfEmptyReceives` ÷ CUR `Receive` requests. High means short
  polling (`ReceiveMessageWaitTimeSeconds` = 0) or too many idle consumers.
  Fix: `WaitTimeSeconds=20`, fewer pollers.
- Messages per receive: `NumberOfMessagesReceived` ÷ non-empty receives. Close to 1 means set
  `MaxNumberOfMessages=10`.
- Redelivery rate: `NumberOfMessagesReceived` ÷ `NumberOfMessagesDeleted`. Above 1 means the
  visibility timeout is too short or consumers are failing.
- `SentMessageSize` over 64 KB: each message bills as several requests. Fix: store the body in
  S3 and send a pointer.

Known pattern in dev: several queues (hubble-parquet-ingest-queue, argus, pythagoras,
native-logging) spend about as much on `GetQueueUrl` as on `Send`.

## Estimating savings

Saving = the waste requests removed × the queue's own cost per request for that operation, from
the same query. Express it per month (daily × 30) and state the assumption, e.g. "if
GetQueueUrl drops to near zero after caching the URL".

## Example: operation mix for one queue

```sql
SELECT line_item_operation,
       ROUND(SUM(line_item_usage_amount), 0) AS requests,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AWSQueueService'
  AND line_item_line_item_type = 'Usage'
  AND line_item_resource_id LIKE '%:my-queue'
GROUP BY 1
ORDER BY cost DESC
```

## Example: queues that look up their URL on every send

```sql
WITH ops AS (
  SELECT line_item_resource_id AS queue,
         SUM(line_item_usage_amount) FILTER (WHERE line_item_operation = 'GetQueueUrl') AS get_url,
         SUM(line_item_usage_amount) FILTER (WHERE line_item_operation IN ('Send', 'SendMessageBatch')) AS sends,
         SUM(line_item_unblended_cost) FILTER (WHERE line_item_operation = 'GetQueueUrl') AS get_url_cost
  FROM cost_report
  WHERE billing_period = '2026-10'
    AND line_item_product_code = 'AWSQueueService'
    AND line_item_line_item_type = 'Usage'
  GROUP BY 1
)
SELECT queue,
       ROUND(get_url, 0) AS get_url_requests,
       ROUND(sends, 0) AS send_requests,
       ROUND(get_url / NULLIF(sends, 0), 2) AS lookups_per_send,
       ROUND(get_url_cost, 2) AS get_url_cost
FROM ops
WHERE get_url > 0
ORDER BY get_url_cost DESC
LIMIT 20
```

## Example: batching across queues

```sql
SELECT line_item_resource_id AS queue,
       ROUND(SUM(line_item_usage_amount) FILTER (WHERE line_item_operation = 'Send'), 0) AS single_sends,
       ROUND(SUM(line_item_usage_amount) FILTER (WHERE line_item_operation = 'SendMessageBatch'), 0) AS batch_sends,
       ROUND(SUM(line_item_usage_amount) FILTER (WHERE line_item_operation = 'DeleteMessage'), 0) AS single_deletes,
       ROUND(SUM(line_item_usage_amount) FILTER (WHERE line_item_operation = 'DeleteMessageBatch'), 0) AS batch_deletes,
       ROUND(SUM(line_item_unblended_cost), 2) AS queue_cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AWSQueueService'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1
ORDER BY queue_cost DESC
LIMIT 20
```
