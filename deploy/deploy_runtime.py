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

_build = pathlib.Path(__file__).parent / "build"
_devrev_secret_arn = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:cost-agent-devrev-pat"
_devrev_meta = _build / "devrev-secret.json"
if _devrev_meta.is_file():
    _devrev_secret_arn = json.loads(_devrev_meta.read_text()).get("secret_arn", _devrev_secret_arn)

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
        "MODEL_ID": "us.anthropic.claude-sonnet-5",
        "MAX_OUTPUT_TOKENS": "16000",
        "GITHUB_DEFAULT_OWNER": "devrev",
        # Secrets Manager ARN for the GitHub PAT (JSON key api_key_value).
        "GITHUB_PAT_SECRET_ARN": (
            "arn:aws:secretsmanager:us-west-2:270149200236:secret:"
            "bedrock-agentcore-identity!default/apikey/github-pat-50ea78aa-npH3Wy"
        ),
        "GITHUB_PAT_SECRET_JSON_KEY": "api_key_value",
        "ATHENA_REGION": REGION,
        "ATHENA_DATABASE": "hackathon_cur",
        "ATHENA_TABLE": "cost_report",
        "ATHENA_WORKGROUP": "cost-agent",
        # Empty disables Grafana; set to https://ai-gateway.dev.devrev-eng.ai/grafana/mcp to attach it.
        "GRAFANA_MCP_URL": "",
        # Secrets Manager ARN for the AI gateway virtual key (JSON key x-litellm-api-key).
        "GRAFANA_MCP_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:270149200236:secret:grafana-mcp-csUlmY",
        "GRAFANA_MCP_SECRET_JSON_KEY": "x-litellm-api-key",
        # Short-term conversational turns (AgentCore Memory STM, no LTM strategies).
        "AGENTCORE_MEMORY_ID": "cost_investigator_stm-geZl5e2x3J",
        "AGENTCORE_ACTOR_ID": "cost-investigator",
        # User downloads. Separate from the code-zip bucket cost-agent-artifacts-*.
        "ARTIFACT_BUCKET": f"cost-agent-user-artifacts-{ACCOUNT}",
        "ARTIFACT_PREFIX": "generated",
        "ARTIFACT_URL_TTL_S": "3600",
        "DEVREV_PAT_SECRET_ARN": _devrev_secret_arn,
        "DEVREV_PAT_SECRET_JSON_KEY": "pat",
        "DEVREV_ISSUE_APPLIES_TO_PART": "don:core:dvrv-us-1:devo/0:feature/2971",
        "DEVREV_ISSUE_OWNED_BY": "don:identity:dvrv-us-1:devo/0:svcacc/273",
        "DEVREV_ISSUE_SPACE": "don:identity:dvrv-us-1:devo/0:space/17H9CvPAF",
        "DEVREV_ISSUE_TAG_ID": "don:core:dvrv-us-1:devo/0:tag/191028",
        "DEVREV_ISSUE_STAGE_ID": "don:core:dvrv-us-1:devo/0:custom_stage/16",
        "DEVREV_ISSUE_PRIORITY": "3",
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
