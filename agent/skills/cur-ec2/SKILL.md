---
name: cur-ec2
description: >
  EC2 cost analysis from CUR: instance hours, EBS, NAT, data transfer and public IPv4,
  plus the CloudWatch metrics that show whether that capacity is used.
  Load after cur-athena when the service in scope is EC2.
---

# EC2 in CUR

Filter every query with `line_item_product_code = 'AmazonEC2'`.
This product line also holds EBS, NAT gateways and public IPv4. Cost is mostly fixed capacity.

`line_item_resource_id` is the instance id (`i-...`), volume id, NAT gateway id or address id.

## Top 5 cost drivers

1. Instance-hours times instance type. Idle and oversized instances bill the same as busy ones.
2. Purchase option. On-demand is the expensive path. Spot, Reserved Instances and Savings Plans cut the same hour. Use effective-cost columns for reserved and Savings Plan lines. Unblended cost is wrong on those lines.
3. EBS volumes and snapshots. gp3 is billed per provisioned GB-month plus extra IOPS and throughput, attached or not. gp2 and old snapshots are common waste.
4. Data transfer and NAT. Cross-AZ (`DataTransfer-Regional-Bytes`), internet egress, and NAT processing plus hourly gateway charge. Traffic to S3 or DynamoDB through a NAT is avoidable with gateway endpoints.
5. Public IPv4 and burstable credits. In-use and idle addresses both bill hourly. T-family unlimited mode bills surplus CPU credits.

October 2026 in this account, largest EC2 usage types: `DataTransfer-Regional-Bytes`, `EBS:VolumeUsage.gp3`, `NatGateway-Bytes`, `SpotUsage:*`, `EBS:VolumeP-IOPS.gp3`. Re-query before citing a dollar figure.

## Top 5 metrics

Namespace `AWS/EC2`, dimension `InstanceId`. Default monitoring is 5 minutes.

1. `CPUUtilization`. Sustained low CPU on a large instance means oversized.
2. Memory used percent. Not in `AWS/EC2`. Needs the CloudWatch agent. CPU alone hides memory-bound instances.
3. `NetworkIn` and `NetworkOut`. Includes free same-AZ traffic, so it overstates billable transfer.
4. `EBSReadBytes` and `EBSWriteBytes`, plus `AWS/EBS` for volume-level idle detection.
5. `CPUCreditBalance` and `CPUCreditUsage` for T-family instances.

Instance type, state and purchase option come from `DescribeInstances`. Volume size and attachment come from `DescribeVolumes`. NAT bytes: namespace `AWS/NATGateway`, `BytesOutToDestination` and `BytesInFromSource`.

## What CUR alone can show

| Check | How |
| --- | --- |
| Which cost line moved | Daily cost by `line_item_usage_type` |
| Spot vs on-demand | Usage types `SpotUsage:<type>` vs `BoxUsage:<type>` |
| Unattached or oversized EBS | `EBS:VolumeUsage.*` cost. Attachment state is the EC2 API, not CUR |
| NAT waste | `NatGateway-Hours` and `NatGateway-Bytes` per gateway resource id |
| Cross-AZ transfer | `DataTransfer-Regional-Bytes` |

Kubernetes nodes stop at the instance in CUR. Per-service split needs pod CPU and memory, and must be labelled estimated. Unused node capacity is a headroom bucket, not a service's cost.

## Example: cost lines per day

```sql
SELECT date(line_item_usage_start_date) AS day,
       line_item_usage_type,
       ROUND(SUM(line_item_usage_amount), 2) AS usage,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonEC2'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1, 2
ORDER BY 1, cost DESC
```
