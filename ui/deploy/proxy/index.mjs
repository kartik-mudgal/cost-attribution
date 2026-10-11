// Proxy Lambda: handles WebSocket API Gateway and HTTP API Gateway requests.
// WebSocket: receives messages on $default route, streams AG-UI events back via
//   API Gateway Management API (@connections) — true real-time streaming.
// HTTP (POST /invocations): buffers agent response and returns as SSE body
//   (API Gateway does not support Lambda response streaming).

import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";
import {
  ApiGatewayManagementApiClient,
  PostToConnectionCommand,
} from "@aws-sdk/client-apigatewaymanagementapi";

const AGENT_RUNTIME_ARN = process.env.AGENT_RUNTIME_ARN;
const REGION = process.env.REGION || "us-west-2";

const agentClient = new BedrockAgentCoreClient({ region: REGION });

// ─── Shared: invoke agent and call onEvent for each SSE event ─────────────────
async function invokeAgent(prompt, onEvent) {
  if (!AGENT_RUNTIME_ARN || AGENT_RUNTIME_ARN === "NONE") {
    await onEvent({
      type: "RUN_ERROR",
      error: { code: "PROXY_NOT_CONFIGURED", message: "AGENT_RUNTIME_ARN not set." },
    });
    return;
  }

  const cmd = new InvokeAgentRuntimeCommand({
    agentRuntimeArn: AGENT_RUNTIME_ARN,
    qualifier: "DEFAULT",
    payload: new TextEncoder().encode(JSON.stringify({ prompt })),
  });

  const resp = await agentClient.send(cmd);
  const body = resp.response;

  if (body && typeof body[Symbol.asyncIterator] === "function") {
    let buffer = "";
    for await (const chunk of body) {
      buffer += new TextDecoder().decode(chunk);
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith("data: ")) {
          try {
            const event = JSON.parse(trimmed.slice(6));
            await onEvent(event);
          } catch (e) {}
        }
      }
    }
    if (buffer.trim().startsWith("data: ")) {
      try {
        const event = JSON.parse(buffer.trim().slice(6));
        await onEvent(event);
      } catch (e) {}
    }
  }
}

// ─── WebSocket handler ────────────────────────────────────────────────────────
async function handleWebSocket(event) {
  const { requestContext } = event;
  const routeKey = requestContext.routeKey;
  const connectionId = requestContext.connectionId;

  if (routeKey === "$connect") {
    return { statusCode: 200, body: "Connected" };
  }

  if (routeKey === "$disconnect") {
    return { statusCode: 200, body: "Disconnected" };
  }

  // $default route — process the message
  const { domainName, stage } = requestContext;
  const callbackUrl = `https://${domainName}/${stage}`;
  const apigw = new ApiGatewayManagementApiClient({
    region: REGION,
    endpoint: callbackUrl,
  });

  let prompt = "";
  try {
    const body = JSON.parse(event.body || "{}");
    prompt = body.prompt || body.message || "";
  } catch (e) {
    prompt = "";
  }

  if (!prompt) {
    await postToConnection(apigw, connectionId, {
      type: "RUN_ERROR",
      error: { code: "INVALID_INPUT", message: "No prompt provided" },
    });
    return { statusCode: 200, body: "ok" };
  }

  try {
    await invokeAgent(prompt, async (agEvent) => {
      await postToConnection(apigw, connectionId, agEvent);
    });
  } catch (err) {
    await postToConnection(apigw, connectionId, {
      type: "RUN_ERROR",
      error: { code: "AGENT_INVOKE_ERROR", message: err.message || String(err) },
    });
  }

  return { statusCode: 200, body: "ok" };
}

async function postToConnection(apigw, connectionId, data) {
  try {
    await apigw.send(
      new PostToConnectionCommand({
        ConnectionId: connectionId,
        Data: JSON.stringify(data),
      })
    );
  } catch (e) {
    if (e.statusCode === 410 || e.name === "GoneException") return;
    throw e;
  }
}

// ─── HTTP handler (SSE — buffered since API GW doesn't support streaming) ────
async function handleHTTP(event) {
  let prompt = "";
  try {
    const raw = event.isBase64Encoded
      ? Buffer.from(event.body || "", "base64").toString("utf8")
      : event.body || "{}";
    const body = JSON.parse(raw);
    prompt = body.prompt || body.message || "";
  } catch (e) {
    return {
      statusCode: 400,
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ error: "invalid JSON body" }),
    };
  }

  const events = [];
  try {
    await invokeAgent(prompt, async (agEvent) => {
      events.push(`data: ${JSON.stringify(agEvent)}\n\n`);
    });
  } catch (err) {
    events.push(
      `data: ${JSON.stringify({ type: "RUN_ERROR", error: { code: "AGENT_INVOKE_ERROR", message: err.message || String(err) } })}\n\n`
    );
  }

  return {
    statusCode: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      "Access-Control-Allow-Origin": "*",
    },
    body: events.join(""),
  };
}

// ─── Main handler ─────────────────────────────────────────────────────────────
export const handler = async (event, context) => {
  // WebSocket events have requestContext.eventType
  if (event.requestContext?.eventType) {
    return handleWebSocket(event);
  }
  // HTTP API events
  return handleHTTP(event);
};
