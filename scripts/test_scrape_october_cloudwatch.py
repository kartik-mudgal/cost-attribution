"""Catalog and query-shape tests for the October CloudWatch scrape."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrape_october_cloudwatch as scrape


def listed(name, *dim_names, value="v"):
    return {
        "MetricName": name,
        "Dimensions": [{"Name": dim, "Value": value} for dim in dim_names],
    }


class TestCatalog(unittest.TestCase):
    def test_notion_metrics_are_in_the_catalog(self):
        present = {}
        for group in scrape.GROUPS:
            present.setdefault(group.service, set()).update(spec.name for spec in group.metrics)
        for service, names in scrape.NOTION_METRICS.items():
            for name in names:
                self.assertIn(name, present[service], f"{service} missing {name}")

    def test_service_order_follows_the_pitch(self):
        self.assertEqual(
            scrape.service_names(),
            ["sns", "lambda", "elasticache", "ec2", "sqs", "s3", "dynamodb", "bedrock"],
        )

    def test_october_window_is_utc_and_exclusive_at_november(self):
        start, end = scrape.october_window(2026)
        self.assertEqual(start, datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 1, tzinfo=timezone.utc))


class TestSelection(unittest.TestCase):
    def _group(self, service, namespace):
        matches = [group for group in scrape.GROUPS if group.service == service and group.namespace == namespace]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_dynamodb_keeps_source_and_index_and_drops_operation(self):
        group = self._group("dynamodb", "AWS/DynamoDB")
        series, skipped = scrape.select_series(
            group,
            [
                listed("ConsumedWriteCapacityUnits", "TableName"),
                listed("ConsumedWriteCapacityUnits", "TableName", "Source"),
                listed("ConsumedWriteCapacityUnits", "TableName", "GlobalSecondaryIndexName"),
                listed("ConsumedWriteCapacityUnits", "TableName", "Operation"),
                listed("ReturnedItemCount", "TableName", "Operation"),
            ],
        )
        kept = {(item.metric, tuple(name for name, _ in item.dimensions)) for item in series}
        self.assertIn(("ConsumedWriteCapacityUnits", ("TableName",)), kept)
        self.assertIn(("ConsumedWriteCapacityUnits", ("TableName", "Source")), kept)
        self.assertIn(("ConsumedWriteCapacityUnits", ("TableName", "GlobalSecondaryIndexName")), kept)
        self.assertNotIn(("ConsumedWriteCapacityUnits", ("TableName", "Operation")), kept)
        self.assertIn(("ReturnedItemCount", ("TableName", "Operation")), kept)
        self.assertEqual(skipped[("ConsumedWriteCapacityUnits", ("Operation", "TableName"))], 1)

    def test_lambda_invocations_stay_on_function_name(self):
        group = self._group("lambda", "AWS/Lambda")
        series, _ = scrape.select_series(
            group,
            [
                listed("Invocations", "FunctionName"),
                listed("Invocations", "FunctionName", "Resource"),
                listed("ProvisionedConcurrencyUtilization", "FunctionName", "Resource"),
            ],
        )
        invocations = [item for item in series if item.metric == "Invocations"]
        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0].dimensions, (("FunctionName", "v"),))
        self.assertTrue(any(item.metric == "ProvisionedConcurrencyUtilization" for item in series))

    def test_bedrock_keeps_model_id_only(self):
        group = self._group("bedrock", "AWS/Bedrock")
        series, skipped = scrape.select_series(
            group,
            [
                listed("Invocations", "ModelId"),
                listed("Invocations"),
                listed("Invocations", "ModelId", "ContextWindow"),
            ],
        )
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0].dimensions, (("ModelId", "v"),))
        self.assertGreater(sum(skipped.values()), 0)

    def test_sns_keeps_topics_and_mobile_push(self):
        group = self._group("sns", "AWS/SNS")
        series, _ = scrape.select_series(
            group,
            [
                listed("NumberOfMessagesPublished", "TopicName"),
                listed("NumberOfMessagesPublished", "Application"),
                listed("NumberOfNotificationsDelivered", "Application", "Platform"),
                listed("PublishSize", "Application"),
            ],
        )
        metrics = {(item.metric, tuple(name for name, _ in item.dimensions)) for item in series}
        self.assertIn(("NumberOfMessagesPublished", ("TopicName",)), metrics)
        self.assertIn(("NumberOfMessagesPublished", ("Application",)), metrics)
        self.assertIn(("NumberOfNotificationsDelivered", ("Application", "Platform")), metrics)
        self.assertNotIn(("PublishSize", ("Application",)), metrics)

    def test_elasticache_skips_account_wide_series(self):
        group = self._group("elasticache", "AWS/ElastiCache")
        series, skipped = scrape.select_series(
            group,
            [
                listed("DatabaseMemoryUsagePercentage", "CacheClusterId"),
                listed("DatabaseMemoryUsagePercentage", "CacheClusterId", "CacheNodeId"),
                listed("DatabaseMemoryUsagePercentage"),
            ],
        )
        self.assertEqual(len(series), 4)  # Average and Maximum for cluster and node
        self.assertEqual(skipped[("DatabaseMemoryUsagePercentage", ())], 1)

    def test_elasticache_keeps_replication_group_series(self):
        group = self._group("elasticache", "AWS/ElastiCache")
        series, _ = scrape.select_series(
            group,
            [
                listed("EngineCPUUtilization", "ReplicationGroupId"),
                listed("EngineCPUUtilization", "ReplicationGroupId", "NodeGroupId"),
                listed("EngineCPUUtilization", "ReplicationGroupId", "Role"),
            ],
        )
        grains = {scrape.grain(item.dimensions) for item in series}
        self.assertEqual(grains, {"replication-group"})
        self.assertEqual(len(series), 6)

    def test_s3_storage_requires_storage_type(self):
        group = self._group("s3", "AWS/S3")
        series, _ = scrape.select_series(
            group,
            [
                listed("BucketSizeBytes", "BucketName", "StorageType"),
                listed("BucketSizeBytes", "BucketName"),
                listed("GetRequests", "BucketName", "FilterId"),
            ],
        )
        names = {item.metric for item in series}
        self.assertEqual(names, {"BucketSizeBytes", "GetRequests"})


class TestQueries(unittest.TestCase):
    def test_batches_stay_within_get_metric_data_limit(self):
        series = [
            scrape.Series("sqs", "AWS/SQS", "NumberOfMessagesSent", "Sum", (("QueueName", f"q{i}"),))
            for i in range(1000)
        ]
        batches = scrape.chunks(series)
        self.assertTrue(all(len(batch) <= 400 for batch in batches))
        self.assertEqual(sum(len(batch) for batch in batches), 1000)

    def test_query_ids_are_unique_and_legal(self):
        batch = [
            scrape.Series("sqs", "AWS/SQS", "NumberOfMessagesSent", "Sum", (("QueueName", "a"),)),
            scrape.Series("sqs", "AWS/SQS", "NumberOfEmptyReceives", "Sum", (("QueueName", "a"),)),
        ]
        queries, meta = scrape.metric_queries(batch)
        ids = [query["Id"] for query in queries]
        self.assertEqual(ids, ["m0", "m1"])
        self.assertEqual(set(meta), set(ids))
        self.assertEqual(queries[0]["MetricStat"]["Period"], 86400)
        self.assertEqual(queries[0]["MetricStat"]["Stat"], "Sum")

    def test_duration_requests_sum_average_p99_and_maximum(self):
        group = next(item for item in scrape.GROUPS if item.service == "lambda")
        duration = next(spec for spec in group.metrics if spec.name == "Duration")
        self.assertEqual(duration.stats, ("Sum", "Average", "p99", "Maximum"))


class TestRollup(unittest.TestCase):
    def test_sum_totals_and_average_does_not(self):
        start, end = scrape.october_window(2026)
        series = scrape.Series("sqs", "AWS/SQS", "NumberOfMessagesSent", "Sum", (("QueueName", "a"),))
        row = scrape.row_for(series, [{"t": "2026-10-01T00:00:00+00:00", "v": 2}, {"t": "2026-10-02T00:00:00+00:00", "v": 4}], "us-east-1", start, end, "Complete")
        summary = scrape.summarize(row)
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["days"], 2)
        self.assertEqual(summary["max"], 4)

        size = scrape.Series("sqs", "AWS/SQS", "SentMessageSize", "Average", (("QueueName", "a"),))
        size_row = scrape.row_for(size, [{"t": "2026-10-01T00:00:00+00:00", "v": 100}], "us-east-1", start, end, "Complete")
        self.assertIsNone(scrape.summarize(size_row)["total"])

    def test_points_are_normalized_to_utc(self):
        from datetime import timedelta

        ist = timezone(timedelta(hours=5, minutes=30))
        stamp = datetime(2026, 10, 1, 5, 30, tzinfo=ist)
        points = scrape.points_from_result({"Timestamps": [stamp], "Values": [3]})
        self.assertEqual(points, [{"t": "2026-10-01T00:00:00+00:00", "v": 3}])

    def test_merge_results_concatenates_pages(self):
        merged, messages = scrape.merge_results(
            [
                {"MetricDataResults": [{"Id": "m0", "Timestamps": ["t1"], "Values": [1], "StatusCode": "PartialData"}], "Messages": [{"Code": "x"}]},
                {"MetricDataResults": [{"Id": "m0", "Timestamps": ["t2"], "Values": [2], "StatusCode": "Complete"}]},
            ]
        )
        self.assertEqual(merged["m0"]["Values"], [1, 2])
        self.assertEqual(merged["m0"]["status"], "PartialData")
        self.assertEqual(messages, [{"Code": "x"}])


class TestParquet(unittest.TestCase):
    def test_catalog_dimensions_have_parquet_columns(self):
        used = set()
        for group in scrape.GROUPS:
            for spec in group.metrics:
                for names in spec.dimension_sets:
                    used.update(names)
        mapped = {name for name, _ in scrape.DIMENSION_FIELDS}
        self.assertEqual(used - mapped, set())

    def test_point_rows_are_one_day_each(self):
        start, end = scrape.october_window(2026)
        series = scrape.Series("sqs", "AWS/SQS", "NumberOfMessagesSent", "Sum", (("QueueName", "a"),))
        row = scrape.row_for(
            series,
            [{"t": "2026-10-01T00:00:00+00:00", "v": 2}, {"t": "2026-10-02T00:00:00+00:00", "v": 4}],
            "us-east-1",
            start,
            end,
            "Complete",
        )
        rows = scrape.point_rows(row, "020040093233")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["account"], "020040093233")
        self.assertEqual(rows[0]["queue_name"], "a")
        self.assertIsNone(rows[0]["table_name"])
        self.assertEqual(rows[0]["value"], 2)
        self.assertEqual(rows[1]["timestamp"], datetime(2026, 10, 2, tzinfo=timezone.utc))

    def test_unknown_dimension_is_rejected(self):
        start, end = scrape.october_window(2026)
        series = scrape.Series("sqs", "AWS/SQS", "NumberOfMessagesSent", "Sum", (("QueueName", "a"),))
        row = scrape.row_for(series, [{"t": "2026-10-01T00:00:00+00:00", "v": 1}], "us-east-1", start, end, "Complete")
        row["dimensions"]["NotADimension"] = "x"
        with self.assertRaises(ValueError):
            scrape.point_rows(row, "020040093233")

    def test_parquet_roundtrip(self):
        import tempfile

        try:
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("pyarrow not installed")
        start, end = scrape.october_window(2026)
        series = scrape.Series("sqs", "AWS/SQS", "NumberOfEmptyReceives", "Sum", (("QueueName", "q"),))
        row = scrape.row_for(series, [{"t": "2026-10-01T00:00:00+00:00", "v": 9}], "us-east-1", start, end, "Complete")
        with tempfile.TemporaryDirectory() as tmp:
            path = scrape.metrics_path(Path(tmp), "us-east-1", "sqs")
            sink = scrape.ParquetSink(path)
            sink.write(scrape.point_rows(row, "020040093233"))
            sink.close()
            table = pq.read_table(path)
        self.assertEqual(table.num_rows, 1)
        self.assertEqual(table.column("queue_name")[0].as_py(), "q")
        self.assertEqual(table.column("value")[0].as_py(), 9.0)
        self.assertIn("region=us-east-1/service=sqs/metrics.parquet", path.as_posix())


if __name__ == "__main__":
    unittest.main()
