"""Create or update the AgentCore runtime from the latest code zip.

Usage: AWS_PROFILE=hackathon python deploy_runtime.py
"""
import json
import pathlib
import time

import boto3

REGION = "us-west-2"
ACCOUNT = "270149200236"
NAME = "cost_investigator"
BUCKET = f"cost-agent-artifacts-{ACCOUNT}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/cost-agent-runtime-role"
KEY = (pathlib.Path(__file__).parent / "build" / "last_key").read_text().strip()

ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)

spec = dict(
    agentRuntimeArtifact={"codeConfiguration": {
        "code": {"s3": {"bucket": BUCKET, "prefix": KEY}},
        "runtime": "PYTHON_3_12",
        "entryPoint": ["agent.py"],
    }},
    roleArn=ROLE_ARN,
    networkConfiguration={"networkMode": "PUBLIC"},
    filesystemConfigurations=[{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
    environmentVariables={
        "WORKSPACE_DIR": "/mnt/workspace",
        "MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    },
    lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800},
)

existing = [r for r in ctl.list_agent_runtimes()["agentRuntimes"] if r["agentRuntimeName"] == NAME]
if existing:
    rid = existing[0]["agentRuntimeId"]
    resp = ctl.update_agent_runtime(agentRuntimeId=rid, **spec)
else:
    resp = ctl.create_agent_runtime(agentRuntimeName=NAME, description="Cost investigator (Strands)", **spec)
    rid = resp["agentRuntimeId"]

while True:
    r = ctl.get_agent_runtime(agentRuntimeId=rid)
    if r["status"] not in ("CREATING", "UPDATING"):
        break
    time.sleep(10)

out = {k: r.get(k) for k in ("agentRuntimeArn", "agentRuntimeVersion", "status", "failureReason")}
out["filesystems"] = r.get("filesystemConfigurations")
print(json.dumps(out, indent=2, default=str))
(pathlib.Path(__file__).parent / "build" / "runtime.json").write_text(json.dumps(out, default=str))
