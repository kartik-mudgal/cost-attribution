# DevRev AWS Cost Investigator — chat UI

Local and CloudFront chat frontend for the AgentCore cost investigator, adapted from
[aws-samples/sample-agui-bedrock-agentcore](https://github.com/aws-samples/sample-agui-bedrock-agentcore).

## Local demo (against deployed runtime)

Requires AWS credentials that can call `InvokeAgentRuntime`, and
`deploy/build/runtime.json` from a prior agent deploy.

```bash
# from repo root
uv run --with boto3 python ui/local_server.py
# open http://127.0.0.1:8787
```

Use the **SSE** transport. Session id is kept in `localStorage` and the URL
(`?sessionId=…`, ≥33 chars). Same id → same AgentCore runtime session / workspace.

Optional: point at a local agent instead of the deployed runtime:

```bash
COST_AGENT_URL=http://127.0.0.1:8080 uv run --with boto3 python ui/local_server.py
```

## AWS-hosted UI (CloudFormation)

Deploys S3 + CloudFront (HTML) and API Gateway + Lambda (SigV4 proxy to your
AgentCore runtime). Needs Node 20+, AWS credentials, and
`deploy/build/runtime.json`.

```bash
# from repo root — uses your public IP as CloudFront allowlist (/32)
./ui/deploy.sh

# or pin CIDR / stack name
ALLOWED_CIDR=203.0.113.10/32 STACK_NAME=cost-agent-ui ./ui/deploy.sh
```

First create takes ~5 minutes (CloudFront). The script prints the Frontend URL.
Re-run to update the proxy zip, Lambda env, and `index.html`.

## API auth (required)

The HTTP/WebSocket proxy requires a shared secret:

| Transport | How to send |
| --- | --- |
| HTTP | Header `X-Cost-Agent-Secret: <secret>` (or `Authorization: Bearer <secret>`) |
| WebSocket | JSON field `api_secret` on each message |

Secret is stored in Secrets Manager as `cost-agent-ui/api-secret` (created by
`./ui/deploy.sh`). Optional override: env / GitHub secret `COST_AGENT_API_SECRET`.

```bash
SECRET=$(aws secretsmanager get-secret-value --secret-id cost-agent-ui/api-secret \
  --query SecretString --output text --region us-west-2)

# Chat / streaming (default): AG-UI SSE
curl -sS -N -X POST 'https://<HttpApiUrl>/invocations' \
  -H 'Content-Type: application/json' \
  -H "X-Cost-Agent-Secret: $SECRET" \
  -d '{"prompt":"hi","session_id":"session-workflow-xxxxxxxxxxxxxxxxx"}'

# Workflows: single JSON body (no SSE parsing)
curl -sS -X POST 'https://<HttpApiUrl>/invocations' \
  -H 'Content-Type: application/json' \
  -H "X-Cost-Agent-Secret: $SECRET" \
  -d '{"prompt":"hi","session_id":"session-workflow-xxxxxxxxxxxxxxxxx","stream":false}'
```

JSON mode (`"stream": false` or `"response_format": "json"`) returns:

```json
{"status_code":200,"session_id":"...","markdown":"Hi! ...","error":null}
```

### Workflow: async invoke + webhook

Pass `"async": true` and a `webhook_url`. AgentCore accepts quickly and runs in the
background with an async-only system prompt and the `post_async_webhook` tool, which
must POST **100%** of the report/answer to that URL (`X-Session-Id` header). Sync/chat
invokes do not register that tool.

```bash
SECRET=$(aws secretsmanager get-secret-value --secret-id cost-agent-ui/api-secret \
  --query SecretString --output text --region us-west-2)
SID="session-workflow-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
WEBHOOK='https://api.dev.devrev-eng.ai/hidden/dev-orgs/DEV-787/event-source-webhooks/custom/12970cfe-a77d-47a6-a95c-cff600d60a07'

curl -sS -X POST 'https://<HttpApiUrl>/invocations' \
  -H 'Content-Type: application/json' \
  -H "X-Cost-Agent-Secret: $SECRET" \
  -d "{
    \"async\": true,
    \"webhook_url\": \"$WEBHOOK\",
    \"session_id\": \"$SID\",
    \"prompt\": \"Investigate SQS cost for October 2026. Write the full report, publish_artifact, then post_async_webhook with the entire report markdown as body.\"
  }"
# → 202 {"accepted":true,"session_id":"...","webhook_url":"..."}
# Missing webhook_url → 400
```

This is a basic shared gate (the browser UI embeds the secret at deploy time).
It stops casual anonymous abuse; it is not per-user auth.

## Notes

- AgentCore Runtime does not expose a chat-history API. Short-term turns are
  stored by **AgentCore Memory** in the agent (see root README) when configured.
- The sample’s demo agent / notebook are not used; this UI talks to the
  cost-attribution AgentCore runtime.
