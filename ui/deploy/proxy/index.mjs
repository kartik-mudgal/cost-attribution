// Proxy Lambda: WebSocket + HTTP → InvokeAgentRuntime (AG-UI SSE).
// HTTP buffers the agent stream (API Gateway does not support response streaming).

import {
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
} from "@aws-sdk/client-bedrock-agentcore";
import {
  ApiGatewayManagementApiClient,
  PostToConnectionCommand,
} from "@aws-sdk/client-apigatewaymanagementapi";
import { randomUUID } from "crypto";

const AGENT_RUNTIME_ARN = process.env.AGENT_RUNTIME_ARN;
const REGION = process.env.REGION || "us-west-2";
const DEFAULT_ACTOR = process.env.AGENTCORE_ACTOR_ID || "cost-investigator";
// Shared secret for HTTP header X-Cost-Agent-Secret (or Bearer / body.api_secret).
const REQUIRED_SECRET = (process.env.COST_AGENT_API_SECRET || "").trim();

const agentClient = new BedrockAgentCoreClient({ region: REGION });

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, X-Cost-Agent-Secret, Authorization",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function headerGet(headers, name) {
  if (!headers) return "";
  const want = name.toLowerCase();
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === want) return String(v || "");
  }
  return "";
}

function extractSecret(event, body) {
  const headers = event.headers || {};
  const fromHeader = headerGet(headers, "x-cost-agent-secret");
  if (fromHeader) return fromHeader.trim();
  const auth = headerGet(headers, "authorization");
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  if (body && typeof body.api_secret === "string") return body.api_secret.trim();
  return "";
}

function authorize(event, body) {
  if (!REQUIRED_SECRET) {
    // Misconfigured deploy — fail closed in Lambda (deploy.sh always sets the secret).
    return false;
  }
  const got = extractSecret(event, body);
  return got.length > 0 && got === REQUIRED_SECRET;
}

function unauthorizedJson() {
  return {
    statusCode: 401,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    body: JSON.stringify({
      error: "unauthorized",
      message: "Missing or invalid X-Cost-Agent-Secret header",
    }),
  };
}

function sessionIdFrom(body) {
  const sid = body.session_id || body.sessionId || body.thread_id || "";
  if (typeof sid === "string" && sid.length >= 33) return sid;
  return `session-${randomUUID().replace(/-/g, "")}`;
}

function actorIdFrom(body) {
  return body.actor_id || body.user_id || DEFAULT_ACTOR;
}

function wrapPlainJson(text) {
  const runId = randomUUID();
  const messageId = randomUUID();
  return [
    { type: "RUN_STARTED", runId },
    { type: "TEXT_MESSAGE_START", messageId, role: "assistant" },
    { type: "TEXT_MESSAGE_CONTENT", messageId, delta: text },
    { type: "TEXT_MESSAGE_END", messageId },
    { type: "RUN_FINISHED", runId },
  ];
}

function webhookUrlFrom(body) {
  const raw = body.webhook_url || body.webhookUrl || "";
  return typeof raw === "string" ? raw.trim() : "";
}

function validateWebhookUrl(url) {
  if (!url) return "webhook_url is required when async=true";
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return "webhook_url must be an absolute http(s) URL";
    }
  } catch (_) {
    return "webhook_url must be an absolute http(s) URL";
  }
  return null;
}

async function invokeAgent({ prompt, sessionId, actorId, asyncMode, webhookUrl }, onEvent) {
  if (!AGENT_RUNTIME_ARN || AGENT_RUNTIME_ARN === "NONE") {
    await onEvent({
      type: "RUN_ERROR",
      error: { code: "PROXY_NOT_CONFIGURED", message: "AGENT_RUNTIME_ARN not set." },
    });
    return;
  }

  const payload = { prompt, actor_id: actorId, session_id: sessionId };
  if (asyncMode) {
    payload.async = true;
    payload.webhook_url = webhookUrl;
  } else if (webhookUrl) {
    payload.webhook_url = webhookUrl;
  }

  const cmd = new InvokeAgentRuntimeCommand({
    agentRuntimeArn: AGENT_RUNTIME_ARN,
    qualifier: "DEFAULT",
    runtimeSessionId: sessionId,
    runtimeUserId: actorId,
    payload: new TextEncoder().encode(JSON.stringify(payload)),
  });

  const resp = await agentClient.send(cmd);
  const body = resp.response;
  let buf = "";
  let mode = null; // "sse" | "json"

  async function emitSseFromBuffer(flushAll = false) {
    while (true) {
      const idx = buf.indexOf("\n\n");
      if (idx === -1) break;
      const event = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of event.split("\n")) {
        const t = line.trim();
        if (!t.startsWith("data: ")) continue;
        try {
          await onEvent(JSON.parse(t.slice(6)));
        } catch (_) {}
      }
    }
    if (flushAll && buf.trim().startsWith("data:")) {
      for (const line of buf.split("\n")) {
        const t = line.trim();
        if (!t.startsWith("data: ")) continue;
        try {
          await onEvent(JSON.parse(t.slice(6)));
        } catch (_) {}
      }
      buf = "";
    }
  }

  if (body && typeof body[Symbol.asyncIterator] === "function") {
    for await (const chunk of body) {
      const text = new TextDecoder().decode(chunk);
      if (mode === null) {
        buf += text;
        const stripped = buf.replace(/^\s+/, "");
        if (!stripped) continue;
        if (stripped.startsWith("data:")) {
          mode = "sse";
          buf = stripped;
          await emitSseFromBuffer(false);
        } else {
          mode = "json";
          buf = stripped;
        }
        continue;
      }
      if (mode === "json") {
        buf += text;
        continue;
      }
      buf += text;
      await emitSseFromBuffer(false);
    }
  }

  if (mode === "sse") {
    await emitSseFromBuffer(true);
    return;
  }

  const trimmed = buf.trim();
  if (!trimmed) {
    await onEvent({
      type: "RUN_ERROR",
      error: { code: "EMPTY_RESPONSE", message: "Agent returned an empty body." },
    });
    return;
  }

  // Legacy JSON { result } → AG-UI events the frontend understands.
  let text = trimmed;
  try {
    const obj = JSON.parse(trimmed);
    if (obj && typeof obj === "object" && "result" in obj) text = String(obj.result);
    else text = JSON.stringify(obj, null, 2);
  } catch (_) {}
  for (const evt of wrapPlainJson(text)) {
    await onEvent(evt);
  }
}

async function handleWebSocket(event) {
  const { requestContext } = event;
  const routeKey = requestContext.routeKey;
  const connectionId = requestContext.connectionId;

  if (routeKey === "$connect" || routeKey === "$disconnect") {
    return { statusCode: 200, body: "ok" };
  }

  const { domainName, stage } = requestContext;
  const apigw = new ApiGatewayManagementApiClient({
    region: REGION,
    endpoint: `https://${domainName}/${stage}`,
  });

  let body = {};
  try {
    body = JSON.parse(event.body || "{}");
  } catch (_) {
    body = {};
  }
  if (!authorize(event, body)) {
    await postToConnection(apigw, connectionId, {
      type: "RUN_ERROR",
      error: {
        code: "UNAUTHORIZED",
        message: "Missing or invalid api_secret / X-Cost-Agent-Secret",
      },
    });
    return { statusCode: 200, body: "ok" };
  }
  const prompt = body.prompt || body.message || "";
  if (!prompt) {
    await postToConnection(apigw, connectionId, {
      type: "RUN_ERROR",
      error: { code: "INVALID_INPUT", message: "No prompt provided" },
    });
    return { statusCode: 200, body: "ok" };
  }

  try {
    await invokeAgent(
      {
        prompt,
        sessionId: sessionIdFrom(body),
        actorId: actorIdFrom(body),
      },
      async (agEvent) => postToConnection(apigw, connectionId, agEvent)
    );
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

async function handleHTTP(event) {
  // HTTP API CORS preflight
  if (event.requestContext?.http?.method === "OPTIONS") {
    return { statusCode: 204, headers: CORS_HEADERS, body: "" };
  }

  let body = {};
  try {
    const raw = event.isBase64Encoded
      ? Buffer.from(event.body || "", "base64").toString("utf8")
      : event.body || "{}";
    body = JSON.parse(raw);
  } catch (_) {
    return {
      statusCode: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      body: JSON.stringify({ error: "invalid JSON body" }),
    };
  }

  if (!authorize(event, body)) {
    return unauthorizedJson();
  }

  const prompt = body.prompt || body.message || "";
  if (!prompt) {
    return {
      statusCode: 400,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      body: JSON.stringify({ error: "prompt or message required" }),
    };
  }

  const sessionId = sessionIdFrom(body);
  const actorId = actorIdFrom(body);
  // Workflows: {"async": true, "webhook_url": "https://..."} → accept quickly;
  // agent POSTs the full report body to webhook_url when ready.
  const wantAsync = body.async === true || body.wait === false;
  const webhookUrl = webhookUrlFrom(body);
  if (wantAsync) {
    const webhookErr = validateWebhookUrl(webhookUrl);
    if (webhookErr) {
      return {
        statusCode: 400,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
        body: JSON.stringify({ error: webhookErr }),
      };
    }
  }
  // Workflows: {"stream": false} or {"response_format": "json"} → single JSON body.
  // Chat UI keeps default SSE (stream omitted / true).
  const wantJson =
    wantAsync ||
    body.stream === false ||
    body.response_format === "json" ||
    String(headerGet(event.headers || {}, "accept")).includes("application/json");

  const events = [];
  const textParts = [];
  let runError = null;
  try {
    await invokeAgent(
      {
        prompt,
        sessionId,
        actorId,
        asyncMode: wantAsync,
        webhookUrl: webhookUrl || undefined,
      },
      async (agEvent) => {
        if (wantJson) {
          if (agEvent?.type === "TEXT_MESSAGE_CONTENT" && agEvent.delta) {
            textParts.push(String(agEvent.delta));
          } else if (agEvent?.type === "RUN_ERROR") {
            runError = agEvent.error || { message: "agent error" };
          }
          return;
        }
        events.push(`data: ${JSON.stringify(agEvent)}\n\n`);
      }
    );
  } catch (err) {
    if (wantJson) {
      runError = { code: "AGENT_INVOKE_ERROR", message: err.message || String(err) };
    } else {
      events.push(
        `data: ${JSON.stringify({
          type: "RUN_ERROR",
          error: { code: "AGENT_INVOKE_ERROR", message: err.message || String(err) },
        })}\n\n`
      );
    }
  }

  if (wantAsync) {
    return {
      statusCode: runError ? 502 : 202,
      headers: {
        "Content-Type": "application/json",
        "X-Thread-Id": sessionId,
        ...CORS_HEADERS,
      },
      body: JSON.stringify({
        accepted: !runError,
        status_code: runError ? 502 : 202,
        session_id: sessionId,
        webhook_url: webhookUrl,
        message: runError
          ? null
          : "Investigation started. Report body will POST to webhook_url when ready.",
        markdown: textParts.join(""),
        error: runError,
      }),
    };
  }

  if (wantJson) {
    const markdown = textParts.join("");
    const ok = !runError && markdown.length > 0;
    return {
      statusCode: ok ? 200 : runError ? 502 : 200,
      headers: {
        "Content-Type": "application/json",
        "X-Thread-Id": sessionId,
        ...CORS_HEADERS,
      },
      body: JSON.stringify({
        status_code: ok ? 200 : runError ? 502 : 200,
        session_id: sessionId,
        markdown,
        error: runError,
      }),
    };
  }

  return {
    statusCode: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      "X-Thread-Id": sessionId,
      ...CORS_HEADERS,
    },
    body: events.join(""),
  };
}

export const handler = async (event) => {
  if (event.requestContext?.eventType) {
    return handleWebSocket(event);
  }
  return handleHTTP(event);
};
