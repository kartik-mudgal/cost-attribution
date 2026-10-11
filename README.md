# cost-attribution

A cost investigator agent for AWS. Given a prompt or a "case" about one service
(SQS, DynamoDB, EC2, S3, SNS, Lambda, ElastiCache or Bedrock), it works out why the cost
changed and replies with the cause, evidence, fix, estimated monthly saving and confidence.

The agent is built with [Strands](https://strandsagents.com/), calls Claude Sonnet 5 on Amazon Bedrock,
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
at 20 GB scanned (about $0.10 at Athena's $5 per TB).

The table has no partition by service, so every query reads the whole month for the columns it
uses. In early October 2026 (10 days of data), a DynamoDB breakdown already scanned about 0.7 GB;
a full month will be roughly three times that. Athena cancels any query that goes over the cap.

```bash
aws athena create-work-group --name cost-agent --region us-west-2 \
  --configuration '{
    "ManagedQueryResultsConfiguration": {"Enabled": true},
    "EnforceWorkGroupConfiguration": true,
    "BytesScannedCutoffPerQuery": 21474836480,
    "PublishCloudWatchMetricsEnabled": true
  }'
```

To change the cap on the existing workgroup (value in bytes):

```bash
aws athena update-work-group --work-group cost-agent --region us-west-2 \
  --configuration-updates '{"BytesScannedCutoffPerQuery": 21474836480}'
```

`ManagedQueryResultsConfiguration` needs a recent AWS CLI. If yours rejects it, upgrade the CLI or
turn on managed query results for the workgroup in the Athena console.

The runtime role's Athena, Glue and CUR bucket permissions are in `deploy/role-policy.json`
(statements `AthenaQuery`, `CurCatalog`, `CurDataList`, `CurDataRead`). The deploy workflow applies
that file to the role on every merge to `main`, so change permissions there, not by hand.

## AgentCore Memory (short-term turns)

Conversation turns are stored in AgentCore Memory STM (no long-term strategies),
wired through `AgentCoreMemorySessionManager` in `agent/agent.py`.

```bash
# One-time (or reuse): creates cost_investigator_stm and writes deploy/build/memory.json
uv run --with boto3 python deploy/create_memory.py
```

`deploy/deploy_runtime.py` sets `AGENTCORE_MEMORY_ID` / `AGENTCORE_ACTOR_ID`.
IAM for CreateEvent/ListEvents/GetEvent/DeleteEvent is in `deploy/role-policy.json`
(`AgentCoreMemoryStm`).

## Local chat UI

See [`ui/README.md`](ui/README.md). Quick start:

```bash
uv run --with boto3 python ui/local_server.py
# http://127.0.0.1:8787
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

# Name one service per request:
uv run --with boto3 python deploy/invoke.py "Which SQS queues changed most in cost over the last week, and why?"

# Reuse a session to keep the same workspace (the ID must be at least 33 characters):
uv run --with boto3 python deploy/invoke.py "Continue from your notes." session-<id-from-previous-call>

# Optional third arg / AGENTCORE_USER_ID: runtimeUserId (not required for GitHub; PAT comes from Secrets Manager).
uv run --with boto3 python deploy/invoke.py "Search github for marina" session-$(uuidgen | tr -d -) my-user
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
| `MODEL_ID` | `us.anthropic.claude-sonnet-5` | Bedrock model or inference profile |
| `GITHUB_DEFAULT_OWNER` | `devrev` | Default GitHub org for code/commit search |
| `GITHUB_PAT_SECRET_ARN` | AgentCore-managed `github-pat` secret | Secrets Manager ARN for the GitHub PAT |
| `GITHUB_PAT_SECRET_JSON_KEY` | `api_key_value` | JSON key inside that secret |
| `SKILLS_DIR` | `<alongside agent.py>/skills` | Bundled Strands skills directory |
| `ATHENA_REGION` | `us-west-2` | Region of the Athena workgroup and Glue catalog |
| `ATHENA_DATABASE` | `hackathon_cur` | Glue database holding the CUR table |
| `ATHENA_TABLE` | `cost_report` | CUR 2.0 table, partitioned by `billing_period` |
| `ATHENA_WORKGROUP` | `cost-agent` | Workgroup with managed query results and a 20 GB per-query scan limit |
| `ATHENA_TIMEOUT_S` | `120` | Seconds before `athena_query` stops a query |
| `GRAFANA_MCP_URL` | `https://ai-gateway.dev.devrev-eng.ai/grafana/mcp` | Grafana MCP server (streamable HTTP). Empty disables it |
| `GRAFANA_MCP_SECRET_ARN` | `grafana-mcp` secret | Secrets Manager ARN for the AI gateway virtual key |
| `GRAFANA_MCP_SECRET_JSON_KEY` | `x-litellm-api-key` | JSON key inside that secret |

## Athena query tool

`athena_query(sql, max_rows=200)` in `agent/agent.py` runs one read-only statement and returns the
rows as JSON with the query ID and MB scanned. It accepts only `SELECT`, `WITH`, `DESCRIBE` and
`SHOW`, requires `SELECT`/`WITH` queries to filter on `billing_period`, and returns at most 500 rows
(200 by default), with a note telling the agent to aggregate further when results are cut off.

Restricting a query to one service is left to the skills, not the tool.

## Agent skills

Strands [Agent Skills](https://strandsagents.com/docs/user-guide/concepts/plugins/skills/) live under
`agent/skills/<skill-name>/SKILL.md`. `package.sh` copies that tree into the runtime zip. At invoke
time the agent registers them with `AgentSkills` and can load full instructions on demand via the
`skills` tool.

Also loaded if present: `/mnt/workspace/skills/` (session-specific skills written into the workspace).

Bundled today:

| Skill | When to use |
| --- | --- |
| `github-code-lookup` | Access `devrev` org repos (PAT from Secrets Manager) |
| `cur-athena` | Before any cost query: CUR table, key columns, cost rules, one-service scope, investigation pattern |
| `cur-sqs` | SQS: request mix, batching, empty receives, top cost drivers and metrics |
| `cur-dynamodb` | DynamoDB: on-demand vs provisioned, storage, PITR, Streams, item size |
| `cur-ec2` | EC2, EBS, NAT and public IPv4: instance hours, volumes, transfer |
| `cur-s3` | S3: storage class, request tier, transfer, retrieval and early-delete fees |
| `cur-sns` | SNS: publishes, deliveries, fan-out transfer, filtering |
| `cur-lambda` | Lambda: GB-seconds, invocations, provisioned concurrency |
| `cur-elasticache` | ElastiCache: node-hours by type, replicas, backups |
| `cur-bedrock` | Bedrock: token types, marketplace model lines, provisioned throughput |
| `cloudwatch-metrics` | Static October 2026 CloudWatch daily values in the `cloudwatch_metrics` Athena view |

To add a service, create `agent/skills/cur-<service>/SKILL.md` and add its product code to the
scope table in `cur-athena`.

Add a new skill by creating `agent/skills/<name>/SKILL.md` with YAML frontmatter (`name`,
`description`) and markdown instructions, then redeploy.

## GitHub credential (Secrets Manager)

The agent reads the GitHub PAT with `secretsmanager:GetSecretValue` (see `GithubPatSecret` in
`deploy/role-policy.json`). By default it uses the secret created when you stored the PAT in
AgentCore Identity (JSON field `api_key_value`):

```bash
aws bedrock-agentcore-control create-api-key-credential-provider \
  --name github-pat --api-key "<token>" --region us-west-2
# Note the apiKeySecretArn from the response → GITHUB_PAT_SECRET_ARN
```

Or create any Secrets Manager secret with `{"api_key_value":"<pat>"}` and point
`GITHUB_PAT_SECRET_ARN` at it. Do not put the PAT in the code zip or plain env vars.

## Grafana MCP server

The agent attaches Grafana as an MCP server through the DevRev AI gateway
(`https://ai-gateway.dev.devrev-eng.ai/grafana/mcp`). Each request sends the header
`x-litellm-api-key: Bearer <virtual key>`.

- The virtual key is in the Secrets Manager secret `grafana-mcp`, under the JSON key
  `x-litellm-api-key`. It can be stored with or without the `Bearer ` prefix. The role reads it via
  `GrafanaMcpSecret` in `deploy/role-policy.json`.
- The gateway exposes 61 tools, named `grafana-<tool>` (e.g. `grafana-query_prometheus`). They
  include write actions such as creating incidents, annotations and datasources, and installing
  plugins; nothing filters them yet.
- A new MCP connection is opened for each invocation and closed when the agent finishes.
- If the secret can't be read or the server can't be reached, the agent logs a warning and runs
  without the Grafana tools.

## Status

Workspace tools, GitHub code tools (Secrets Manager PAT), the `athena_query` tool, the CUR
skills, the Grafana MCP server and the `cloudwatch-metrics` skill are in place. October 2026
daily metrics are the Athena view `cloudwatch_metrics`. No skill or prompt tells the agent when
to use the Grafana tools yet. Still to add in `agent/agent.py`: a live `get_cloudwatch` tool and
`describe_resource`.
