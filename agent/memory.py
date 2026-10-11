"""Shared long-term memory for the cost agent, stored as AgentCore Memory records.

Records are shared by every user (no actor in the namespace) and split by kind, so the
agent recalls only the kind it needs at each step instead of loading everything:

    /cost-agent/resource-map/<service>   where a resource is defined, who owns it, who calls it
    /cost-agent/queries/<service|all>    SQL that answered a kind of question
    /cost-agent/conclusions/<service>    past findings and their outcome
    /cost-agent/lessons/<request type>   what made a kind of request faster or more accurate

Records are written directly (BatchCreateMemoryRecords), not extracted from conversations,
so only facts the agent has verified are stored.

Staleness: each record carries the date it was last verified and, for resource maps, the
GitHub blob sha of each file it relies on. Recall reports every record as fresh, expired
(older than its kind's limit: re-verify fully) or disputed, and deletes records older than
twice the limit. Confirming a record refreshes its date; corrections replace it.
"""
import json
import os
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError, ParamValidationError
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
# Days after which a record must be re-verified fully. Records older than twice this are deleted.
MAX_AGE_DAYS = {"conclusions": 14, "resource-map": 30, "lessons": 60, "queries": 90}
MAX_TEXT = 2000
DEFAULT_RECALL = 5
MAX_RECALL = 10
MIN_SCORE = 0.3
DUPLICATE_SCORE = 0.9

_VERIFIED = re.compile(r"^Verified: (\d{4}-\d{2}-\d{2})$", re.M)
_STATUS = re.compile(r"^Status: (.*)$", re.M)
_FOOTER = re.compile(r"\n(Evidence|Files|Status|Verified|Saved): .*", re.S)

MEMORY_PROMPT = """
SHARED MEMORY
Memory saved by earlier requests, shared by all users. Use it to skip rediscovery, never as
evidence: every number still comes from a fresh query. If the data disagrees with a memory,
trust the data and replace the memory. The memory tools (remember, dispute, forget) are the one
exception to the AWS read-only rule: they only change these memory records.

Kinds (recall(kind, scope, query)):
- resource-map, scope = service: where a resource is defined, who owns it, which code calls it.
- queries, scope = service or all: SQL that answered a kind of question.
- conclusions, scope = service: past findings and their outcome (accepted, rejected and why,
  fixed, not an issue).
- lessons, scope = overview, deep-dive or anomaly: what made that kind of request faster or
  more accurate.

When to recall (only the kinds listed, not everything):
- Quick question: nothing.
- Overview: lessons/overview first; after ranking, conclusions for the top services.
- Deep dive: resource-map, conclusions and queries for the resource's service, before any
  query or code search.
- Anomaly: lessons/anomaly first; once the resource is known, resource-map and conclusions for
  its service.

Each recalled record has a status:
- fresh: use it as below.
- expired: too old to trust. Redo that step fully (search, query), then remember the result
  with replaces=<id>.
- disputed: someone said it may be wrong. Redo that step fully, then replace or forget it.

Using a fresh record (this overrides the code steps in your request type):
- resource-map replaces the code search. Read only the files it names, one github_get_file
  each, and compare each file's sha with the one in the record. Same sha: the record holds.
  Different sha: read the relevant function again and replace the record. File missing: the
  code moved; search again and replace the record. Do not call github_search_code for what a
  holding record covers.
- queries replaces writing that SQL. Run it as stored, changing only dates or names.
- conclusions tells you what to check first and what was already accepted, rejected or fixed.
  Re-run its query for current figures; never reuse its figures. If a fix was recorded but the
  data still shows the problem, say so.
- After a record holds, call remember with replaces=<id> and the same fact to refresh its
  verified date.

When to remember (one verified fact per call, with its evidence):
- resource-map: after reading the file that defines or calls the resource. Name every file and
  function, and pass files as "owner/repo path@sha" using the sha github_get_file returned.
- queries: for each query that answered a step (cost by operation, per-day trend, metric
  check), store the SQL with what it answers, unless a queries record already has it.
- conclusions: when a finding is High confidence. Figures may appear only with their query ID,
  as context.
- lessons: at most one at the end of an overview, deep dive or anomaly, if something
  non-obvious saved time or avoided a mistake.
Never remember guesses.

When the user corrects or updates something:
- A decision only they know (accepted, rejected and why, fixed in a PR): remember it as a
  conclusion with evidence "confirmed by user: <what they said>", replacing the old conclusion.
- A claim about code or data: check it (read the file, run the query). If it holds, replace or
  forget the record. If you cannot check it now, dispute the record with their reason; never
  store an unchecked claim as a fact.
"""


def namespace(kind: str, scope: str) -> str:
    """Return the namespace for a kind and scope, or raise ValueError if either is unknown."""
    if kind not in SCOPES:
        raise ValueError(f"kind must be one of {', '.join(SCOPES)}")
    scope = scope.strip().lower()
    if scope not in SCOPES[kind]:
        raise ValueError(f"scope for {kind} must be one of {', '.join(SCOPES[kind])}")
    return f"{ROOT}/{kind}/{scope}"


def compose(fact: str, evidence: str, files: str = "", status: str = "verified", verified: date | None = None) -> str:
    """Build a record's text: the fact, then one footer line per field."""
    lines = [fact.strip(), f"Evidence: {evidence.strip()}"]
    if files.strip():
        lines.append(f"Files: {files.strip()}")
    lines.append(f"Status: {status}")
    lines.append(f"Verified: {(verified or datetime.now(timezone.utc).date()).isoformat()}")
    return "\n".join(lines)[:MAX_TEXT]


def verified_on(text: str, fallback: datetime | None = None) -> date | None:
    match = _VERIFIED.search(text)
    if match:
        return date.fromisoformat(match.group(1))
    return fallback.date() if fallback else None


def assess(kind: str, text: str, created: datetime | None, today: date) -> tuple[str, int | None]:
    """Return (fresh | expired | disputed | delete, age in days) for a record."""
    when = verified_on(text, created)
    age = (today - when).days if when else None
    limit = MAX_AGE_DAYS[kind]
    if age is not None and age > 2 * limit:
        return "delete", age
    status = _STATUS.search(text)
    if status and status.group(1).startswith("disputed"):
        return "disputed", age
    if age is None or age > limit:
        return "expired", age
    return "fresh", age


class SharedMemory:
    """Remember, recall, dispute and forget shared records in one AgentCore Memory resource."""

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

    def _owned(self, record_id: str) -> dict:
        """Fetch a record and check it belongs to this agent's shared namespaces."""
        record = self.client.get_memory_record(memoryId=self.memory_id, memoryRecordId=record_id)["memoryRecord"]
        if not any(ns.rstrip("/").startswith(ROOT + "/") for ns in record.get("namespaces", [])):
            raise ValueError(f"record {record_id} is not in shared memory")
        return record

    def _update(self, record_id: str, ns: str, text: str) -> None:
        out = self.client.batch_update_memory_records(
            memoryId=self.memory_id,
            records=[{
                "memoryRecordId": record_id,
                "namespaces": [ns],
                "content": {"text": text},
                "timestamp": datetime.now(timezone.utc),
            }],
        )
        if out.get("failedRecords"):
            raise RuntimeError(f"memory update failed: {out['failedRecords'][0]}")

    def _delete(self, record_ids: list[str]) -> None:
        if record_ids:
            self.client.batch_delete_memory_records(
                memoryId=self.memory_id, records=[{"memoryRecordId": rid} for rid in record_ids]
            )

    def recall(self, kind: str, scope: str, query: str, limit: int = DEFAULT_RECALL) -> dict:
        ns = namespace(kind, scope)
        today = datetime.now(timezone.utc).date()
        records, stale = [], []
        for h in self._search(ns, query, max(1, min(limit, MAX_RECALL))):
            if h.get("score", 0.0) < MIN_SCORE:
                continue
            text = h["content"].get("text", "")
            status, age = assess(kind, text, h.get("createdAt"), today)
            if status == "delete":
                stale.append(h["memoryRecordId"])
                continue
            records.append({
                "id": h["memoryRecordId"],
                "status": status,
                "age_days": age,
                "score": round(h.get("score", 0.0), 2),
                "text": text,
            })
        self._delete(stale)
        return {"namespace": ns, "records": records, "deleted_stale": len(stale)}

    def remember(self, kind: str, scope: str, fact: str, evidence: str, files: str = "", replaces: str = "") -> dict:
        ns = namespace(kind, scope)
        fact, evidence = fact.strip(), evidence.strip()
        if not fact or not evidence:
            raise ValueError("fact and evidence are both required")
        text = compose(fact, evidence, files)
        if replaces:
            self._owned(replaces)
            self._update(replaces, ns, text)
            return {"namespace": ns, "status": "replaced", "id": replaces}
        for hit in self._search(ns, fact, 1):
            if hit.get("score", 0.0) >= DUPLICATE_SCORE:
                self._update(hit["memoryRecordId"], ns, text)
                return {"namespace": ns, "status": "refreshed existing record", "id": hit["memoryRecordId"]}
        out = self.client.batch_create_memory_records(
            memoryId=self.memory_id,
            records=[{
                "requestIdentifier": uuid.uuid4().hex[:32],
                "namespaces": [ns],
                "content": {"text": text},
                "timestamp": datetime.now(timezone.utc),
            }],
        )
        if out.get("failedRecords"):
            raise RuntimeError(f"memory write failed: {out['failedRecords'][0]}")
        return {"namespace": ns, "status": "remembered", "id": out["successfulRecords"][0]["memoryRecordId"]}

    def dispute(self, record_id: str, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("reason is required")
        record = self._owned(record_id)
        text = record["content"].get("text", "")
        fact = _FOOTER.sub("", text).strip()
        evidence = re.search(r"^Evidence: (.*)$", text, re.M)
        files = re.search(r"^Files: (.*)$", text, re.M)
        updated = compose(
            fact,
            evidence.group(1) if evidence else "unknown",
            files.group(1) if files else "",
            status=f"disputed: {reason.strip()}",
            verified=verified_on(text, record.get("createdAt")),
        )
        ns = record["namespaces"][0].rstrip("/")
        self._update(record_id, ns, updated)
        return {"status": "disputed", "id": record_id}

    def forget(self, record_id: str, reason: str) -> dict:
        if not reason.strip():
            raise ValueError("reason is required")
        self._owned(record_id)
        self._delete([record_id])
        return {"status": "forgotten", "id": record_id, "reason": reason.strip()}


_store: SharedMemory | None = None


def configure(memory_id: str, region: str) -> list:
    """Enable shared memory and return its tools, or an empty list when memory_id is empty."""
    global _store
    if not memory_id:
        _store = None
        return []
    _store = SharedMemory(memory_id, boto3.client("bedrock-agentcore", region_name=region))
    return [recall, remember, dispute, forget]


def _call(fn) -> str:
    if _store is None:
        return json.dumps({"error": "shared memory is not configured"})
    try:
        return json.dumps(fn())
    except (ValueError, ClientError, ParamValidationError) as e:
        return json.dumps({"error": str(e)})


@tool
def recall(kind: str, scope: str, query: str, limit: int = DEFAULT_RECALL) -> str:
    """Search shared memory of one kind. Each record comes back fresh, expired or disputed.

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
def remember(kind: str, scope: str, fact: str, evidence: str, files: str = "", replaces: str = "") -> str:
    """Save one verified fact to shared memory, or replace or refresh an existing record.

    Args:
        kind: resource-map, queries, conclusions or lessons.
        scope: A service for resource-map, queries and conclusions ("all" also for queries);
            overview, deep-dive or anomaly for lessons.
        fact: One self-contained fact, naming the resource.
        evidence: Where it was verified: repo/file/function, Athena query ID, or "confirmed by
            user: <reason>".
        files: For resource-map, the files relied on as "owner/repo path@sha", comma-separated,
            using the sha github_get_file returned.
        replaces: ID of the record this replaces (correction, re-verification, or refreshing a
            record that still holds).
    """
    return _call(lambda: _store.remember(kind, scope, fact, evidence, files, replaces))


@tool
def dispute(record_id: str, reason: str) -> str:
    """Mark a shared memory record as possibly wrong when you cannot check a correction now.

    Args:
        record_id: ID returned by recall.
        reason: Why it may be wrong, e.g. what the user said.
    """
    return _call(lambda: _store.dispute(record_id, reason))


@tool
def forget(record_id: str, reason: str) -> str:
    """Delete a shared memory record that is wrong or no longer applies.

    Args:
        record_id: ID returned by recall.
        reason: Why it is being deleted.
    """
    return _call(lambda: _store.forget(record_id, reason))
