"""Invoke the deployed runtime. Usage: python invoke.py "<prompt>" [session-id] [user-id]

Streams AG-UI SSE events from the runtime and prints text deltas live; other
event types are printed as compact one-liners.
"""
import json
import os
import pathlib
import sys
import uuid

import boto3

arn = json.loads((pathlib.Path(__file__).parent / "build" / "runtime.json").read_text())["agentRuntimeArn"]
prompt = sys.argv[1]
session = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else f"session-{uuid.uuid4().hex}"  # >= 33 chars
user_id = (
    sys.argv[3]
    if len(sys.argv) > 3 and sys.argv[3]
    else os.environ.get("AGENTCORE_USER_ID", "cost-investigator")
)

client = boto3.client("bedrock-agentcore", region_name="us-west-2")
resp = client.invoke_agent_runtime(
    agentRuntimeArn=arn,
    runtimeSessionId=session,
    runtimeUserId=user_id,
    payload=json.dumps({"prompt": prompt}).encode(),
)
print("session:", session)
print("user:", user_id)
print("contentType:", resp.get("contentType", ""))

body = resp["response"]
# Streaming body: prefer iter_lines when available (botocore StreamingBody).
if hasattr(body, "iter_lines"):
    lines = (line.decode("utf-8") if isinstance(line, (bytes, bytearray)) else line for line in body.iter_lines())
else:
    lines = body.read().decode().splitlines()

in_text = False
for line in lines:
    line = line.strip()
    if not line.startswith("data: "):
        if line:
            print(line)
        continue
    try:
        event = json.loads(line[6:])
    except json.JSONDecodeError:
        print(line)
        continue
    etype = event.get("type", "")
    if etype == "TEXT_MESSAGE_CONTENT":
        sys.stdout.write(event.get("delta", ""))
        sys.stdout.flush()
        in_text = True
    elif etype == "TEXT_MESSAGE_END":
        if in_text:
            print()
            in_text = False
    elif etype == "TOOL_CALL_START":
        if in_text:
            print()
            in_text = False
        print(f"\n[tool] {event.get('toolName')} ({event.get('toolCallId')})")
    elif etype == "TOOL_CALL_RESULT":
        result = event.get("result")
        preview = json.dumps(result) if not isinstance(result, str) else result
        if len(preview) > 240:
            preview = preview[:240] + "…"
        print(f"[tool result] {preview}")
    elif etype in ("RUN_STARTED", "RUN_FINISHED", "STATE_SNAPSHOT", "STATE_DELTA"):
        print(f"[{etype}]")
    elif etype == "RUN_ERROR":
        if in_text:
            print()
            in_text = False
        print(f"[error] {event.get('error')}", file=sys.stderr)
    # TOOL_CALL_ARGS / TOOL_CALL_END: quiet by default

if in_text:
    print()
