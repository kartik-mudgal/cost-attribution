"""Cost investigator agent: Strands on Bedrock, served by AgentCore Runtime.

Payload: {"prompt": "..."} or {"case": {...}}. Session storage is mounted at
WORKSPACE so files written in one invocation are visible in later invocations
of the same session.
"""
import json
import os
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

WORKSPACE = Path(os.environ.get("WORKSPACE_DIR", "/mnt/workspace"))
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

SYSTEM_PROMPT = """You investigate AWS cost changes for one DynamoDB table or SQS queue.
Use only numbers returned by tools or present in the case. Never invent figures.
Keep working notes in the workspace so a later call in the same session can continue.
Answer with: cause, evidence, fix, estimated monthly saving, confidence."""

app = BedrockAgentCoreApp()


def _safe_path(name: str) -> Path:
    path = (WORKSPACE / name).resolve()
    if WORKSPACE.resolve() not in path.parents and path != WORKSPACE.resolve():
        raise ValueError("path must stay inside the workspace")
    return path


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


# Cost tools go here: get_resource_cost (CUR via DuckDB), get_cloudwatch, describe_resource.
TOOLS = [list_workspace, read_workspace_file, write_workspace_file]


@app.entrypoint
def invoke(payload: dict) -> dict:
    if "case" in payload:
        prompt = "Investigate this case:\n" + json.dumps(payload["case"])
    else:
        prompt = payload.get("prompt", "List the workspace and say what is there.")
    agent = Agent(model=MODEL_ID, system_prompt=SYSTEM_PROMPT, tools=TOOLS)
    result = agent(prompt)
    return {"result": str(result), "workspace_files": list_workspace()}


if __name__ == "__main__":
    app.run()
