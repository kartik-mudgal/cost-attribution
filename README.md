# cost-attribution

A cost investigator agent for AWS. Given a prompt or a "case" about a DynamoDB table or SQS queue,
it works out why the cost changed and replies with the cause, evidence, fix, estimated monthly saving
and confidence.

The agent is built with [Strands](https://strandsagents.com/), calls Claude Sonnet 4.5 on Amazon Bedrock,
and is hosted on Amazon Bedrock AgentCore Runtime. Each session gets a persistent workspace mounted at
`/mnt/workspace`, so notes written in one call are still there in later calls with the same session ID.

## Layout

```
agent/
  agent.py            Agent code and AgentCore entrypoint
  requirements.txt    Python dependencies
  skills/             Strands Agent Skills (SKILL.md packages), bundled into the runtime zip
deploy/
  package.sh          Builds the code zip and uploads it to S3
  deploy_runtime.py   Creates or updates the AgentCore runtime
  invoke.py           Calls the deployed runtime
  trust.json          Trust policy for the runtime IAM role
  role-policy.json    Permissions policy for the runtime IAM role
architecture.html     Architecture notes
cur-guide.html        Cost and Usage Report (CUR) guide
```

## What is not committed

These paths are listed in `.gitignore` because they are generated:

| Path | What it is | How to recreate it |
| --- | --- | --- |
| `agent/.venv/` | Local Python 3.12 virtual environment | `cd agent && uv venv --python 3.12 && uv pip install -r requirements.txt` |
| `deploy/build/pkg/` | Linux ARM64 dependencies plus `agent.py`, unzipped | `deploy/package.sh` |
| `deploy/build/agent.zip` | The code zip uploaded to S3 | `deploy/package.sh` |
| `deploy/build/last_key` | S3 key of the most recent upload, read by `deploy_runtime.py` | `deploy/package.sh` |
| `deploy/build/runtime.json` | ARN, version and status of the deployed runtime, read by `invoke.py` | `deploy/deploy_runtime.py` |

`package.sh` deletes and rebuilds the whole `deploy/build/` folder each time it runs.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python 3.12
- AWS CLI v2 with credentials for account `270149200236` (the scripts use the `hackathon` profile)
- `zip`

All resources are in `us-west-2`.

## One-time AWS setup

You only need this if the S3 bucket and IAM role don't exist yet.

```bash
export AWS_PROFILE=hackathon

aws s3 mb s3://cost-agent-artifacts-270149200236 --region us-west-2

aws iam create-role --role-name cost-agent-runtime-role \
  --assume-role-policy-document file://deploy/trust.json
aws iam put-role-policy --role-name cost-agent-runtime-role \
  --policy-name cost-agent-runtime \
  --policy-document file://deploy/role-policy.json
```

## Build and deploy

```bash
export AWS_PROFILE=hackathon

# 1. Build the zip (Linux ARM64 wheels + agent.py) and upload it to S3.
#    Writes deploy/build/agent.zip and deploy/build/last_key.
./deploy/package.sh

# 2. Create the runtime, or update it if it already exists, and wait until it is ready.
#    Writes deploy/build/runtime.json.
uv run --with boto3 python deploy/deploy_runtime.py
```

`package.sh` uses the `BUCKET` and `KEY` environment variables if they are set, so you can upload
somewhere else without editing the script.

## Invoke

```bash
uv run --with boto3 python deploy/invoke.py "List the workspace and say what is there."

# Reuse a session to keep the same workspace (the ID must be at least 33 characters):
uv run --with boto3 python deploy/invoke.py "Continue from your notes." session-<id-from-previous-call>
```

`invoke.py` reads the runtime ARN from `deploy/build/runtime.json`. On a fresh clone that file won't
exist. You can either run `deploy_runtime.py` (which redeploys from `last_key`, so run `package.sh`
first), or create the file by hand using the existing runtime's ARN:

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-west-2 \
  --query "agentRuntimes[?agentRuntimeName=='cost_investigator'].agentRuntimeArn" --output text
```

```bash
mkdir -p deploy/build
echo '{"agentRuntimeArn": "<arn from above>"}' > deploy/build/runtime.json
```

## Configuration

The runtime sets these environment variables (see `deploy_runtime.py`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORKSPACE_DIR` | `/mnt/workspace` | Where session storage is mounted |
| `MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock model or inference profile |
| `GITHUB_PAT_PROVIDER` | `github-pat` | AgentCore Identity API-key credential provider name |
| `GITHUB_DEFAULT_OWNER` | `devrev` | Default GitHub org for code/commit search |
| `SKILLS_DIR` | `<alongside agent.py>/skills` | Bundled Strands skills directory |

## Agent skills

Strands [Agent Skills](https://strandsagents.com/docs/user-guide/concepts/plugins/skills/) live under
`agent/skills/<skill-name>/SKILL.md`. `package.sh` copies that tree into the runtime zip. At invoke
time the agent registers them with `AgentSkills` and can load full instructions on demand via the
`skills` tool.

Also loaded if present: `/mnt/workspace/skills/` (session-specific skills written into the workspace).

Bundled today:

| Skill | When to use |
| --- | --- |
| `github-code-lookup` | Fetch GitHub PAT via Identity and access `devrev` org repos |

Add a new skill by creating `agent/skills/<name>/SKILL.md` with YAML frontmatter (`name`,
`description`) and markdown instructions, then redeploy.

## GitHub credential (AgentCore Identity)

Store a GitHub PAT once (do not put it in env vars or the zip):

```bash
aws bedrock-agentcore-control create-api-key-credential-provider \
  --name github-pat --api-key "<token>" --region us-west-2
```

The runtime role must allow `bedrock-agentcore:GetResourceApiKey` on that provider and
`secretsmanager:GetSecretValue` on the managed secret (see `deploy/role-policy.json`). Agent tools
fetch the PAT with `@requires_api_key(provider_name="github-pat")`.

## Status

Workspace tools, GitHub code tools (via `github-pat`), and skills are in place. Cost tools are still
to be added in `agent/agent.py`: `get_resource_cost` (CUR data via DuckDB), `get_cloudwatch` and
`describe_resource`.
