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

## Notes

- AgentCore Runtime does not expose a chat-history API. Short-term turns are
  stored by **AgentCore Memory** in the agent (see root README) when configured.
- The sample’s demo agent / notebook are not used; this UI talks to the
  cost-attribution AgentCore runtime.
