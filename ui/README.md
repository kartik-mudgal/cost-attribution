# Cost investigator chat UI

Local chat frontend for the AgentCore cost investigator, adapted from
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

## AWS-hosted UI (optional)

`deploy/cloudformation.yaml` + `deploy/proxy/` are the sample’s CloudFront /
API Gateway / Lambda proxy. After the stack is up, set the proxy Lambda’s
`AGENT_RUNTIME_ARN` to your cost investigator runtime ARN and upload
`frontend/index.html` (inject `window.__API_BASE` / `window.__WS_URL` from stack
outputs). Prefer the local server for hackathon demos.

## Notes

- AgentCore Runtime does not expose a chat-history API. Short-term turns are
  stored by **AgentCore Memory** in the agent (see root README) when configured.
- The sample’s demo agent / notebook are not used; this UI talks to the
  cost-attribution AgentCore runtime.
