"""Cost investigator agent: Strands on Bedrock, served by AgentCore Runtime.

Payload: {"prompt": "..."} or {"case": {...}} (also accepts "message").
The entrypoint streams AG-UI SSE events (text tokens, tool calls, results)
so frontends like sample-agui-bedrock-agentcore can render the run live.
BedrockAgentCoreApp wraps each yielded dict as ``data: {...}\\n\\n``.

Session storage is mounted at WORKSPACE so files written in one invocation
are visible in later invocations of the same session.

When AGENTCORE_MEMORY_ID is set, short-term conversation turns are loaded and
saved via AgentCore Memory (AgentCoreMemorySessionManager) keyed by the
Runtime session id and actor id. The same memory resource also holds shared
long-term records (memory.py: recall and remember tools), shared by all users.

GitHub access reads a PAT from AWS Secrets Manager (JSON key api_key_value).
Default secret is the AgentCore-managed github-pat vault entry; override with
GITHUB_PAT_SECRET_ARN. The token is never logged or returned to the model.

CUR data is queried through Athena (athena_query). The cur-athena skill describes
the table. One skill per service (cur-sqs, cur-dynamodb, cur-ec2, cur-s3, cur-sns,
cur-lambda, cur-elasticache, cur-bedrock) describes how to query that service.

Grafana can be attached as an MCP server through the DevRev AI gateway by setting
GRAFANA_MCP_URL (off by default). Its virtual key is read from Secrets Manager
(GRAFANA_MCP_SECRET_ARN); the gateway names its tools grafana-*.

User-facing artifacts are uploaded to ARTIFACT_BUCKET. publish_artifact returns a
presigned download URL for each file.

With github-fix-pr, the agent may clone devrev repos under workspace/repos/, edit files with
edit_workspace_file, and open fix PRs via github_clone_repo and github_create_fix_pr (git on PATH;
GitHub auth uses the PAT from Secrets Manager).

Async runs (payload ``{"async": true, "webhook_url": "https://..."}``) return
immediately; the investigation continues in a background thread (AgentCore async
task). When finished the agent must call post_async_webhook, which POSTs JSON
``{"session_id": "...", "body": "<full report md>"}`` to the caller webhook_url.
"""
import asyncio
import base64
import contextvars
import json
import logging
import os
import re
import subprocess
import threading
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
from strands.types.exceptions import MaxTokensReachedException

from artifacts import (
    DEFAULT_BUCKET,
    DEFAULT_PREFIX,
    DEFAULT_URL_TTL_S,
    format_download_message,
    publish_workspace_file,
    safe_session_id,
)
from github_fix import clone_repository, create_fix_pull_request
from memory import MEMORY_PROMPT
from memory import configure as configure_shared_memory

logger = logging.getLogger(__name__)

_AGENT_DIR = Path(__file__).resolve().parent
_BUNDLED_BIN = _AGENT_DIR / "bin"
if _BUNDLED_BIN.is_dir():
    os.environ["PATH"] = f"{_BUNDLED_BIN}{os.pathsep}{os.environ.get('PATH', '')}"

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
GRAFANA_MCP_URL = os.environ.get("GRAFANA_MCP_URL", "")
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
_artifact_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "artifact_session", default="unknown-session"
)
# Set per-invocation from payload.webhook_url (required for async runs).
_report_webhook_url: contextvars.ContextVar[str] = contextvars.ContextVar(
    "report_webhook_url", default=""
)
_async_webhook_enabled: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "async_webhook_enabled", default=False
)
# Original Runtime session id for webhook JSON (not S3-safe-normalized).
_report_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "report_session_id", default=""
)

SYSTEM_PROMPT = f"""You analyse AWS cost for DevRev's dev account (020040093233) and recommend fixes. Services
covered: SQS, DynamoDB, EC2 (including EBS, NAT and public IPv4), S3, SNS, Lambda,
ElastiCache and Bedrock.

RULES FOR EVERY REQUEST
- Use only numbers returned by tools or present in the case. Never invent figures, metric
  values, file paths, function names or URLs.
- Every number you report comes from a query; cite its Athena query ID. Every code claim
  cites repo, file and function.
- AWS is read-only. Never create, change or delete anything in AWS.
- GitHub writes are allowed only when you load github-fix-pr and the user asked for a code fix
  or explicitly approved opening a PR; use github_clone_repo and github_create_fix_pr only then.
- Keep working notes in the workspace so a later call in the same session can continue.

DATA
- Cost: the CUR table {ATHENA_DATABASE}.{ATHENA_TABLE}, via athena_query. Load the cur-athena
  skill before any cost query, then the skill for the service in scope: cur-sqs, cur-dynamodb,
  cur-ec2, cur-s3, cur-sns, cur-lambda, cur-elasticache or cur-bedrock.
- Metrics: the Athena view cloudwatch_metrics (October 2026, daily values). Load the
  cloudwatch-metrics skill before using it. It is the only source of metric values.
- Code: GitHub tools for devrev org repos. Load github-code-lookup before using them. They
  load a PAT from Secrets Manager; never print or request the token.
- Deploys: devrev/marina image.tag changes per env. Load marina-deployments when correlating
  a cost or metric change to a deploy (anomalies, deep dives); use github_list_commits on the
  service env values.yaml path.
- Fix PRs: load github-fix-pr to clone a repo, edit under workspace/repos/, then open a PR with
  github_create_fix_pr (branch must be cost-fix/<slug>).
- Limits, to state whenever they affect a conclusion: CUR covers 2026-10-01 to 2026-10-10 and
  the last day is partial, so the newest settled day is 2026-10-08. There is no earlier
  month, so baselines are at most 7 days. Metrics are daily, so a cost jump can be timed to
  the hour from CUR but confirmed in metrics only for the day.

WHAT KIND OF REQUEST IS THIS?
Decide first, then follow only that type's steps.

1. Quick question (e.g. "what did queue X cost on Oct 8"): answer with the fewest queries.
   No investigation, no code search.

2. Overview (all resources, or one service): rank services by cost and by change in one
   query across services, then for the top services load their skill and run its waste
   checks. Investigate only the top 3 findings by dollar impact in depth (metrics, then
   code); list the rest with their numbers. If a resource shows a sudden increase, handle it
   as an anomaly.

3. Deep dive (one named resource): cost by usage type and operation per day; the matching
   metrics; then code: where the resource is defined (Terraform, Helm values) and which
   service calls it and how. Name the owning service if the code shows it.

4. Anomaly ("why did cost jump", or "what changed"): first confirm an increase: over 30%
   and over $5/day against the baseline, increases only, settled days only. If nothing
   passes, answer "No anomaly", say what you checked, and stop. If confirmed: find which
   cost line moved, whether volume or cost per unit moved, and the hour it started. Then
   check metrics for a traffic change on that day, then load marina-deployments and list marina
   image.tag deploys for the owning service in that env shortly before the jump (then app-repo
   commits at the new tag if needed).

Use at most about 50 tool calls. If you need more, report what you have and say what is
left.

CONFIDENCE AND SAVINGS
- High: cause confirmed in production code or in metrics. Medium: CUR plus a metrics
  pattern, cause not seen in code. Low: CUR only.
- Monthly saving = settled daily cost removed x 30. State the assumption, e.g. "if
  GetQueueUrl drops to near zero". Give each saving as a share of the service's monthly
  spend.
- Keep confirmed savings and savings that need validation in separate totals.

ANSWER FORMAT
Start every answer except a quick question with a verdict line: "Action recommended",
"No action needed", "Anomaly confirmed" or "No anomaly". This includes deep dives, and
answers where you paste an artifact's content because it could not be published.
Then:
- Scope checked: services, dates, data limits that matter.
- Findings, largest dollar impact first. Each has cause, evidence (query IDs, metrics,
  code), fix, estimated monthly saving and confidence, plus the owner if known.
- Not an issue: high-cost resources you checked that are fine, and why.
- Totals: current monthly run-rate, confirmed savings, savings that need validation.
If nothing needs changing, say so plainly and list what you checked.

ARTIFACTS
Every artifact you generate for the user (report, CSV or JSON export, HTML, chart,
investigation write-up) must be stored for download. Build a report with
append_workspace_file, one section per call, using the answer's sections: verdict and scope
checked, then each finding, then not an issue, then totals. Cite query ids and figures in
evidence. Do not paste raw query rows into the file, and do not put the whole report in one
call. After the last section, call publish_artifact once. Include each download_url in the
answer and say that it expires after expires_in_seconds. Never invent a download URL."""

_CONTINUE_REPORT = (
    "The previous response hit the output limit and was cut off. Continue the same report. "
    "Call append_workspace_file once per remaining section (verdict and scope checked, each "
    "finding, not an issue, totals). Do not repeat sections already in the file. "
    "Do not paste raw query rows. When the file is complete, call publish_artifact once."
)

# Appended only when payload has async=true. post_async_webhook is the only delivery path.
ASYNC_SYSTEM_PROMPT = """

ASYNC INVOKE — WEBHOOK REQUIRED (this run)
async=true was set and webhook_url was provided. You have the post_async_webhook tool.
Your last action MUST be post_async_webhook. That tool POSTs a JSON object to the
caller's webhook: {"session_id": "...", "body": "<your full markdown>"}.
Rules:
1. Complete the user's request (report or short answer).
2. If you built a report file, publish_artifact it, then read the full file contents.
3. Call post_async_webhook exactly once with body = 100% of what you deliver — the full
   report markdown (or your full answer if there is no report). Nothing summarized,
   truncated, or omitted. Pass the markdown string as body; the tool wraps session_id.
4. Do not end the turn without a successful post_async_webhook result.
Even for a short prompt (e.g. "hi"), still call post_async_webhook with your full reply.
"""

_ASYNC_CONTINUE_REPORT = (
    "The previous response hit the output limit and was cut off. Continue the same report. "
    "Call append_workspace_file once per remaining section (verdict and scope checked, each "
    "finding, not an issue, totals). Do not repeat sections already in the file. "
    "Do not paste raw query rows. When the file is complete, call publish_artifact once, "
    "then post_async_webhook with 100% of the report markdown as body (nothing omitted)."
)

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


def _github_request(
    path: str,
    params: dict | None = None,
    accept: str = "application/vnd.github+json",
) -> dict | list:
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
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
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
    """Write a file to the session workspace (notes or one report section).

    For a report, prefer append_workspace_file so each call stays one section.
    For edits under repos/ after github_clone_repo, prefer edit_workspace_file so only
    the changed snippet is sent—not the whole file.
    User-facing artifacts must also be passed to publish_artifact.
    """
    path = _safe_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"wrote {len(content)} bytes to {name}"


@tool
def edit_workspace_file(
    name: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Apply a small search-and-replace edit in the session workspace.

    Use this for code fixes under repos/<owner>/<repo>/ after github_clone_repo.
    Include enough surrounding lines in old_string that it matches exactly once.
    read_workspace_file only the region you need before editing.

    Args:
        name: Workspace-relative path, e.g. repos/devrev/wisp/pkg/foo.go.
        old_string: Exact text to replace (empty only when creating a new file).
        new_string: Replacement text.
        replace_all: If true, replace every occurrence of old_string.
    """
    from workspace_edit import apply_workspace_edit

    try:
        result = apply_workspace_edit(
            WORKSPACE,
            name,
            old_string,
            new_string,
            replace_all=replace_all,
        )
    except (ValueError, FileNotFoundError, OSError) as e:
        return json.dumps({"error": str(e)})
    return json.dumps({**result, "path": name})


@tool
def append_workspace_file(name: str, content: str) -> str:
    """Append one report section to a workspace file, creating the file if needed.

    Call once per section (verdict and scope checked, each finding, not an issue, totals).
    Do not send the whole report in one call. After the last section, call publish_artifact.

    Args:
        name: Workspace-relative path, for example artifacts/sqs-report.md.
        content: One section of markdown, not the full report.
    """
    path = _safe_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.is_file() and path.stat().st_size and content and not content.startswith("\n"):
        prefix = "\n"
    with path.open("a") as handle:
        handle.write(prefix + content)
    return f"appended {len(content)} bytes to {name}"


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
def post_async_webhook(body: str) -> str:
    """Required final step on async=true invokes: POST JSON to the caller's webhook_url.

    Only registered when async=true. Call exactly once at the end of the run with the
    complete report markdown (or full answer). The tool POSTs application/json:
    {"session_id": "<runtime session>", "body": "<your markdown>"}. Do not summarize.

    Args:
        body: 100% of the report or answer markdown (not a summary).
    """
    if not _async_webhook_enabled.get():
        return json.dumps(
            {
                "error": "post_async_webhook is only available when async=true",
            }
        )
    webhook_url = _report_webhook_url.get().strip()
    if not webhook_url:
        return json.dumps(
            {
                "error": "webhook_url was not provided on the invoke request "
                "(required when async=true)",
            }
        )
    if not isinstance(body, str) or not body.strip():
        return json.dumps({"error": "body must be a non-empty string"})

    session_id = _report_session_id.get() or _artifact_session.get()
    payload = {"session_id": session_id, "body": body}
    data = json.dumps(payload).encode("utf-8")

    logger.info(
        "post_async_webhook posting bytes=%s session=%s url=%s",
        len(data),
        session_id,
        webhook_url,
    )
    req = urllib.request.Request(
        webhook_url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(data)),
            "X-Session-Id": session_id,
            "User-Agent": "cost-investigator-agent",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")[:2000]
            logger.info(
                "post_async_webhook ok status=%s bytes=%s session=%s",
                getattr(resp, "status", 200),
                len(data),
                session_id,
            )
            return json.dumps(
                {
                    "ok": True,
                    "status": getattr(resp, "status", 200),
                    "bytes": len(data),
                    "webhook_url": webhook_url,
                    "session_id": session_id,
                    "response": resp_body,
                }
            )
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        logger.warning(
            "post_async_webhook HTTPError status=%s session=%s detail=%s",
            e.code,
            session_id,
            detail,
        )
        return json.dumps({"ok": False, "status": e.code, "error": detail})
    except urllib.error.URLError as e:
        logger.warning(
            "post_async_webhook URLError session=%s err=%s",
            session_id,
            e,
        )
        return json.dumps({"ok": False, "error": str(e.reason if hasattr(e, "reason") else e)})


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
def github_list_commits(
    owner: str,
    repo: str,
    path: str,
    since: str = "",
    until: str = "",
    per_page: int = 30,
) -> str:
    """List commits on a repository path, newest first (marina deploy history).

    Use for devrev/marina paths like codex/dev/values.yaml around a cost spike window.
    Pair with github_get_file(ref=sha) to detect image.tag changes between commits.

    Args:
        owner: Repository owner.
        repo: Repository name (e.g. marina).
        path: File path in the repo.
        since: ISO 8601 timestamp; only commits after this time (optional).
        until: ISO 8601 timestamp; only commits before this time (optional).
        per_page: Max commits to return (1-100).
    """
    params: dict[str, str | int] = {
        "path": path,
        "per_page": max(1, min(per_page, 100)),
    }
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    data = _github_request(f"/repos/{owner}/{repo}/commits", params)
    items = []
    for hit in data if isinstance(data, list) else []:
        commit = hit.get("commit", {})
        items.append({
            "sha": hit.get("sha"),
            "message": (commit.get("message") or "").split("\n", 1)[0],
            "author": (commit.get("author") or {}).get("name"),
            "date": (commit.get("author") or {}).get("date"),
            "url": hit.get("html_url"),
        })
    return json.dumps(items, indent=2)


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


@tool
def github_clone_repo(owner: str, repo: str, base_branch: str = "main") -> str:
    """Clone or refresh a devrev GitHub repo into the session workspace for code fixes.

    Files appear under workspace/repos/<owner>/<repo>/. Edit them with read_workspace_file and
    edit_workspace_file (preferred) or write_workspace_file using that prefix. Load github-fix-pr
    before use.

    Args:
        owner: Repository owner (must be GITHUB_DEFAULT_OWNER / devrev).
        repo: Repository name.
        base_branch: Branch to clone (default main).
    """
    try:
        result = clone_repository(
            WORKSPACE,
            owner,
            repo,
            base_branch,
            _github_pat(),
            GITHUB_DEFAULT_OWNER,
        )
    except (ValueError, FileNotFoundError, RuntimeError, subprocess.SubprocessError) as e:
        return json.dumps({"error": str(e)})
    return json.dumps(result, indent=2)


@tool
def github_create_fix_pr(
    owner: str,
    repo: str,
    branch: str,
    title: str,
    body: str,
    commit_message: str,
    base_branch: str = "main",
    paths: str = "",
) -> str:
    """Commit changes in the workspace clone, push branch, and open a pull request.

    Load github-fix-pr first. Branch must be cost-fix/<slug>. Stage all changes or only paths
    (comma-separated, repo-relative).

    Args:
        owner: Repository owner (devrev).
        repo: Repository name.
        branch: New branch name, e.g. cost-fix/sqs-getqueueurl-wisp.
        title: PR title.
        body: PR description (include cost evidence and query IDs).
        commit_message: Single git commit message.
        base_branch: Target branch for the PR (default main).
        paths: Optional comma-separated paths under the clone to stage.
    """
    path_list = [p.strip() for p in paths.split(",") if p.strip()] or None
    try:
        result = create_fix_pull_request(
            WORKSPACE,
            owner,
            repo,
            branch,
            base_branch,
            title,
            body,
            commit_message,
            _github_pat(),
            GITHUB_DEFAULT_OWNER,
            path_list,
        )
    except (ValueError, FileNotFoundError, RuntimeError, subprocess.SubprocessError) as e:
        return json.dumps({"error": str(e)})
    return json.dumps(result, indent=2)


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
# post_async_webhook is attached only for async=true runs (see _build_agent).
TOOLS = [
    list_workspace,
    read_workspace_file,
    write_workspace_file,
    edit_workspace_file,
    append_workspace_file,
    publish_artifact,
    athena_query,
    github_search_code,
    github_get_file,
    github_search_commits,
    github_list_commits,
    github_list_pull_requests,
    github_clone_repo,
    github_create_fix_pr,
]
MEMORY_TOOLS = configure_shared_memory(AGENTCORE_MEMORY_ID, AWS_REGION)


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


def _webhook_url_from_payload(payload: dict) -> str:
    raw = payload.get("webhook_url") or payload.get("webhookUrl") or ""
    return str(raw).strip() if raw else ""


def _validate_webhook_url(url: str) -> str | None:
    """Return an error message if url is missing/invalid; else None."""
    if not url:
        return "webhook_url is required when async=true"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "webhook_url must be an absolute http(s) URL"
    return None


def _bind_report_webhook(url: str) -> None:
    _report_webhook_url.set(url.strip())


def _bind_async_webhook(enabled: bool) -> None:
    _async_webhook_enabled.set(bool(enabled))


def _bind_report_session(session_id: str) -> None:
    _report_session_id.set(session_id)


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


def _build_agent(
    session_manager: AgentCoreMemorySessionManager | None = None,
    *,
    async_mode: bool = False,
) -> Agent:
    grafana = _grafana_mcp()
    system = SYSTEM_PROMPT + (MEMORY_PROMPT if MEMORY_TOOLS else "")
    tools: list[Any] = list(TOOLS)
    if async_mode:
        system += ASYNC_SYSTEM_PROMPT
        tools.append(post_async_webhook)
    kwargs: dict[str, Any] = {
        "model": _model(),
        "system_prompt": system,
        "tools": tools + MEMORY_TOOLS + ([grafana] if grafana else []),
        "plugins": [_skills_plugin()],
    }
    if session_manager is not None:
        kwargs["session_manager"] = session_manager
    return Agent(**kwargs)


def _want_async(payload: dict) -> bool:
    """True when the client asked not to wait for the full investigation."""
    if payload.get("async") is True:
        return True
    if payload.get("wait") is False:
        return True
    return False


async def _run_investigation_silent(
    prompt: str, session_id: str, actor_id: str, webhook_url: str
) -> None:
    """Run the agent to completion without streaming (background / async jobs)."""
    _bind_artifact_session(session_id)
    _bind_report_session(session_id)
    _bind_report_webhook(webhook_url)
    _bind_async_webhook(True)
    session_manager = _memory_session_manager(session_id, actor_id)
    agent = _build_agent(session_manager=session_manager, async_mode=True)
    current_prompt = prompt
    report_continuations = 0
    try:
        while True:
            try:
                async for _event in agent.stream_async(current_prompt):
                    pass
                break
            except MaxTokensReachedException:
                report_continuations += 1
                if report_continuations > 3:
                    raise
                current_prompt = _ASYNC_CONTINUE_REPORT
    finally:
        if session_manager is not None:
            session_manager.close()
        agent.cleanup()


def _start_async_investigation(
    prompt: str, session_id: str, actor_id: str, webhook_url: str
) -> str:
    """Kick off investigation in a background thread; keep the runtime session busy."""
    task_id = app.add_async_task(
        "cost_report",
        {"session_id": session_id, "actor_id": actor_id},
    )

    def worker() -> None:
        try:
            asyncio.run(
                _run_investigation_silent(prompt, session_id, actor_id, webhook_url)
            )
        except Exception:
            logger.exception(
                "async investigation failed session_id=%s task_id=%s",
                session_id,
                task_id,
            )
        finally:
            try:
                app.complete_async_task(task_id)
            except Exception:
                logger.exception("complete_async_task failed task_id=%s", task_id)

    threading.Thread(
        target=worker,
        name=f"cost-report-{safe_session_id(session_id)}",
        daemon=True,
    ).start()
    return str(task_id)


@app.entrypoint
async def invoke(payload: dict, context: RequestContext) -> AsyncIterator[dict]:
    """Stream AG-UI events for the cost investigator run."""
    prompt = _prompt_from_payload(payload)
    session_id = _session_id(payload, context)
    actor_id = _actor_id(payload)
    run_id = str(uuid.uuid4())
    webhook_url = _webhook_url_from_payload(payload)
    _bind_artifact_session(session_id)
    _bind_report_session(session_id)
    _bind_report_webhook(webhook_url)
    _bind_async_webhook(False)

    if _want_async(payload):
        webhook_err = _validate_webhook_url(webhook_url)
        if webhook_err:
            yield _agui_event("RUN_STARTED", runId=run_id)
            yield _agui_event(
                "RUN_ERROR",
                runId=run_id,
                error={"code": "INVALID_INPUT", "message": webhook_err},
            )
            return
        task_id = _start_async_investigation(
            prompt, session_id, actor_id, webhook_url
        )
        yield _agui_event("RUN_STARTED", runId=run_id)
        yield _agui_event(
            "STATE_SNAPSHOT",
            snapshot={
                "status": "accepted",
                "async": True,
                "task_id": task_id,
                "session_id": session_id,
                "actor_id": actor_id,
                "webhook_url": webhook_url,
            },
        )
        for event in _text_events(
            "Accepted. Investigation is running in the background; "
            "post_async_webhook will POST the full body to webhook_url when done."
        ):
            yield event
        yield _agui_event(
            "STATE_DELTA",
            delta={"status": "busy", "async": True, "task_id": task_id},
        )
        yield _agui_event("RUN_FINISHED", runId=run_id)
        return

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

    current_prompt = prompt
    report_continuations = 0
    try:
        while True:
            try:
                async for event in agent.stream_async(current_prompt):
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
                break
            except MaxTokensReachedException:
                if active_tool_call_id is not None:
                    yield _agui_event("TOOL_CALL_END", toolCallId=active_tool_call_id)
                    active_tool_call_id = None
                if active_text_msg_id is not None:
                    yield _agui_event("TEXT_MESSAGE_END", messageId=active_text_msg_id)
                    active_text_msg_id = None
                report_continuations += 1
                if report_continuations > 3:
                    raise
                current_prompt = _CONTINUE_REPORT

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
