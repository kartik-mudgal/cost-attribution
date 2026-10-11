"""Invoke the deployed runtime. Usage: python invoke.py "<prompt>" [session-id]"""
import json
import pathlib
import sys
import uuid

import boto3

arn = json.loads((pathlib.Path(__file__).parent / "build" / "runtime.json").read_text())["agentRuntimeArn"]
prompt = sys.argv[1]
session = sys.argv[2] if len(sys.argv) > 2 else f"session-{uuid.uuid4().hex}"  # must be >= 33 chars

client = boto3.client("bedrock-agentcore", region_name="us-west-2")
resp = client.invoke_agent_runtime(
    agentRuntimeArn=arn,
    runtimeSessionId=session,
    payload=json.dumps({"prompt": prompt}).encode(),
)
print("session:", session)
print(resp["response"].read().decode())
