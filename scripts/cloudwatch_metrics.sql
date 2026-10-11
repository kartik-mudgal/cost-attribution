-- Static October 2026 CloudWatch snapshot for the cost investigator.
-- Flat copies of the hive-partitioned parquet live in cloudwatch/athena/
-- because region and service are columns inside the files, so they cannot
-- also be Athena partition keys.
--
-- Query the view, not the external table. billing_period is a constant so
-- athena_query's billing_period check succeeds.

CREATE EXTERNAL TABLE IF NOT EXISTS hackathon_cur.cloudwatch_daily (
  account string,
  region string,
  service string,
  namespace string,
  metric string,
  stat string,
  grain string,
  `timestamp` timestamp,
  value double,
  period_seconds int,
  status string,
  topic_name string,
  application string,
  platform string,
  function_name string,
  resource string,
  cache_cluster_id string,
  cache_node_id string,
  replication_group_id string,
  node_group_id string,
  role string,
  instance_id string,
  instance_type string,
  image_id string,
  volume_id string,
  queue_name string,
  bucket_name string,
  storage_type string,
  filter_id string,
  table_name string,
  global_secondary_index_name string,
  source string,
  operation string,
  model_id string
)
STORED AS PARQUET
LOCATION 's3://cost-investigator-skills-270149200236/cloudwatch/athena/'
TBLPROPERTIES ('parquet.compression'='ZSTD');

DROP VIEW IF EXISTS hackathon_cur.cloudwatch_metrics;

CREATE VIEW hackathon_cur.cloudwatch_metrics AS
SELECT
  '2026-10' AS billing_period,
  account,
  region,
  service,
  namespace,
  metric,
  stat,
  grain,
  "timestamp" AS metric_time,
  value,
  period_seconds,
  status,
  topic_name,
  application,
  platform,
  function_name,
  resource,
  cache_cluster_id,
  cache_node_id,
  replication_group_id,
  node_group_id,
  role,
  instance_id,
  instance_type,
  image_id,
  volume_id,
  queue_name,
  bucket_name,
  storage_type,
  filter_id,
  table_name,
  global_secondary_index_name,
  source,
  operation,
  model_id
FROM hackathon_cur.cloudwatch_daily;
