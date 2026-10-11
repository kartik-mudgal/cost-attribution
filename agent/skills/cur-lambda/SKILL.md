---
name: cur-lambda
description: >
  Lambda cost analysis from CUR: GB-seconds, invocations, provisioned concurrency
  and the log and NAT costs that sit outside the Lambda line.
  Load after cur-athena when the service in scope is Lambda.
---

# Lambda in CUR

Filter every query with `line_item_product_code = 'AWSLambda'`.
`line_item_resource_id` is the function ARN. Confirm whether provisioned-concurrency lines carry an alias or version.

## Top 5 cost drivers

1. Duration in GB-seconds. Memory times runtime. Past a short threshold this dominates the request charge.
2. Invocation count. Retries, async redelivery and payloads over 256 KB add billed requests.
3. Memory over-provisioning and architecture. Unused memory scales every GB-second. Arm (`-ARM` usage types) is cheaper for the same work.
4. Provisioned concurrency. Billed for configured time whether or not it is invoked.
5. Costs outside the Lambda line. CloudWatch Logs ingestion, NAT bytes for VPC functions (under EC2), and ephemeral storage above 512 MB.

October 2026 in this account Lambda was small: `Request`, `DataTransfer-Out-Bytes`, then VPC peering and tenant-isolated memory. Re-query before citing dollars.

## Top 5 metrics

Namespace `AWS/Lambda`, dimension `FunctionName`.

1. `Duration` Sum, Average, p99 and Max. Sum feeds GB-seconds.
2. `Invocations`
3. `Errors` and `Throttles` (retries are billed invocations)
4. `ProvisionedConcurrencyUtilization`
5. `ConcurrentExecutions`

Memory size, architecture and ephemeral storage come from `GetFunctionConfiguration`. Init time is in the log REPORT line, not a standard metric. Caller identity is not in CloudWatch.

## What CUR alone can show

| Check | Usage type |
| --- | --- |
| Requests | `Request`, `Request-ARM` |
| Duration | `Lambda-GB-Second`, `Lambda-GB-Second-ARM` |
| Ephemeral storage | `Lambda-Storage-GB-Second` |
| Provisioned concurrency | `Lambda-Provisioned-Concurrency`, `Lambda-Provisioned-GB-Second` |
| Logs | product code `AmazonCloudWatch`, log group `/aws/lambda/<name>` |

Savings Plans can cover duration. Use effective-cost columns for those lines.

## Example: functions by request and duration cost

```sql
SELECT line_item_resource_id AS function_arn,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%Request%'), 2) AS request_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%GB-Second%'), 2) AS duration_cost,
       ROUND(SUM(line_item_unblended_cost) FILTER (WHERE line_item_usage_type LIKE '%Provisioned%'), 2) AS pc_cost,
       ROUND(SUM(line_item_unblended_cost), 2) AS total_cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AWSLambda'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1
ORDER BY total_cost DESC
LIMIT 20
```
