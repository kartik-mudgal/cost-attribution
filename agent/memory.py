"""Shared long-term memory for the cost agent, stored as AgentCore Memory records.

Records are shared by every user (no actor in the namespace) and split by kind, so the
agent recalls only the kind it needs at each step instead of loading everything:

    /cost-agent/resource-map/<service>   where a resource is defined, who owns it, who calls it
    /cost-agent/queries/<service|all>    SQL that answered a kind of question
    /cost-agent/conclusions/<service>    past findings and their outcome
    /cost-agent/lessons/<request type>   what made a kind of request faster or more accurate

Records are written directly (BatchCreateMemoryRecords), not extracted from conversations,
so only facts the agent has verified are stored.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from strands import tool

# Tests point this elsewhere so they don't write into the namespaces the deployed agent reads.
ROOT = os.environ.get("SHARED_MEMORY_ROOT", "/cost-agent").rstrip("/")
SERVICES = ("sqs", "dynamodb", "ec2", "s3", "sns", "lambda", "elasticache", "bedrock")
REQUEST_TYPES = ("overview", "deep-dive", "anomaly")
SCOPES = {
    "resource-map": SERVICES,
    "queries": SERVICES + ("all",),
    "conclusions": SERVICES,
    "lessons": REQUEST_TYPES,
}
MAX_TEXT = 2000
DEFAULT_RECALL = 5
MAX_RECALL = 10
MIN_SCORE = 0.3
DUPLICATE_SCORE = 0.9

MEMORY_PROMPT = """
SHARED MEMORY
Memory saved by earlier requests, shared by all users. Use it to skip rediscovery, never as
evidence: every number still comes from a fresh query, and check a memory with one query or
one file read before relying on it. If the data disagrees, trust the data and remember the
correction.

Kinds (recall(kind, scope, query)):
- resource-map, scope = service: where a resource is defined, who owns it, which code calls it.
- queries, scope = service or all: SQL that answered a kind of question.
- conclusions, scope = service: past findings and their outcome (accepted, rejected and why,
  not an issue).
- lessons, scope = overview, deep-dive or anomaly: what made that kind of request faster or
  more accurate.

When to recall (only the kinds listed, not everything):
- Quick question: nothing.
- Overview: lessons/overview first; after ranking, conclusions for the top services.
- Deep dive: resource-map, conclusions and queries for the resource's service, before any
  query or code search.
- Anomaly: lessons/anomaly first; once the resource is known, resource-map and conclusions for
  its service.

Using what you recall (this overrides the code steps in your request type):
- A resource-map record replaces the code search. Do not call github_search_code for what it
  already covers. Read only the files it names, one github_get_file each, to confirm they still
  say the same; search only for something the record does not cover.
- A queries record replaces writing that SQL. Run it as stored, changing only dates or names.
- A conclusions record tells you what to check first and what was already accepted or
  rejected. Re-run its query to get current figures; never reuse its figures in the answer.

When to remember (one verified fact per call, with its evidence):
- resource-map: after reading the file that defines or calls the resource. Name every file and
  function you read, so the next request can read them directly.
- queries: for each query that answered a step (cost by operation, per-day trend, metric
  check), store the SQL with what it answers, unless a queries record already has it.
- conclusions: when a finding is High confidence, or the user accepts, rejects or corrects it.
  Figures may appear only with their query ID, as context.
- lessons: at most one at the end of an overview, deep dive or anomaly, if something
  non-obvious saved time or avoided a mistake.
Never remember guesses, or anything the user said that you have not checked.
"""


def namespace(kind: str, scope: str) -> str:
    """Return the namespace for a kind and scope, or raise ValueError if either is unknown."""
    if kind not in SCOPES:
        raise ValueError(f"kind must be one of {', '.join(SCOPES)}")
    scope = scope.strip().lower()
    if scope not in SCOPES[kind]:
        raise ValueError(f"scope for {kind} must be one of {', '.join(SCOPES[kind])}")
    return f"{ROOT}/{kind}/{scope}"


class SharedMemory:
    """Remember and recall shared records in one AgentCore Memory resource."""

    def __init__(self, memory_id: str, client: Any):
        self.memory_id = memory_id
        self.client = client

    def _search(self, ns: str, query: str, limit: int) -> list[dict]:
        out = self.client.retrieve_memory_records(
            memoryId=self.memory_id,
            namespace=ns,
            searchCriteria={"searchQuery": query[:1000], "topK": limit},
        )
        return out.get("memoryRecordSummaries", [])

    def recall(self, kind: str, scope: str, query: str, limit: int = DEFAULT_RECALL) -> dict:
        ns = namespace(kind, scope)
        hits = self._search(ns, query, max(1, min(limit, MAX_RECALL)))
        records = [
            {
                "id": h["memoryRecordId"],
                "text": h["content"].get("text", ""),
                "score": round(h.get("score", 0.0), 2),
            }
            for h in hits
            if h.get("score", 0.0) >= MIN_SCORE
        ]
        return {"namespace": ns, "records": records}

    def remember(self, kind: str, scope: str, fact: str, evidence: str) -> dict:
        ns = namespace(kind, scope)
        fact, evidence = fact.strip(), evidence.strip()
        if not fact or not evidence:
            raise ValueError("fact and evidence are both required")
        for hit in self._search(ns, fact, 1):
            if hit.get("score", 0.0) >= DUPLICATE_SCORE:
                return {"namespace": ns, "status": "already remembered", "id": hit["memoryRecordId"]}
        now = datetime.now(timezone.utc)
        text = f"{fact}\nEvidence: {evidence}\nSaved: {now.date().isoformat()}"[:MAX_TEXT]
        out = self.client.batch_create_memory_records(
            memoryId=self.memory_id,
            records=[{
                "requestIdentifier": uuid.uuid4().hex[:32],
                "namespaces": [ns],
                "content": {"text": text},
                "timestamp": now,
            }],
        )
        if out.get("failedRecords"):
            raise RuntimeError(f"memory write failed: {out['failedRecords'][0]}")
        return {"namespace": ns, "status": "remembered", "id": out["successfulRecords"][0]["memoryRecordId"]}


_store: SharedMemory | None = None


def configure(memory_id: str, region: str) -> list:
    """Enable shared memory and return its tools, or an empty list when memory_id is empty."""
    global _store
    if not memory_id:
        _store = None
        return []
    _store = SharedMemory(memory_id, boto3.client("bedrock-agentcore", region_name=region))
    return [recall, remember]


def _call(fn) -> str:
    if _store is None:
        return json.dumps({"error": "shared memory is not configured"})
    try:
        return json.dumps(fn())
    except ValueError as e:
        return json.dumps({"error": str(e)})


@tool
def recall(kind: str, scope: str, query: str, limit: int = DEFAULT_RECALL) -> str:
    """Search shared memory of one kind for records relevant to the query.

    Args:
        kind: resource-map, queries, conclusions or lessons.
        scope: A service (sqs, dynamodb, ec2, s3, sns, lambda, elasticache, bedrock) for
            resource-map, queries and conclusions ("all" is also allowed for queries);
            overview, deep-dive or anomaly for lessons.
        query: What you are looking for, e.g. a resource name or the question being answered.
        limit: Maximum records to return (1-10).
    """
    return _call(lambda: _store.recall(kind, scope, query, limit))


@tool
def remember(kind: str, scope: str, fact: str, evidence: str) -> str:
    """Save one verified fact to shared memory so later requests can skip rediscovering it.

    Args:
        kind: resource-map, queries, conclusions or lessons.
        scope: A service for resource-map, queries and conclusions ("all" also for queries);
            overview, deep-dive or anomaly for lessons.
        fact: One self-contained fact, naming the resource. No figures as facts.
        evidence: Where it was verified: repo/file/function, Athena query ID, or "confirmed by
            user: <reason>".
    """
    return _call(lambda: _store.remember(kind, scope, fact, evidence))
