#!/usr/bin/env python3
"""Scrape October CloudWatch metrics for the dev account.

Metrics follow the top 5 in the hackathon pitch:
https://app.notion.com/p/devrev/AWS-Cost-Attribution-by-Service-Hackathon-Pitch-3f581946d83381878efaef39fc56368b

Uses the AWS profile from SSO (`dev` by default), which is the account that
holds dev-cluster-2. Login first if the session is expired:

    aws sso login --profile dev

Then:

    uv run --with boto3 --with pyarrow python scripts/scrape_october_cloudwatch.py

Daily points (period 86400, midnight UTC), one row per series per day, land in
out/cloudwatch-2026-10/region=<region>/service=<service>/metrics.parquet
Sync that tree to S3. Region and service are both directory partitions and columns.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PERIOD = 86400
BATCH_SIZE = 400
SUMMABLE_STATS = frozenset({"Sum", "SampleCount"})

NOTES = (
    "Timestamps are period starts. End is exclusive, so 2026-11-01 includes October 31. The current day is partial.",
    "Do not add a DynamoDB TableName series to that table's TableName+Source series. Index series are separate from the table series.",
    "Do not add an ElastiCache CacheClusterId series to its node series or its ReplicationGroupId series. Those overlap.",
    "Lambda series use FunctionName only, so aliases are not added twice. ProvisionedConcurrencyUtilization uses FunctionName+Resource.",
    "S3 request metrics are opt-in. No GetRequests series means that bucket has no request-metrics configuration.",
    "EC2 memory is not in AWS/EC2. Zero CWAgent mem_used_percent series means the agent is not publishing it.",
    "SNS Application and Platform dimensions are mobile push, not topics.",
    "Parquet is one row per day per series. Publish region=/service=/ to S3.",
)

# CloudWatch dimension name -> parquet column. Nullable. Sparse across services.
DIMENSION_FIELDS: tuple[tuple[str, str], ...] = (
    ("TopicName", "topic_name"),
    ("Application", "application"),
    ("Platform", "platform"),
    ("FunctionName", "function_name"),
    ("Resource", "resource"),
    ("CacheClusterId", "cache_cluster_id"),
    ("CacheNodeId", "cache_node_id"),
    ("ReplicationGroupId", "replication_group_id"),
    ("NodeGroupId", "node_group_id"),
    ("Role", "role"),
    ("InstanceId", "instance_id"),
    ("InstanceType", "instance_type"),
    ("ImageId", "image_id"),
    ("VolumeId", "volume_id"),
    ("QueueName", "queue_name"),
    ("BucketName", "bucket_name"),
    ("StorageType", "storage_type"),
    ("FilterId", "filter_id"),
    ("TableName", "table_name"),
    ("GlobalSecondaryIndexName", "global_secondary_index_name"),
    ("Source", "source"),
    ("Operation", "operation"),
    ("ModelId", "model_id"),
)


@dataclass(frozen=True)
class MetricSpec:
    name: str
    stats: tuple[str, ...]
    dimension_sets: tuple[frozenset[str], ...]


@dataclass(frozen=True)
class MetricGroup:
    service: str
    namespace: str
    metrics: tuple[MetricSpec, ...]


@dataclass(frozen=True)
class Series:
    service: str
    namespace: str
    metric: str
    stat: str
    dimensions: tuple[tuple[str, str], ...]

    def label(self) -> str:
        dims = ",".join(f"{name}={value}" for name, value in self.dimensions)
        return f"{self.namespace}/{self.metric}/{self.stat}[{dims}]"


def _sets(*groups: set[str]) -> tuple[frozenset[str], ...]:
    return tuple(frozenset(group) for group in groups)


TOPIC = _sets({"TopicName"})
MOBILE = _sets({"Application"}, {"Platform"}, {"Application", "Platform"})
FUNCTION = _sets({"FunctionName"})
FUNCTION_RESOURCE = _sets({"FunctionName", "Resource"})
TABLE_SPLIT = _sets(
    {"TableName"},
    {"TableName", "Source"},
    {"TableName", "GlobalSecondaryIndexName"},
    {"TableName", "GlobalSecondaryIndexName", "Source"},
)
TABLE_INDEX = _sets({"TableName"}, {"TableName", "GlobalSecondaryIndexName"})
RETURNED = _sets(
    {"TableName"},
    {"TableName", "Operation"},
    {"TableName", "GlobalSecondaryIndexName"},
    {"TableName", "GlobalSecondaryIndexName", "Operation"},
)
NODE = _sets(
    {"CacheClusterId"},
    {"CacheClusterId", "CacheNodeId"},
    {"ReplicationGroupId"},
    {"ReplicationGroupId", "NodeGroupId"},
    {"ReplicationGroupId", "Role"},
)
INSTANCE = _sets({"InstanceId"})
VOLUME = _sets({"VolumeId"})
MODEL = _sets({"ModelId"})
STORAGE = _sets({"BucketName", "StorageType"})
REQUESTS = _sets({"BucketName", "FilterId"})
MEMORY_DIMS = _sets(
    {"InstanceId"},
    {"InstanceId", "InstanceType"},
    {"InstanceId", "ImageId"},
    {"InstanceId", "InstanceType", "ImageId"},
)

# Notion order: SNS, Lambda, ElastiCache, EC2, SQS, S3, DynamoDB, Bedrock.
GROUPS: tuple[MetricGroup, ...] = (
    MetricGroup(
        "sns",
        "AWS/SNS",
        (
            MetricSpec("NumberOfMessagesPublished", ("Sum",), TOPIC + MOBILE),
            MetricSpec("PublishSize", ("Average",), TOPIC),
            MetricSpec("NumberOfNotificationsDelivered", ("Sum",), TOPIC + MOBILE),
            MetricSpec("NumberOfNotificationsFilteredOut", ("Sum",), TOPIC),
            MetricSpec("NumberOfNotificationsFailed", ("Sum",), TOPIC + MOBILE),
        ),
    ),
    MetricGroup(
        "lambda",
        "AWS/Lambda",
        (
            MetricSpec("Duration", ("Sum", "Average", "p99", "Maximum"), FUNCTION),
            MetricSpec("Invocations", ("Sum",), FUNCTION),
            MetricSpec("Errors", ("Sum",), FUNCTION),
            MetricSpec("Throttles", ("Sum",), FUNCTION),
            MetricSpec("ProvisionedConcurrencyUtilization", ("Average", "Maximum"), FUNCTION_RESOURCE),
            MetricSpec("ConcurrentExecutions", ("Maximum",), FUNCTION),
        ),
    ),
    MetricGroup(
        "elasticache",
        "AWS/ElastiCache",
        (
            MetricSpec("DatabaseMemoryUsagePercentage", ("Average", "Maximum"), NODE),
            MetricSpec("EngineCPUUtilization", ("Average", "Maximum"), NODE),
            MetricSpec("CurrConnections", ("Average", "Maximum"), NODE),
            MetricSpec("GetTypeCmds", ("Sum",), NODE),
            MetricSpec("SetTypeCmds", ("Sum",), NODE),
            MetricSpec("Evictions", ("Sum",), NODE),
        ),
    ),
    MetricGroup(
        "ec2",
        "AWS/EC2",
        (
            MetricSpec("CPUUtilization", ("Average", "Maximum"), INSTANCE),
            MetricSpec("NetworkIn", ("Sum",), INSTANCE),
            MetricSpec("NetworkOut", ("Sum",), INSTANCE),
            MetricSpec("EBSReadBytes", ("Sum",), INSTANCE),
            MetricSpec("EBSWriteBytes", ("Sum",), INSTANCE),
            MetricSpec("EBSReadOps", ("Sum",), INSTANCE),
            MetricSpec("EBSWriteOps", ("Sum",), INSTANCE),
            MetricSpec("CPUCreditBalance", ("Minimum", "Average"), INSTANCE),
            MetricSpec("CPUCreditUsage", ("Sum",), INSTANCE),
        ),
    ),
    MetricGroup(
        "ec2",
        "CWAgent",
        (MetricSpec("mem_used_percent", ("Average", "Maximum"), MEMORY_DIMS),),
    ),
    MetricGroup(
        "ec2",
        "AWS/EBS",
        (
            MetricSpec("VolumeIdleTime", ("Sum",), VOLUME),
            MetricSpec("VolumeReadOps", ("Sum",), VOLUME),
            MetricSpec("VolumeWriteOps", ("Sum",), VOLUME),
        ),
    ),
    MetricGroup(
        "sqs",
        "AWS/SQS",
        (
            MetricSpec("NumberOfMessagesSent", ("Sum",), _sets({"QueueName"})),
            MetricSpec("NumberOfMessagesReceived", ("Sum",), _sets({"QueueName"})),
            MetricSpec("NumberOfEmptyReceives", ("Sum",), _sets({"QueueName"})),
            MetricSpec("SentMessageSize", ("Average",), _sets({"QueueName"})),
            MetricSpec("ApproximateNumberOfMessagesVisible", ("Average", "Maximum"), _sets({"QueueName"})),
            MetricSpec("ApproximateAgeOfOldestMessage", ("Maximum",), _sets({"QueueName"})),
        ),
    ),
    MetricGroup(
        "s3",
        "AWS/S3",
        (
            MetricSpec("BucketSizeBytes", ("Average",), STORAGE),
            MetricSpec("NumberOfObjects", ("Average",), STORAGE),
            MetricSpec("AllRequests", ("Sum",), REQUESTS),
            MetricSpec("GetRequests", ("Sum",), REQUESTS),
            MetricSpec("PutRequests", ("Sum",), REQUESTS),
            MetricSpec("BytesDownloaded", ("Sum",), REQUESTS),
            MetricSpec("BytesUploaded", ("Sum",), REQUESTS),
            MetricSpec("ListRequests", ("Sum",), REQUESTS),
            MetricSpec("DeleteRequests", ("Sum",), REQUESTS),
        ),
    ),
    MetricGroup(
        "dynamodb",
        "AWS/DynamoDB",
        (
            MetricSpec("ConsumedWriteCapacityUnits", ("Sum",), TABLE_SPLIT),
            MetricSpec("ConsumedReadCapacityUnits", ("Sum",), TABLE_SPLIT),
            MetricSpec("ProvisionedWriteCapacityUnits", ("Average", "Maximum"), TABLE_INDEX),
            MetricSpec("ProvisionedReadCapacityUnits", ("Average", "Maximum"), TABLE_INDEX),
            MetricSpec("ReadThrottleEvents", ("Sum",), TABLE_INDEX),
            MetricSpec("WriteThrottleEvents", ("Sum",), TABLE_INDEX),
            MetricSpec("ReturnedItemCount", ("Sum",), RETURNED),
        ),
    ),
    MetricGroup(
        "bedrock",
        "AWS/Bedrock",
        (
            MetricSpec("OutputTokenCount", ("Sum",), MODEL),
            MetricSpec("InputTokenCount", ("Sum",), MODEL),
            MetricSpec("CacheReadInputTokenCount", ("Sum",), MODEL),
            MetricSpec("CacheWriteInputTokenCount", ("Sum",), MODEL),
            MetricSpec("Invocations", ("Sum",), MODEL),
            MetricSpec("InvocationThrottles", ("Sum",), MODEL),
            MetricSpec("InvocationClientErrors", ("Sum",), MODEL),
            MetricSpec("InvocationServerErrors", ("Sum",), MODEL),
        ),
    ),
)

NOTION_METRICS = {
    "sns": [
        "NumberOfMessagesPublished",
        "PublishSize",
        "NumberOfNotificationsDelivered",
        "NumberOfNotificationsFilteredOut",
        "NumberOfNotificationsFailed",
    ],
    "lambda": [
        "Duration",
        "Invocations",
        "Errors",
        "Throttles",
        "ProvisionedConcurrencyUtilization",
        "ConcurrentExecutions",
    ],
    "elasticache": [
        "DatabaseMemoryUsagePercentage",
        "EngineCPUUtilization",
        "CurrConnections",
        "GetTypeCmds",
        "SetTypeCmds",
        "Evictions",
    ],
    "ec2": [
        "CPUUtilization",
        "NetworkIn",
        "NetworkOut",
        "EBSReadBytes",
        "EBSWriteBytes",
        "CPUCreditBalance",
        "CPUCreditUsage",
    ],
    "sqs": [
        "NumberOfMessagesSent",
        "NumberOfMessagesReceived",
        "NumberOfEmptyReceives",
        "SentMessageSize",
        "ApproximateNumberOfMessagesVisible",
        "ApproximateAgeOfOldestMessage",
    ],
    "s3": [
        "BucketSizeBytes",
        "NumberOfObjects",
        "GetRequests",
        "PutRequests",
        "BytesDownloaded",
        "BytesUploaded",
        "ListRequests",
        "DeleteRequests",
    ],
    "dynamodb": [
        "ConsumedWriteCapacityUnits",
        "ConsumedReadCapacityUnits",
        "ProvisionedWriteCapacityUnits",
        "ProvisionedReadCapacityUnits",
        "ReadThrottleEvents",
        "WriteThrottleEvents",
        "ReturnedItemCount",
    ],
    "bedrock": [
        "OutputTokenCount",
        "InputTokenCount",
        "CacheReadInputTokenCount",
        "CacheWriteInputTokenCount",
        "Invocations",
        "InvocationThrottles",
        "InvocationClientErrors",
        "InvocationServerErrors",
    ],
}


def october_window(year: int) -> tuple[datetime, datetime]:
    start = datetime(year, 10, 1, tzinfo=timezone.utc)
    end = datetime(year, 11, 1, tzinfo=timezone.utc)
    return start, end


def service_names() -> list[str]:
    names: list[str] = []
    for group in GROUPS:
        if group.service not in names:
            names.append(group.service)
    return names


def groups_for(service: str) -> list[MetricGroup]:
    return [group for group in GROUPS if group.service == service]


def select_series(group: MetricGroup, listed: list[dict]) -> tuple[list[Series], Counter]:
    """Keep listed metrics whose dimension names match a Notion dimension set."""
    specs = {spec.name: spec for spec in group.metrics}
    series: list[Series] = []
    skipped: Counter = Counter()
    for item in listed:
        spec = specs.get(item.get("MetricName", ""))
        if spec is None:
            continue
        dims = tuple((dim["Name"], dim["Value"]) for dim in item.get("Dimensions") or [])
        names = frozenset(name for name, _ in dims)
        if names not in spec.dimension_sets:
            skipped[(spec.name, tuple(sorted(names)))] += 1
            continue
        for stat in spec.stats:
            series.append(
                Series(
                    service=group.service,
                    namespace=group.namespace,
                    metric=spec.name,
                    stat=stat,
                    dimensions=dims,
                )
            )
    series.sort(key=lambda item: (item.metric, item.stat, item.dimensions))
    return series, skipped


def chunks(items: list[Series], size: int = BATCH_SIZE) -> list[list[Series]]:
    if size < 1 or size > 500:
        raise ValueError("GetMetricData accepts at most 500 queries")
    return [items[index : index + size] for index in range(0, len(items), size)]


def metric_queries(batch: list[Series]) -> tuple[list[dict], dict[str, Series]]:
    queries = []
    meta: dict[str, Series] = {}
    for index, series in enumerate(batch):
        query_id = f"m{index}"
        meta[query_id] = series
        queries.append(
            {
                "Id": query_id,
                "ReturnData": True,
                "MetricStat": {
                    "Metric": {
                        "Namespace": series.namespace,
                        "MetricName": series.metric,
                        "Dimensions": [
                            {"Name": name, "Value": value} for name, value in series.dimensions
                        ],
                    },
                    "Period": PERIOD,
                    "Stat": series.stat,
                },
            }
        )
    return queries, meta


def merge_results(pages: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    merged: dict[str, dict] = {}
    messages: list[dict] = []
    for page in pages:
        messages.extend(page.get("Messages") or [])
        for result in page.get("MetricDataResults") or []:
            slot = merged.setdefault(
                result["Id"],
                {"Timestamps": [], "Values": [], "status": "Complete"},
            )
            slot["Timestamps"].extend(result.get("Timestamps") or [])
            slot["Values"].extend(result.get("Values") or [])
            status = result.get("StatusCode")
            if status and status != "Complete":
                slot["status"] = status
    return merged, messages


def points_from_result(result: dict) -> list[dict]:
    points = []
    for stamp, value in zip(result.get("Timestamps") or [], result.get("Values") or []):
        if hasattr(stamp, "astimezone"):
            stamp = stamp.astimezone(timezone.utc)
        text = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)
        points.append({"t": text, "v": value})
    points.sort(key=lambda point: point["t"])
    return points


def grain(dimensions: tuple[tuple[str, str], ...] | dict) -> str:
    names = set(dimensions) if isinstance(dimensions, dict) else {name for name, _ in dimensions}
    if "CacheNodeId" in names:
        return "node"
    if "ReplicationGroupId" in names:
        return "replication-group"
    if "GlobalSecondaryIndexName" in names and "Source" in names:
        return "index-source"
    if "GlobalSecondaryIndexName" in names:
        return "index"
    if "Source" in names:
        return "source"
    if "Operation" in names:
        return "operation"
    if "StorageType" in names:
        return "storage-type"
    if "FilterId" in names:
        return "request-filter"
    if "Application" in names or "Platform" in names:
        return "platform-application"
    if "VolumeId" in names:
        return "volume"
    return "resource"


def row_for(series: Series, points: list[dict], region: str, start: datetime, end: datetime, status: str) -> dict:
    return {
        "service": series.service,
        "region": region,
        "namespace": series.namespace,
        "metric": series.metric,
        "stat": series.stat,
        "grain": grain(series.dimensions),
        "dimensions": {name: value for name, value in series.dimensions},
        "period_seconds": PERIOD,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "status": status,
        "points": points,
    }


def summarize(row: dict) -> dict:
    values = [point["v"] for point in row["points"]]
    stat = row["stat"]
    return {
        "service": row["service"],
        "region": row["region"],
        "namespace": row["namespace"],
        "metric": row["metric"],
        "stat": stat,
        "grain": row["grain"],
        "dimensions": row["dimensions"],
        "days": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": (sum(values) / len(values)) if values else None,
        "total": sum(values) if values and stat in SUMMABLE_STATS else None,
        "status": row["status"],
    }


def metrics_path(out_dir: Path, region: str, service: str) -> Path:
    return out_dir / f"region={region}" / f"service={service}" / "metrics.parquet"


def parquet_schema():
    import pyarrow as pa

    fields = [
        ("account", pa.string()),
        ("region", pa.string()),
        ("service", pa.string()),
        ("namespace", pa.string()),
        ("metric", pa.string()),
        ("stat", pa.string()),
        ("grain", pa.string()),
        ("timestamp", pa.timestamp("us", tz="UTC")),
        ("value", pa.float64()),
        ("period_seconds", pa.int32()),
        ("status", pa.string()),
    ]
    fields.extend((column, pa.string()) for _, column in DIMENSION_FIELDS)
    return pa.schema(fields)


def point_rows(row: dict, account: str) -> list[dict]:
    """Explode one series into one parquet row per day."""
    dims = row["dimensions"]
    unknown = set(dims) - {name for name, _ in DIMENSION_FIELDS}
    if unknown:
        raise ValueError(f"unexpected CloudWatch dimensions: {sorted(unknown)}")
    base = {
        "account": account,
        "region": row["region"],
        "service": row["service"],
        "namespace": row["namespace"],
        "metric": row["metric"],
        "stat": row["stat"],
        "grain": row["grain"],
        "period_seconds": row["period_seconds"],
        "status": row["status"],
    }
    for cloudwatch_name, column in DIMENSION_FIELDS:
        base[column] = dims.get(cloudwatch_name)
    rows = []
    for point in row["points"]:
        item = dict(base)
        item["timestamp"] = datetime.fromisoformat(point["t"])
        item["value"] = point["v"]
        rows.append(item)
    return rows


class ParquetSink:
    """Append daily rows. Writes an empty table with the schema if nothing arrives."""

    def __init__(self, path: Path):
        self.path = path
        self.count = 0
        self._writer = None
        self._schema = None

    def write(self, rows: list[dict]) -> None:
        if not rows:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq

        if self._schema is None:
            self._schema = parquet_schema()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._writer = pq.ParquetWriter(self.path, self._schema, compression="zstd")
        table = pa.Table.from_pylist(rows, schema=self._schema)
        self._writer.write_table(table)
        self.count += table.num_rows

    def close(self) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        if self._writer is not None:
            self._writer.close()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist([], schema=parquet_schema()), self.path, compression="zstd")


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def list_metric_name(client, namespace: str, metric_name: str) -> list[dict]:
    listed: list[dict] = []
    paginator = client.get_paginator("list_metrics")
    for page in paginator.paginate(Namespace=namespace, MetricName=metric_name):
        listed.extend(page.get("Metrics") or [])
    return listed


def discover_group(client, group: MetricGroup) -> tuple[list[Series], Counter]:
    listed: list[dict] = []
    names = [spec.name for spec in group.metrics]
    workers = min(8, len(names)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(list_metric_name, client, group.namespace, name): name for name in names
        }
        for future in as_completed(futures):
            found = future.result()
            listed.extend(found)
            _log(f"{group.service} {group.namespace} {futures[future]}: {len(found)} listed")
    series, skipped = select_series(group, listed)
    _log(f"{group.service} {group.namespace}: listed {len(listed)}, kept {len(series)} series")
    return series, skipped


def _pages(client, queries: list[dict], start: datetime, end: datetime) -> list[dict]:
    pages = []
    token = None
    while True:
        kwargs = {
            "MetricDataQueries": queries,
            "StartTime": start,
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        }
        if token:
            kwargs["NextToken"] = token
        page = client.get_metric_data(**kwargs)
        pages.append(page)
        token = page.get("NextToken")
        if not token:
            return pages


def fetch_batch(client, batch: list[Series], start: datetime, end: datetime) -> tuple[list[tuple[Series, list[dict], str]], list[dict]]:
    from botocore.exceptions import ClientError

    queries, meta = metric_queries(batch)
    try:
        pages = _pages(client, queries, start, end)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDenied", "AccessDeniedException", "ExpiredTokenException", "UnrecognizedClientException"}:
            raise
        if len(batch) == 1:
            return [], [{"series": batch[0].label(), "error": str(exc)}]
        mid = len(batch) // 2
        left_rows, left_errors = fetch_batch(client, batch[:mid], start, end)
        right_rows, right_errors = fetch_batch(client, batch[mid:], start, end)
        return left_rows + right_rows, left_errors + right_errors

    merged, messages = merge_results(pages)
    rows: list[tuple[Series, list[dict], str]] = []
    for query_id, series in meta.items():
        result = merged.get(query_id, {"Timestamps": [], "Values": [], "status": "Complete"})
        rows.append((series, points_from_result(result), result.get("status", "Complete")))
    errors = []
    for message in messages:
        errors.append({"code": message.get("Code"), "value": message.get("Value")})
    return rows, errors


def scrape_service(
    session,
    region: str,
    service: str,
    start: datetime,
    end: datetime,
    out_dir: Path,
    workers: int,
    discover_only: bool,
    account: str,
) -> dict:
    from botocore.config import Config

    config = Config(retries={"max_attempts": 12, "mode": "adaptive"})
    client = session.client("cloudwatch", region_name=region, config=config)
    series: list[Series] = []
    skipped: Counter = Counter()
    for group in groups_for(service):
        found, missed = discover_group(client, group)
        series.extend(found)
        skipped.update(missed)

    manifest = {
        "service": service,
        "region": region,
        "series": len(series),
        "skipped_dimension_sets": [
            {"metric": name, "dimensions": list(dims), "count": count}
            for (name, dims), count in skipped.most_common(30)
        ],
    }
    if discover_only:
        return manifest

    path = metrics_path(out_dir, region, service)
    error_path = path.parent / "errors.jsonl"
    errors: list[dict] = []
    lock = threading.Lock()
    done = 0
    written_series = 0
    local = threading.local()
    sink = ParquetSink(path)

    def thread_client():
        if not hasattr(local, "client"):
            local.client = session.client("cloudwatch", region_name=region, config=config)
        return local.client

    def run(batch: list[Series]):
        return fetch_batch(thread_client(), batch, start, end)

    batches = chunks(series)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, batch) for batch in batches]
            for future in as_completed(futures):
                rows, batch_errors = future.result()
                with lock:
                    for item, points, status in rows:
                        if not points:
                            continue
                        row = row_for(item, points, region, start, end, status)
                        sink.write(point_rows(row, account))
                        written_series += 1
                    errors.extend(batch_errors)
                    done += 1
                    _log(f"{service} {region}: batch {done}/{len(batches)}")
    finally:
        sink.close()

    if errors:
        error_path.write_text("".join(json.dumps(row) + "\n" for row in errors))
    elif error_path.exists():
        error_path.unlink()
    manifest["points_written"] = written_series
    manifest["rows_written"] = sink.count
    manifest["parquet"] = str(path)
    manifest["errors"] = len(errors)
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape October CloudWatch metrics for the dev account.")
    parser.add_argument("--profile", default="dev", help="AWS SSO profile. Default: dev")
    parser.add_argument("--region", action="append", dest="regions", help="Region to scrape. Repeatable. Default: us-east-1")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--services", default=",".join(service_names()), help="Comma-separated service keys")
    parser.add_argument("--out", type=Path, default=Path("out/cloudwatch-2026-10"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--discover-only", action="store_true", help="List matching series and exit without GetMetricData")
    args = parser.parse_args(argv)
    if not args.regions:
        args.regions = ["us-east-1"]
    args.service_list = [item.strip() for item in args.services.split(",") if item.strip()]
    unknown = sorted(set(args.service_list) - set(service_names()))
    if unknown:
        parser.error(f"unknown services: {', '.join(unknown)}")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    import boto3

    args = parse_args(argv)
    start, end = october_window(args.year)
    session = boto3.Session(profile_name=args.profile)
    identity = session.client("sts").get_caller_identity()
    _log(f"account {identity['Account']} {identity['Arn']}")
    _log(f"window {start.isoformat()} .. {end.isoformat()} (end exclusive)")
    manifests = []
    for region in args.regions:
        for service in args.service_list:
            manifests.append(
                scrape_service(
                    session,
                    region,
                    service,
                    start,
                    end,
                    args.out,
                    args.workers,
                    args.discover_only,
                    identity["Account"],
                )
            )
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "manifest.json"
    previous_services = []
    if manifest_path.exists():
        previous_services = json.loads(manifest_path.read_text()).get("services") or []
    by_key = {(item["service"], item["region"]): item for item in previous_services}
    for item in manifests:
        by_key[(item["service"], item["region"])] = item
    ordered = []
    for service in service_names():
        for region in args.regions:
            item = by_key.pop((service, region), None)
            if item:
                ordered.append(item)
    ordered.extend(by_key.values())
    manifest = {
        "account": identity["Account"],
        "arn": identity["Arn"],
        "profile": args.profile,
        "regions": args.regions,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "discover_only": args.discover_only,
        "notes": NOTES,
        "services": ordered,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    _log(f"wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
