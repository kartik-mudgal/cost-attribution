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
  --policy-name runtime \
  --policy-document file://deploy/role-policy.json
```

### Athena workgroup for CUR queries

The agent queries the CUR table `hackathon_cur.cost_report` (Glue catalog, `us-west-2`) through a
dedicated `cost-agent` workgroup. The workgroup uses Athena managed query results, so there is no
results bucket and the runtime role needs no S3 permissions for results. It also caps each query
at 1 GB scanned.

```bash
aws athena create-work-group --name cost-agent --region us-west-2 \
  --configuration '{
    "ManagedQueryResultsConfiguration": {"Enabled": true},
    "EnforceWorkGroupConfiguration": true,
    "BytesScannedCutoffPerQuery": 1073741824,
    "PublishCloudWatchMetricsEnabled": true
  }'
```

`ManagedQueryResultsConfiguration` needs a recent AWS CLI. If yours rejects it, upgrade the CLI or
turn on managed query results for the workgroup in the Athena console.

The runtime role's Athena, Glue and CUR bucket permissions are in `deploy/role-policy.json`
(statements `AthenaQuery`, `CurCatalog`, `CurDataList`, `CurDataRead`). After changing that file,
apply it with the `aws iam put-role-policy` command above.

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

### CI deploy (merge to main)

`.github/workflows/deploy.yml` applies `deploy/role-policy.json` to `cost-agent-runtime-role`,
then runs `package.sh` and `deploy_runtime.py`, on pushes to `main` that touch `agent/` or
`deploy/` (and on manual `workflow_dispatch`). Re-applying an unchanged policy document has no
effect on permissions.

Add these repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
| --- | --- |
| `AWS_ACCESS_KEY_ID` | Access key for an IAM user/role that can deploy (see below) |
| `AWS_SECRET_ACCESS_KEY` | Matching secret key |

The AWS principal behind those keys needs permission to:

- `s3:PutObject` (and usually `s3:GetObject`) on `s3://cost-agent-artifacts-270149200236/*`
- AgentCore control: create/update/get/list agent runtimes in `us-west-2`
- `iam:PassRole` on `arn:aws:iam::270149200236:role/cost-agent-runtime-role`
- `iam:PutRolePolicy` on `arn:aws:iam::270149200236:role/cost-agent-runtime-role`

## Invoke

```bash
uv run --with boto3 python deploy/invoke.py "List the workspace and say what is there."

# Name one service per request (sqs or dynamodb):
uv run --with boto3 python deploy/invoke.py "Which SQS queues changed most in cost over the last week, and why?"

# Reuse a session to keep the same workspace (the ID must be at least 33 characters):
uv run --with boto3 python deploy/invoke.py "Continue from your notes." session-<id-from-previous-call>

# Optional third arg / AGENTCORE_USER_ID: runtimeUserId for AgentCore Identity (github-pat).
# Default is cost-investigator. Required for GitHub tools — without it @requires_api_key fails.
uv run --with boto3 python deploy/invoke.py "Search github for marina" session-$(uuidgen | tr -d -) my-user
```

`invoke.py` always sends `runtimeUserId` so the runtime can mint a workload access token for
Identity (provider `github-pat` on workload `cost_investigator-jnlRkABJRb`).

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
| `ATHENA_REGION` | `us-west-2` | Region of the Athena workgroup and Glue catalog |
| `ATHENA_DATABASE` | `hackathon_cur` | Glue database holding the CUR table |
| `ATHENA_TABLE` | `cost_report` | CUR 2.0 table, partitioned by `billing_period` |
| `ATHENA_WORKGROUP` | `cost-agent` | Workgroup with managed query results and a per-query scan limit |
| `ATHENA_TIMEOUT_S` | `120` | Seconds before `athena_query` stops a query |

## Athena query tool

`athena_query(sql, max_rows=200)` in `agent/agent.py` runs one read-only statement and returns the
rows as JSON with the query ID and MB scanned. It accepts only `SELECT`, `WITH`, `DESCRIBE` and
`SHOW`, requires `SELECT`/`WITH` queries to filter on `billing_period`, and returns at most 500 rows
(200 by default), with a note telling the agent to aggregate further when results are cut off.

Restricting a query to one service (SQS or DynamoDB) is left to the skills, not the tool.

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
| `cur-athena` | Before any cost query: CUR table, key columns, cost rules, one-service scope, investigation pattern |
| `cur-sqs` | SQS analysis: billed operations, waste checks (URL lookups, batching, empty receives), example queries |
| `cur-dynamodb` | DynamoDB analysis: on-demand vs provisioned, storage and PITR, Streams, item-size signals, example queries |

Only SQS and DynamoDB are covered. To add a service, create `agent/skills/cur-<service>/SKILL.md`
and add its product code to the scope table in `cur-athena`.

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

Workspace tools, GitHub code tools (via `github-pat`), the `athena_query` tool and the CUR skills
are in place. Still to add in `agent/agent.py`: `get_cloudwatch` and `describe_resource`. Until
then the skills tell the agent which CloudWatch metric would confirm a finding and to lower its
confidence.
