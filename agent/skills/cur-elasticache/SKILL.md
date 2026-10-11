---
name: cur-elasticache
description: >
  ElastiCache node-based cost analysis from CUR: node-hours by type, backups
  and the memory and CPU metrics that show idle capacity.
  Load after cur-athena when the service in scope is ElastiCache.
---

# ElastiCache in CUR

Filter every query with `line_item_product_code = 'AmazonElastiCache'`.
This account uses node-based clusters, not Serverless. Cost is mostly fixed: nodes bill whether or not they are busy.

`line_item_resource_id` may be a node (`<cluster>-0001-001`) or a replication group. Check which before you roll up.

## Top 5 cost drivers

1. Node-hours times node type. About 95% of a typical bill.
2. Node count. Every replica is a full-price node.
3. Engine and generation. Valkey is cheaper than Redis OSS on the same node. Graviton is cheaper. Redis OSS past end of life adds an Extended Support premium.
4. Commitment coverage. Steady on-demand nodes with no reserved nodes leave a large discount unused. Use effective cost for reserved and Savings Plan lines.
5. Backups and cross-AZ traffic. Usually small. Snapshots grow with retention.

October 2026 in this account the lines were `NodeUsage:cache.t4g.small`, `cache.t4g.medium` and `cache.t4g.micro`. Re-query before citing dollars.

## Top 5 metrics

Namespace `AWS/ElastiCache`, dimensions cache cluster id and cache node id.

1. `DatabaseMemoryUsagePercentage`. Under about 30% suggests an oversized cluster.
2. `EngineCPUUtilization`. Low CPU plus low memory is waste.
3. `CurrConnections`. Near zero means idle.
4. `GetTypeCmds` and `SetTypeCmds` for command rate.
5. `Evictions`. High evictions means do not downsize.

Node count and node type come from `DescribeReplicationGroups` and `DescribeCacheClusters`, not from metrics. CloudWatch does not say which service sent the traffic.

## Attribution

Per cluster is direct. A shared cluster needs an allocation signal (key prefix, operation share, or ACL user) and must be labelled estimated. Idle capacity can sit in an explicit headroom bucket instead of being pushed onto a service.

## Example: clusters by node type

```sql
SELECT line_item_resource_id AS node,
       line_item_usage_type,
       ROUND(SUM(line_item_usage_amount), 2) AS node_hours,
       ROUND(SUM(line_item_unblended_cost), 2) AS cost
FROM cost_report
WHERE billing_period = '2026-10'
  AND line_item_product_code = 'AmazonElastiCache'
  AND line_item_line_item_type = 'Usage'
GROUP BY 1, 2
ORDER BY cost DESC
LIMIT 20
```
