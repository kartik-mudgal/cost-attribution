# Copyright (c) 2026 DevRev Inc. All rights reserved.
"""Local AG-UI chat server for the cost investigator.

Serves ``ui/frontend`` and proxies ``POST /invocations`` to either:
  - the deployed AgentCore Runtime (default; reads ``deploy/build/runtime.json``)
  - a local agent if ``COST_AGENT_URL`` is set (e.g. ``http://127.0.0.1:8080``)

Usage:
  # Against local agent (recommended while iterating on AG-UI):
  COST_AGENT_URL=http://127.0.0.1:8080 uv run --with boto3 python ui/local_server.py

  # Against the deployed runtime:
  uv run --with boto3 python ui/local_server.py

Then open http://127.0.0.1:8787
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3

ROOT = pathlib.Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
RUNTIME_JSON = ROOT.parent / "deploy" / "build" / "runtime.json"
HOST = os.environ.get("UI_HOST", "127.0.0.1")
PORT = int(os.environ.get("UI_PORT", "8787"))
REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))
COST_AGENT_URL = os.environ.get("COST_AGENT_URL", "").rstrip("/")
USER_ID = os.environ.get("AGENTCORE_USER_ID", "cost-investigator-local")


def _runtime_arn() -> str:
    if not RUNTIME_JSON.is_file():
        raise FileNotFoundError(
            f"Missing {RUNTIME_JSON}. Deploy the agent first, or set COST_AGENT_URL."
        )
    return json.loads(RUNTIME_JSON.read_text())["agentRuntimeArn"]


def _session_id(payload: dict) -> str:
    sid = (
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("thread_id")
        or ""
    )
    if isinstance(sid, str) and len(sid) >= 33:
        return sid
    return f"session-{uuid.uuid4().hex}"


def _prompt(payload: dict) -> str:
    if "case" in payload:
        return "Investigate this case:\n" + json.dumps(payload["case"])
    return payload.get("prompt") or payload.get("message") or ""


def _stream_local(prompt: str, session_id: str) -> Iterator[bytes]:
    body = json.dumps({"prompt": prompt}).encode()
    req = Request(
        f"{COST_AGENT_URL}/invocations",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
            "X-Amzn-Bedrock-AgentCore-Runtime-User-Id": USER_ID,
        },
        method="POST",
    )
    with urlopen(req, timeout=600) as resp:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            yield chunk


def _as_bytes(chunk: object) -> bytes:
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk)
    if isinstance(chunk, str):
        return chunk.encode()
    return bytes(chunk)


def _agui_wrap_text(text: str) -> Iterator[bytes]:
    """Turn a plain final answer into the AG-UI events the frontend expects."""
    run_id = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())

    def evt(t: str, **d: object) -> bytes:
        return f"data: {json.dumps({'type': t, 'eventId': str(uuid.uuid4()), **d})}\n\n".encode()

    yield evt("RUN_STARTED", runId=run_id)
    yield evt("TEXT_MESSAGE_START", messageId=msg_id, role="assistant")
    yield evt("TEXT_MESSAGE_CONTENT", messageId=msg_id, delta=text)
    yield evt("TEXT_MESSAGE_END", messageId=msg_id)
    yield evt("RUN_FINISHED", runId=run_id)


def _maybe_wrap_payload(data: bytes) -> Iterator[bytes]:
    """Pass through SSE; wrap legacy JSON ``{result: ...}`` as AG-UI events."""
    stripped = data.lstrip()
    if not stripped:
        yield from _agui_wrap_text("(empty response from runtime)")
        return
    if stripped.startswith(b"data:"):
        yield data
        return
    try:
        obj = json.loads(stripped)
    except Exception:
        yield from _agui_wrap_text(stripped.decode(errors="replace"))
        return
    if isinstance(obj, dict) and "result" in obj:
        yield from _agui_wrap_text(str(obj["result"]))
        return
    yield from _agui_wrap_text(json.dumps(obj, indent=2))


def _stream_runtime(prompt: str, session_id: str) -> Iterator[bytes]:
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=_runtime_arn(),
        runtimeSessionId=session_id,
        runtimeUserId=USER_ID,
        payload=json.dumps(
            {"prompt": prompt, "actor_id": USER_ID, "session_id": session_id}
        ).encode(),
    )
    body = resp["response"]
    # Always buffer: current deployed runtime returns JSON, not SSE. StreamingBody
    # still exposes iter_chunks, so we must not passthrough those bytes blindly.
    chunks: list[bytes] = []
    if hasattr(body, "iter_chunks"):
        for chunk in body.iter_chunks():
            if chunk:
                chunks.append(_as_bytes(chunk))
    else:
        data = body.read()
        if data:
            chunks.append(_as_bytes(data))
    yield from _maybe_wrap_payload(b"".join(chunks))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[ui] {self.address_string()} {fmt % args}")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS, GET")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/invocations":
            self.send_error(404, "Not found")
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self.send_error(400, "invalid JSON")
            return
        prompt = _prompt(payload)
        if not prompt:
            self.send_error(400, "prompt or message required")
            return
        session_id = _session_id(payload)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Thread-Id", session_id)
        self.end_headers()
        try:
            stream = (
                _stream_local(prompt, session_id)
                if COST_AGENT_URL
                else _stream_runtime(prompt, session_id)
            )
            for chunk in stream:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (HTTPError, URLError, Exception) as e:
            err = {
                "type": "RUN_ERROR",
                "error": {"code": "PROXY_ERROR", "message": str(e)},
            }
            self.wfile.write(f"data: {json.dumps(err)}\n\n".encode())
            self.wfile.flush()


def main() -> None:
    target = COST_AGENT_URL or f"AgentCore Runtime ({RUNTIME_JSON})"
    print(f"Serving chat UI on http://{HOST}:{PORT}")
    print(f"Proxy /invocations → {target}")
    print("Use the SSE transport toggle in the UI (default).")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
