---
name: cloudwatch-metrics
description: >
  Query the static October 2026 CloudWatch daily metrics in the Athena view
  cloudwatch_metrics. Load when a cost finding needs metric values for SQS,
  DynamoDB, EC2, EBS, S3, SNS, Lambda, ElastiCache or Bedrock.
---

# Static CloudWatch metrics

October 2026 daily CloudWatch values for dev account `020040093233` in `us-east-1` are already in Athena. Query the view `cloudwatch_metrics` with `athena_query`. Do not call CloudWatch. Do not invent a value that this view does not return.

The snapshot was taken on 2026-10-11. `metric_time` of 2026-10-11 is a partial day. There is no other month. S3 request metrics, EC2 memory (`mem_used_percent`), and Lambda provisioned concurrency were not publishing, so those queries return no rows.

Source objects: `s3://cost-investigator-skills-270149200236/cloudwatch/year=2026/month=10/region=us-east-1/`.

## Rules

- Filter `billing_period = '2026-10'` on every query. The tool rejects SQL that omits it.
- Filter `service` to the service in scope: `sqs`, `dynamodb`, `ec2`, `s3`, `sns`, `lambda`, `elasticache`, `bedrock`.
- One row is one series for one UTC day. `value` is that day's stat, not a month total.
- `stat = 'Sum'`: `SUM(value)` across days is the month total.
- `stat` of `Average`, `Maximum`, `Minimum`, or `p99`: do not sum the days. Use `AVG(value)` or `MAX(value)`.
- Aggregate. The view has about 878,000 rows. Rank with `ORDER BY ... LIMIT`.
- If the query returns no rows, say the metric is absent from this snapshot and lower your confidence.

## Do not add these together

| Rows | Why |
| --- | --- |
| DynamoDB `grain = 'resource'` and `grain = 'source'` for the same table | The table series already includes every source |
| ElastiCache `grain = 'resource'` and `grain = 'node'` | The cluster series already includes its nodes |
| ElastiCache `grain = 'replication-group'` and the cluster or node series | Same traffic, different dimension |
| SNS `grain = 'platform-application'` and `grain = 'resource'` | Mobile push is not a topic |

Index rows (`global_secondary_index_name` set) are not inside the base table series. Add them only when the question includes indexes.

## Columns

`billing_period`, `account`, `region`, `service`, `namespace`, `metric`, `stat`, `grain`, `metric_time`, `value`, `period_seconds`, `status`.

Dimension columns are null when that metric does not use them: `queue_name`, `table_name`, `global_secondary_index_name`, `source`, `operation`, `function_name`, `instance_id`, `volume_id`, `bucket_name`, `storage_type`, `filter_id`, `topic_name`, `application`, `platform`, `model_id`, `cache_cluster_id`, `cache_node_id`, `replication_group_id`, `node_group_id`, `role`, `resource`, `instance_type`, `image_id`.

## Metrics in the snapshot

| service | metrics |
| --- | --- |
| sqs | `NumberOfMessagesSent`, `NumberOfMessagesReceived`, `NumberOfEmptyReceives`, `SentMessageSize`, `ApproximateNumberOfMessagesVisible`, `ApproximateAgeOfOldestMessage` |
| dynamodb | `ConsumedWriteCapacityUnits`, `ConsumedReadCapacityUnits`, `ProvisionedWriteCapacityUnits`, `ProvisionedReadCapacityUnits`, `WriteThrottleEvents`, `ReadThrottleEvents`, `ReturnedItemCount` |
| ec2 | `CPUUtilization`, `NetworkIn`, `NetworkOut`, `EBSReadBytes`, `EBSWriteBytes`, `EBSReadOps`, `EBSWriteOps`, `CPUCreditBalance`, `CPUCreditUsage`, plus `AWS/EBS` `VolumeIdleTime`, `VolumeReadOps`, `VolumeWriteOps` |
| s3 | `BucketSizeBytes`, `NumberOfObjects` |
| sns | `NumberOfMessagesPublished`, `PublishSize`, `NumberOfNotificationsDelivered`, `NumberOfNotificationsFailed` |
| lambda | `Duration`, `Invocations`, `Errors`, `Throttles`, `ConcurrentExecutions` |
| elasticache | `DatabaseMemoryUsagePercentage`, `EngineCPUUtilization`, `CurrConnections`, `GetTypeCmds`, `SetTypeCmds`, `Evictions` |
| bedrock | `OutputTokenCount`, `InputTokenCount`, `CacheReadInputTokenCount`, `CacheWriteInputTokenCount`, `Invocations`, `InvocationClientErrors`, `InvocationServerErrors` |

## Example: queues with the most empty receives

```sql
SELECT queue_name,
       SUM(value) AS empty_receives
FROM cloudwatch_metrics
WHERE billing_period = '2026-10'
  AND service = 'sqs'
  AND metric = 'NumberOfEmptyReceives'
  AND stat = 'Sum'
GROUP BY 1
ORDER BY empty_receives DESC
LIMIT 20
```

## Example: daily write units for one table

```sql
SELECT metric_time,
       grain,
       source,
       global_secondary_index_name,
       SUM(value) AS write_units
FROM cloudwatch_metrics
WHERE billing_period = '2026-10'
  AND service = 'dynamodb'
  AND table_name = 'my-table'
  AND metric = 'ConsumedWriteCapacityUnits'
  AND stat = 'Sum'
GROUP BY 1, 2, 3, 4
ORDER BY 1
```

## Example: token totals by model

```sql
SELECT model_id,
       metric,
       SUM(value) AS tokens
FROM cloudwatch_metrics
WHERE billing_period = '2026-10'
  AND service = 'bedrock'
  AND metric IN ('InputTokenCount', 'OutputTokenCount', 'CacheReadInputTokenCount', 'CacheWriteInputTokenCount')
  AND stat = 'Sum'
GROUP BY 1, 2
ORDER BY tokens DESC
LIMIT 30
```
