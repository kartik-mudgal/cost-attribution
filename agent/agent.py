"""Cost investigator agent: Strands on Bedrock, served by AgentCore Runtime.

Payload: {"prompt": "..."} or {"case": {...}}. Session storage is mounted at
WORKSPACE so files written in one invocation are visible in later invocations
of the same session.

GitHub access uses the AgentCore Identity API-key credential provider
``github-pat`` (created via create-api-key-credential-provider). Tools fetch
the PAT at call time with @requires_api_key; the token never lives in env vars.

CUR data is queried through Athena (athena_query). The cur-athena skill describes
the table. One skill per service (cur-sqs, cur-dynamodb, cur-ec2, cur-s3, cur-sns,
cur-lambda, cur-elasticache, cur-bedrock) describes how to query that service.
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boto3
from bedrock_agentcore.identity.auth import requires_api_key
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, AgentSkills, tool

WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/mnt/workspace"))
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
GITHUB_PAT_PROVIDER = os.environ.get("GITHUB_PAT_PROVIDER", "github-pat")
GITHUB_DEFAULT_OWNER = os.environ.get("GITHUB_DEFAULT_OWNER", "devrev")
GITHUB_API = "https://api.github.com"
# Bundled with the runtime zip next to agent.py; override with SKILLS_DIR.
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", str(Path(__file__).resolve().parent / "skills")))
ATHENA_REGION = os.environ.get("ATHENA_REGION", "us-west-2")
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "hackathon_cur")
ATHENA_TABLE = os.environ.get("ATHENA_TABLE", "cost_report")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "cost-agent")
ATHENA_TIMEOUT_S = int(os.environ.get("ATHENA_TIMEOUT_S", "120"))
ATHENA_MAX_ROWS = 500

SYSTEM_PROMPT = f"""You investigate AWS cost changes for SQS, DynamoDB, EC2 (including EBS, NAT and public IPv4), S3, SNS, Lambda, ElastiCache and Bedrock.
Use only numbers returned by tools or present in the case. Never invent figures.
Keep working notes in the workspace so a later call in the same session can continue.

Cost data is the CUR table {ATHENA_DATABASE}.{ATHENA_TABLE}, queried with athena_query.
Before writing any query, load the cur-athena skill, then the skill for the service
in scope: cur-sqs, cur-dynamodb, cur-ec2, cur-s3, cur-sns, cur-lambda, cur-elasticache
or cur-bedrock. Analyse one service at a time. CloudWatch metric names in those skills
are confirmation targets. You cannot read CloudWatch yet, so do not invent metric values.

Skills are available via the skills tool — load github-code-lookup when you
need GitHub access. That skill uses AgentCore Identity provider ``github-pat``
to fetch a token for ``devrev`` org repos; never print or request the token.

Answer with: cause, evidence, fix, estimated monthly saving, confidence."""

_athena = boto3.client("athena", region_name=ATHENA_REGION)


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


@requires_api_key(provider_name=GITHUB_PAT_PROVIDER)
def _github_pat(*, api_key: str) -> str:
    """Fetch the GitHub PAT from AgentCore Identity (provider: github-pat)."""
    return api_key


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
    """Write a file to the session workspace (e.g. notes, intermediate results)."""
    path = _safe_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"wrote {len(content)} bytes to {name}"


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
    athena_query,
    github_search_code,
    github_get_file,
    github_search_commits,
    github_list_pull_requests,
]


@app.entrypoint
def invoke(payload: dict) -> dict:
    if "case" in payload:
        prompt = "Investigate this case:\n" + json.dumps(payload["case"])
    else:
        prompt = payload.get("prompt", "List the workspace and say what is there.")
    agent = Agent(
        model=MODEL_ID,
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        plugins=[_skills_plugin()],
    )
    result = agent(prompt)
    return {"result": str(result), "workspace_files": list_workspace()}


if __name__ == "__main__":
    app.run()
