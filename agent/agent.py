"""Cost investigator agent: Strands on Bedrock, served by AgentCore Runtime.

Payload: {"prompt": "..."} or {"case": {...}} (also accepts "message").
The entrypoint streams AG-UI SSE events (text tokens, tool calls, results)
so frontends like sample-agui-bedrock-agentcore can render the run live.
BedrockAgentCoreApp wraps each yielded dict as ``data: {...}\\n\\n``.

Session storage is mounted at WORKSPACE so files written in one invocation
are visible in later invocations of the same session.

When AGENTCORE_MEMORY_ID is set, short-term conversation turns are loaded and
saved via AgentCore Memory (AgentCoreMemorySessionManager) keyed by the
Runtime session id and actor id. No long-term strategies are configured.

GitHub access reads a PAT from AWS Secrets Manager (JSON key api_key_value).
Default secret is the AgentCore-managed github-pat vault entry; override with
GITHUB_PAT_SECRET_ARN. The token is never logged or returned to the model.

CUR data is queried through Athena (athena_query). The cur-athena skill describes
the table. One skill per service (cur-sqs, cur-dynamodb, cur-ec2, cur-s3, cur-sns,
cur-lambda, cur-elasticache, cur-bedrock) describes how to query that service.

Grafana is attached as an MCP server through the DevRev AI gateway. Its virtual key
is read from Secrets Manager (GRAFANA_MCP_SECRET_ARN); the gateway names its tools grafana-*.

User-facing artifacts are uploaded to ARTIFACT_BUCKET. publish_artifact returns a
presigned download URL for each file.
"""
import base64
import contextvars
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

import boto3
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext
from strands import Agent, AgentSkills, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from artifacts import (
    DEFAULT_BUCKET,
    DEFAULT_PREFIX,
    DEFAULT_URL_TTL_S,
    format_download_message,
    publish_workspace_file,
    safe_session_id,
)

logger = logging.getLogger(__name__)

WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/mnt/workspace"))
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-5")
# One response must fit the report tool call. Bedrock reserves this against the token quota.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "16000"))
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "").strip()
AGENTCORE_ACTOR_ID = os.environ.get("AGENTCORE_ACTOR_ID", "cost-investigator")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
GITHUB_DEFAULT_OWNER = os.environ.get("GITHUB_DEFAULT_OWNER", "devrev")
GITHUB_API = "https://api.github.com"
# AgentCore Identity stores the PAT under this managed secret (JSON: api_key_value).
GITHUB_PAT_SECRET_ARN = os.environ.get(
    "GITHUB_PAT_SECRET_ARN",
    "arn:aws:secretsmanager:us-west-2:270149200236:secret:bedrock-agentcore-identity!default/apikey/github-pat-50ea78aa-npH3Wy",
)
GITHUB_PAT_SECRET_JSON_KEY = os.environ.get("GITHUB_PAT_SECRET_JSON_KEY", "api_key_value")
# Bundled with the runtime zip next to agent.py; override with SKILLS_DIR.
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", str(Path(__file__).resolve().parent / "skills")))
ATHENA_REGION = os.environ.get("ATHENA_REGION", "us-west-2")
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "hackathon_cur")
ATHENA_TABLE = os.environ.get("ATHENA_TABLE", "cost_report")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "cost-agent")
ATHENA_TIMEOUT_S = int(os.environ.get("ATHENA_TIMEOUT_S", "120"))
ATHENA_MAX_ROWS = 500
# Empty GRAFANA_MCP_URL disables the Grafana MCP server.
GRAFANA_MCP_URL = os.environ.get("GRAFANA_MCP_URL", "https://ai-gateway.dev.devrev-eng.ai/grafana/mcp")
GRAFANA_MCP_SECRET_ARN = os.environ.get(
    "GRAFANA_MCP_SECRET_ARN",
    "arn:aws:secretsmanager:us-west-2:270149200236:secret:grafana-mcp-csUlmY",
)
GRAFANA_MCP_SECRET_JSON_KEY = os.environ.get("GRAFANA_MCP_SECRET_JSON_KEY", "x-litellm-api-key")
# Dedicated bucket for files the user can download. Not the code-zip bucket.
ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", DEFAULT_BUCKET)
ARTIFACT_PREFIX = os.environ.get("ARTIFACT_PREFIX", DEFAULT_PREFIX)
ARTIFACT_URL_TTL_S = int(os.environ.get("ARTIFACT_URL_TTL_S", str(DEFAULT_URL_TTL_S)))
_s3 = boto3.client("s3", region_name=AWS_REGION)
_artifact_session: contextvars.ContextVar[str] = contextvars.ContextVar("artifact_session", default="unknown-session")

SYSTEM_PROMPT = f"""You investigate AWS cost changes for SQS, DynamoDB, EC2 (including EBS, NAT and public IPv4), S3, SNS, Lambda, ElastiCache and Bedrock.
Use only numbers returned by tools or present in the case. Never invent figures.
Keep working notes in the workspace so a later call in the same session can continue.

Every artifact you generate for the user (report, CSV or JSON export, HTML, chart,
investigation write-up) must be stored for download. Write the file in the workspace,
then call publish_artifact with its workspace path. Include each download_url in the
answer and say that it expires after expires_in_seconds. Never invent a download URL.
Write the artifact as one short markdown file in a single write_workspace_file call,
using the same sections as the answer. A response that hits the output limit is dropped,
so the file is never saved.

Cost data is the CUR table {ATHENA_DATABASE}.{ATHENA_TABLE}, queried with athena_query.
Before writing any query, load the cur-athena skill, then the skill for the service
in scope: cur-sqs, cur-dynamodb, cur-ec2, cur-s3, cur-sns, cur-lambda, cur-elasticache
or cur-bedrock. Analyse one service at a time. October 2026 CloudWatch daily values
are in the Athena view cloudwatch_metrics. Load the cloudwatch-metrics skill before
using them. Do not invent metric values.

Skills are available via the skills tool — load github-code-lookup when you
need GitHub access. GitHub tools load a PAT from Secrets Manager for ``devrev``
org repos; never print or request the token.

Answer with: cause, evidence, fix, estimated monthly saving, confidence."""

_athena = boto3.client("athena", region_name=ATHENA_REGION)
_secrets = boto3.client("secretsmanager", region_name=AWS_REGION)
_github_pat_cache: str | None = None
_grafana_key_cache: str | None = None


def _skills_plugin() -> AgentSkills:
    """Load SKILL.md packages from the bundled skills/ dir and optional workspace."""
    sources: list[str] = []
    if SKILLS_DIR.is_dir():
        sources.append(str(SKILLS_DIR))
    workspace_skills = WORKSPACE / "skills"
    if workspace_skills.is_dir():
        sources.append(str(workspace_skills))
    return AgentSkills(skills=sources or [str(SKILLS_DIR)])

app = BedrockAgentCoreApp()


def _safe_path(name: str) -> Path:
    path = (WORKSPACE / name).resolve()
    if WORKSPACE.resolve() not in path.parents and path != WORKSPACE.resolve():
        raise ValueError("path must stay inside the workspace")
    return path


def _github_pat() -> str:
    """Fetch the GitHub PAT from Secrets Manager (cached for the process)."""
    global _github_pat_cache
    if _github_pat_cache:
        return _github_pat_cache
    resp = _secrets.get_secret_value(SecretId=GITHUB_PAT_SECRET_ARN)
    raw = resp.get("SecretString") or ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        token = payload.get(GITHUB_PAT_SECRET_JSON_KEY) or payload.get("api_key") or payload.get("token")
        if not token:
            raise RuntimeError(
                f"secret {GITHUB_PAT_SECRET_ARN!r} JSON missing key {GITHUB_PAT_SECRET_JSON_KEY!r}"
            )
    else:
        token = raw.strip()
    if not token:
        raise RuntimeError(f"empty GitHub PAT in secret {GITHUB_PAT_SECRET_ARN!r}")
    _github_pat_cache = token
    return token


def _grafana_key() -> str:
    """Return the x-litellm-api-key header value for the Grafana MCP gateway (cached for the process)."""
    global _grafana_key_cache
    if _grafana_key_cache:
        return _grafana_key_cache
    raw = _secrets.get_secret_value(SecretId=GRAFANA_MCP_SECRET_ARN).get("SecretString") or ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    key = (payload.get(GRAFANA_MCP_SECRET_JSON_KEY) if isinstance(payload, dict) else raw) or ""
    key = key.strip()
    if not key:
        raise RuntimeError(f"secret {GRAFANA_MCP_SECRET_ARN!r} has no value for {GRAFANA_MCP_SECRET_JSON_KEY!r}")
    # The gateway expects "Bearer <virtual key>"; the secret may hold either form.
    if not key.lower().startswith("bearer "):
        key = f"Bearer {key}"
    _grafana_key_cache = key
    return key


def _grafana_mcp() -> MCPClient | None:
    """Build the Grafana MCP client, or None if it is disabled or its key can't be read."""
    if not GRAFANA_MCP_URL:
        return None
    try:
        key = _grafana_key()
    except Exception as e:
        logger.warning("Grafana MCP disabled: could not read %s: %s", GRAFANA_MCP_SECRET_ARN, e)
        return None
    return MCPClient(
        url=GRAFANA_MCP_URL,
        headers={"x-litellm-api-key": key},
        continue_on_error=True,
    )


def _github_request(path: str, params: dict | None = None, accept: str = "application/vnd.github+json") -> dict | list:
    token = _github_pat()
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{GITHUB_API}{path}{query}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cost-investigator-agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500]
        raise RuntimeError(f"GitHub API {e.code} for {path}: {detail}") from e


@tool
def list_workspace() -> list[str]:
    """List files saved in the session workspace."""
    if not WORKSPACE.exists():
        return []
    return sorted(str(p.relative_to(WORKSPACE)) for p in WORKSPACE.rglob("*") if p.is_file())


@tool
def read_workspace_file(name: str) -> str:
    """Read a file from the session workspace."""
    return _safe_path(name).read_text()


@tool
def write_workspace_file(name: str, content: str) -> str:
    """Write a file to the session workspace (e.g. notes, intermediate results).

    User-facing artifacts must also be passed to publish_artifact.
    """
    path = _safe_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"wrote {len(content)} bytes to {name}"


@tool
def publish_artifact(name: str) -> str:
    """Upload a workspace file to the artifact bucket and return a presigned download URL.

    Call this for every artifact the user should keep. Include download_url in the answer.
    The link expires after expires_in_seconds (sooner if the signing credentials expire).

    Args:
        name: Workspace-relative path, for example artifacts/sqs-report.md.
    """
    try:
        result = publish_workspace_file(
            workspace=WORKSPACE,
            name=name,
            session_id=_artifact_session.get(),
            s3_client=_s3,
            bucket=ARTIFACT_BUCKET,
            prefix=ARTIFACT_PREFIX,
            ttl_s=ARTIFACT_URL_TTL_S,
        )
    except (ValueError, FileNotFoundError, OSError) as e:
        return json.dumps({"error": str(e)})
    return json.dumps(result)


@tool
def github_search_code(query: str, owner: str = "", per_page: int = 10) -> str:
    """Search GitHub code. Use for finding Terraform/Helm/app references to a table or queue name.

    Args:
        query: GitHub code search query (e.g. table name, 'resource "aws_dynamodb_table"').
        owner: Org or user to scope the search (default: GITHUB_DEFAULT_OWNER / devrev).
        per_page: Max results (1-30).
    """
    org = owner or GITHUB_DEFAULT_OWNER
    q = f"{query} org:{org}"
    data = _github_request(
        "/search/code",
        {"q": q, "per_page": max(1, min(per_page, 30))},
        accept="application/vnd.github.text-match+json",
    )
    items = []
    for hit in data.get("items", []):
        items.append({
            "repo": hit.get("repository", {}).get("full_name"),
            "path": hit.get("path"),
            "url": hit.get("html_url"),
            "matches": [m.get("fragment") for m in hit.get("text_matches", []) if m.get("fragment")],
        })
    return json.dumps({"total_count": data.get("total_count", 0), "items": items}, indent=2)


@tool
def github_get_file(owner: str, repo: str, path: str, ref: str = "") -> str:
    """Read a file from a GitHub repository (decoded text).

    Args:
        owner: Repository owner (org or user).
        repo: Repository name.
        path: File path in the repo.
        ref: Branch, tag, or commit SHA (optional).
    """
    params = {"ref": ref} if ref else None
    data = _github_request(f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}", params)
    if isinstance(data, list):
        return json.dumps({"error": "path is a directory", "entries": [e.get("path") for e in data[:50]]}, indent=2)
    if data.get("encoding") == "base64" and data.get("content"):
        text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        # Cap very large files so the model context stays usable.
        if len(text) > 80_000:
            text = text[:80_000] + "\n...[truncated]..."
        return json.dumps({
            "repo": f"{owner}/{repo}",
            "path": data.get("path"),
            "sha": data.get("sha"),
            "html_url": data.get("html_url"),
            "content": text,
        }, indent=2)
    return json.dumps(data, indent=2)[:80_000]


@tool
def github_search_commits(query: str, owner: str = "", per_page: int = 10) -> str:
    """Search GitHub commits (message / author). Useful for changes around a cost window.

    Args:
        query: Commit search query (e.g. 'queue-name author-date:2026-09-01..2026-10-10').
        owner: Org or user to scope the search (default: GITHUB_DEFAULT_OWNER / devrev).
        per_page: Max results (1-30).
    """
    org = owner or GITHUB_DEFAULT_OWNER
    q = f"{query} org:{org}"
    data = _github_request(
        "/search/commits",
        {"q": q, "per_page": max(1, min(per_page, 30))},
        accept="application/vnd.github+json",
    )
    items = []
    for hit in data.get("items", []):
        commit = hit.get("commit", {})
        items.append({
            "repo": hit.get("repository", {}).get("full_name"),
            "sha": hit.get("sha"),
            "message": (commit.get("message") or "").split("\n", 1)[0],
            "author": (commit.get("author") or {}).get("name"),
            "date": (commit.get("author") or {}).get("date"),
            "url": hit.get("html_url"),
        })
    return json.dumps({"total_count": data.get("total_count", 0), "items": items}, indent=2)


@tool
def github_list_pull_requests(owner: str, repo: str, state: str = "closed", per_page: int = 10) -> str:
    """List recent pull requests for a repository (merged/closed by default).

    Args:
        owner: Repository owner.
        repo: Repository name.
        state: open, closed, or all.
        per_page: Max results (1-30).
    """
    data = _github_request(
        f"/repos/{owner}/{repo}/pulls",
        {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": max(1, min(per_page, 30)),
        },
    )
    items = [{
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "merged_at": pr.get("merged_at"),
        "updated_at": pr.get("updated_at"),
        "user": (pr.get("user") or {}).get("login"),
        "url": pr.get("html_url"),
    } for pr in data]
    return json.dumps(items, indent=2)


def _check_sql(sql: str) -> str:
    """Return the SQL to run, or raise ValueError if it isn't a single read-only query."""
    body = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    body = re.sub(r"--[^\n]*", " ", body).strip().rstrip(";").strip()
    if ";" in body:
        raise ValueError("send one statement at a time")
    keyword = body.split(None, 1)[0].lower() if body else ""
    if keyword not in ("select", "with", "describe", "show"):
        raise ValueError("only SELECT, WITH, DESCRIBE and SHOW are allowed")
    if keyword in ("select", "with") and "billing_period" not in body.lower():
        raise ValueError("filter on billing_period (e.g. billing_period = '2026-10') so the query reads only the months it needs")
    return body


@tool
def athena_query(sql: str, max_rows: int = 200) -> str:
    """Run a read-only SQL query on the CUR table in Athena and return the rows as JSON.

    Load the cur-athena skill before using this. Aggregate in SQL (GROUP BY, ORDER BY ... LIMIT);
    raw line items will not fit in the result.

    Args:
        sql: One SELECT, WITH, DESCRIBE or SHOW statement (Athena engine v3 / Trino SQL).
            SELECT and WITH queries must filter on billing_period.
        max_rows: Maximum rows to return (1-500).
    """
    try:
        query = _check_sql(sql)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    limit = max(1, min(max_rows, ATHENA_MAX_ROWS))

    qid = _athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        WorkGroup=ATHENA_WORKGROUP,
    )["QueryExecutionId"]
    deadline = time.monotonic() + ATHENA_TIMEOUT_S
    while True:
        execution = _athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = execution["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.monotonic() > deadline:
            _athena.stop_query_execution(QueryExecutionId=qid)
            return json.dumps({"query_execution_id": qid, "error": f"timed out after {ATHENA_TIMEOUT_S}s; narrow the dates or aggregate more"})
        time.sleep(1)

    scanned_mb = round(execution.get("Statistics", {}).get("DataScannedInBytes", 0) / 1e6, 1)
    if state != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", state)
        return json.dumps({"query_execution_id": qid, "error": reason, "data_scanned_mb": scanned_mb})

    columns: list[str] = []
    rows: list[list[str | None]] = []
    truncated = False
    for page in _athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        if not columns:
            columns = [c["Name"] for c in page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
        for row in page["ResultSet"]["Rows"]:
            values = [d.get("VarCharValue") for d in row["Data"]]
            if not rows and values == columns:
                continue  # SELECT results repeat the header as the first row
            if len(rows) == limit:
                truncated = True
                break
            rows.append(values)
        if truncated:
            break

    result = {
        "query_execution_id": qid,
        "data_scanned_mb": scanned_mb,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }
    if truncated:
        result["note"] = (f"Result cut off at {limit} rows. Aggregate further (GROUP BY, coarser time) "
                          "or rank with ORDER BY ... LIMIT instead of reading more rows.")
    return json.dumps(result)


# Still to add: get_cloudwatch, describe_resource.
TOOLS = [
    list_workspace,
    read_workspace_file,
    write_workspace_file,
    publish_artifact,
    athena_query,
    github_search_code,
    github_get_file,
    github_search_commits,
    github_list_pull_requests,
]


def _agui_event(event_type: str, **data: Any) -> dict:
    """Build one AG-UI event dict (AgentCore wraps it as SSE)."""
    return {
        "type": event_type,
        "eventId": str(uuid.uuid4()),
        "timestamp": time.time(),
        **data,
    }


def _extract_tool_results(message_obj: Any) -> list[tuple[str, Any]]:
    """Pull toolResult blocks from a Strands history message event."""
    if not isinstance(message_obj, dict):
        return []
    if message_obj.get("role") != "user":
        return []
    out: list[tuple[str, Any]] = []
    for block in message_obj.get("content", []) or []:
        if not isinstance(block, dict) or "toolResult" not in block:
            continue
        tr = block["toolResult"]
        content = tr.get("content", [])
        if (
            isinstance(content, list)
            and content
            and isinstance(content[0], dict)
            and "text" in content[0]
        ):
            txt = content[0]["text"]
        else:
            txt = str(content)
        try:
            parsed: Any = json.loads(txt)
        except Exception:
            parsed = {"result": txt}
        out.append((tr.get("toolUseId", ""), parsed))
    return out


def _prompt_from_payload(payload: dict) -> str:
    if "case" in payload:
        return "Investigate this case:\n" + json.dumps(payload["case"])
    return payload.get(
        "prompt",
        payload.get("message", "List the workspace and say what is there."),
    )


def _actor_id(payload: dict) -> str:
    actor = payload.get("actor_id") or payload.get("user_id") or AGENTCORE_ACTOR_ID
    return str(actor) if actor else "cost-investigator"


def _session_id(payload: dict, context: RequestContext | None) -> str:
    if context and context.session_id and len(context.session_id) >= 33:
        return context.session_id
    sid = payload.get("session_id") or payload.get("sessionId") or ""
    if isinstance(sid, str) and len(sid) >= 33:
        return sid
    return f"session-{uuid.uuid4().hex}"


def _memory_session_manager(
    session_id: str, actor_id: str
) -> AgentCoreMemorySessionManager | None:
    """Build STM session manager when AGENTCORE_MEMORY_ID is configured."""
    if not AGENTCORE_MEMORY_ID:
        return None
    config = AgentCoreMemoryConfig(
        memory_id=AGENTCORE_MEMORY_ID,
        session_id=session_id,
        actor_id=actor_id,
        batch_size=1,
    )
    return AgentCoreMemorySessionManager(config, region_name=AWS_REGION)


def _bind_artifact_session(session_id: str) -> None:
    """Scope artifact object keys to this Runtime session."""
    _artifact_session.set(safe_session_id(session_id))


def _text_events(text: str) -> list[dict]:
    message_id = str(uuid.uuid4())
    return [
        _agui_event("TEXT_MESSAGE_START", messageId=message_id, role="assistant"),
        _agui_event("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=text),
        _agui_event("TEXT_MESSAGE_END", messageId=message_id),
    ]


def _collect_download(parsed: Any, downloads: list[dict]) -> None:
    if isinstance(parsed, dict) and parsed.get("download_url"):
        downloads.append(parsed)


def _model() -> BedrockModel:
    return BedrockModel(
        model_id=MODEL_ID,
        max_tokens=MAX_OUTPUT_TOKENS,
        region_name=AWS_REGION,
    )


def _build_agent(session_manager: AgentCoreMemorySessionManager | None = None) -> Agent:
    grafana = _grafana_mcp()
    kwargs: dict[str, Any] = {
        "model": _model(),
        "system_prompt": SYSTEM_PROMPT,
        "tools": TOOLS + ([grafana] if grafana else []),
        "plugins": [_skills_plugin()],
    }
    if session_manager is not None:
        kwargs["session_manager"] = session_manager
    return Agent(**kwargs)


@app.entrypoint
async def invoke(payload: dict, context: RequestContext) -> AsyncIterator[dict]:
    """Stream AG-UI events for the cost investigator run."""
    prompt = _prompt_from_payload(payload)
    session_id = _session_id(payload, context)
    actor_id = _actor_id(payload)
    run_id = str(uuid.uuid4())
    _bind_artifact_session(session_id)
    session_manager = _memory_session_manager(session_id, actor_id)
    agent = _build_agent(session_manager=session_manager)

    yield _agui_event("RUN_STARTED", runId=run_id)
    yield _agui_event(
        "STATE_SNAPSHOT",
        snapshot={
            "status": "processing",
            "tools_invoked": [],
            "workspace_files": list_workspace(),
            "session_id": session_id,
            "actor_id": actor_id,
            "memory_enabled": bool(session_manager),
        },
    )

    active_text_msg_id: str | None = None
    active_tool_call_id: str | None = None
    seen_tool_args: dict[str, str] = {}
    tools_invoked: list[str] = []
    downloads: list[dict] = []

    try:
        async for event in agent.stream_async(prompt):
            if event.get("type") == "tool_use_stream":
                tu = event.get("current_tool_use") or {}
                tu_id = tu.get("toolUseId")
                tu_name = tu.get("name", "")
                tu_input = tu.get("input", "")
                if not tu_id:
                    continue

                if active_text_msg_id is not None:
                    yield _agui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)
                    active_text_msg_id = None

                if active_tool_call_id != tu_id:
                    if active_tool_call_id is not None:
                        yield _agui_event("TOOL_CALL_END", toolCallId=active_tool_call_id)
                    active_tool_call_id = tu_id
                    tools_invoked.append(tu_name)
                    yield _agui_event("TOOL_CALL_START", toolCallId=tu_id, toolName=tu_name)

                cumulative = tu_input if isinstance(tu_input, str) else json.dumps(tu_input)
                prev = seen_tool_args.get(tu_id, "")
                if cumulative.startswith(prev) and len(cumulative) > len(prev):
                    delta = cumulative[len(prev) :]
                    yield _agui_event("TOOL_CALL_ARGS", toolCallId=tu_id, delta=delta)
                    seen_tool_args[tu_id] = cumulative
                elif cumulative != prev:
                    yield _agui_event("TOOL_CALL_ARGS", toolCallId=tu_id, delta=cumulative)
                    seen_tool_args[tu_id] = cumulative
                continue

            if "data" in event and isinstance(event["data"], str) and event["data"]:
                if active_tool_call_id is not None:
                    yield _agui_event("TOOL_CALL_END", toolCallId=active_tool_call_id)
                    active_tool_call_id = None

                if active_text_msg_id is None:
                    active_text_msg_id = str(uuid.uuid4())
                    yield _agui_event(
                        "TEXT_MESSAGE_START",
                        messageId=active_text_msg_id,
                        role="assistant",
                    )
                yield _agui_event(
                    "TEXT_MESSAGE_CONTENT",
                    messageId=active_text_msg_id,
                    delta=event["data"],
                )
                continue

            if "message" in event:
                msg = event["message"]
                if isinstance(msg, str):
                    try:
                        msg = json.loads(msg.replace("'", '"'))
                    except Exception:
                        msg = None
                for tu_id, parsed in _extract_tool_results(msg):
                    if active_tool_call_id == tu_id:
                        yield _agui_event("TOOL_CALL_END", toolCallId=tu_id)
                        active_tool_call_id = None
                    _collect_download(parsed, downloads)
                    yield _agui_event("TOOL_CALL_RESULT", toolCallId=tu_id, result=parsed)

        if active_tool_call_id is not None:
            yield _agui_event("TOOL_CALL_END", toolCallId=active_tool_call_id)
        if active_text_msg_id is not None:
            yield _agui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)

        download_text = format_download_message(downloads)
        if download_text:
            for event in _text_events(download_text):
                yield event

        yield _agui_event(
            "STATE_DELTA",
            delta={
                "status": "idle",
                "tools_invoked": tools_invoked,
                "workspace_files": list_workspace(),
            },
        )
        yield _agui_event("RUN_FINISHED", runId=run_id)
    except Exception as e:
        if active_tool_call_id is not None:
            yield _agui_event("TOOL_CALL_END", toolCallId=active_tool_call_id)
        if active_text_msg_id is not None:
            yield _agui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)
        download_text = format_download_message(downloads)
        if download_text:
            for event in _text_events(download_text):
                yield event
        yield _agui_event(
            "RUN_ERROR",
            runId=run_id,
            error={"code": "AGENT_ERROR", "message": str(e)},
        )
    finally:
        if session_manager is not None:
            session_manager.close()
        agent.cleanup()


if __name__ == "__main__":
    app.run()
