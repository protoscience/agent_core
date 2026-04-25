"""WhatsApp bridge — OpenAI-compatible chat-completions API.

Designed to sit behind OpenClaw (a WhatsApp gateway running on a VPS)
that POSTs the conversation to /v1/chat/completions on every turn.

Sessions are keyed by a stable per-peer hash so each WhatsApp sender
gets their own ClaudeSDKClient with the right per-sender memory.

Public API:
    run_whatsapp_bridge(build_opts, *, port=4000, token=None,
                        session_max_age_seconds=12*3600,
                        confirm_callback=None)

`build_opts` is `Callable[[*, sender_key, sender_name], ClaudeAgentOptions]`
— the framework calls it once per new session.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from typing import Callable, Awaitable

from claude_agent_sdk import (
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ResultMessage,
    ClaudeAgentOptions,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

from agent_core.context import IMAGE_MARKER
from agent_core.tools import cost_log
from agent_core.tools.confirm import confirm_callback as confirm_cb_ctx, deny_confirm


log = logging.getLogger("agent-core.bridge")


BuildOptsFn = Callable[..., ClaudeAgentOptions]
ConfirmFn = Callable[[str], Awaitable[bool]]


class _Message(BaseModel):
    role: str
    content: str | list | None = None


class _ChatRequest(BaseModel):
    model: str | None = None
    messages: list[_Message]
    user: str | None = None
    stream: bool | None = False


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "".join(parts)
    return ""


def _derive_peer_key(req: _ChatRequest) -> str | None:
    """Stable per-caller identity.

    Priority:
      1. explicit `user` field (forwarded by some gateways, useful for curl).
      2. SHA-256 of the first user message — gateways like OpenClaw replay
         the full history per WhatsApp sender on every request, so messages[0]
         is stable per sender.
    """
    if req.user and req.user.strip():
        return req.user.strip()
    user_msgs = [m for m in req.messages if m.role == "user"]
    if not user_msgs:
        return None
    first = _content_to_text(user_msgs[0].content).strip()
    if not first:
        return None
    return "wa-" + hashlib.sha256(first.encode("utf-8")).hexdigest()[:16]


def run_whatsapp_bridge(
    build_opts: BuildOptsFn,
    *,
    port: int = 4000,
    token: str | None = None,
    session_max_age_seconds: int = 12 * 60 * 60,
    confirm_callback: ConfirmFn | None = None,
    model_id: str = "agent-core",
    log_channel: str = "wa",
) -> None:
    """Run the WhatsApp bridge server (blocks until killed).

    Args:
        build_opts: Per-session options factory. Called as
            `build_opts(sender_key=..., sender_name=...)` for each new peer.
        port: TCP port to bind on 127.0.0.1. Default 4000.
        token: Bearer token required on /v1/chat/completions. If None, falls
            back to the BRIDGE_TOKEN env var. The server refuses to start
            without one.
        session_max_age_seconds: Idle-session TTL. Default 12h.
        confirm_callback: Async callback for `place_order`-style tools that
            need human approval. The default denies all confirmations
            (WhatsApp has no UI for confirming risky actions).
        model_id: Value reported by GET /v1/models. Cosmetic.
        log_channel: Tag passed to cost_log.log_turn(). Default "wa".
    """
    if token is None:
        token = os.environ.get("BRIDGE_TOKEN", "")
    if not token:
        log.error("run_whatsapp_bridge: no token (pass token= or set BRIDGE_TOKEN)")
        sys.exit(1)

    if confirm_callback is None:
        confirm_callback = deny_confirm

    sessions: dict[str, ClaudeSDKClient] = {}
    session_meta: dict[str, dict] = {}
    locks: dict[str, asyncio.Lock] = {}

    app = FastAPI()

    async def _expire_session(key: str):
        if key in sessions:
            try:
                await sessions[key].disconnect()
            except Exception:
                pass
            sessions.pop(key, None)
            session_meta.pop(key, None)
            log.info(f"Expired session for peer={key[:8]}...")

    async def _sweep_idle_sessions():
        while True:
            await asyncio.sleep(300)
            now = time.time()
            for key in list(sessions):
                meta = session_meta.get(key, {})
                if now - meta.get("last_used", 0) <= session_max_age_seconds:
                    continue
                lock = locks.get(key)
                if lock is not None and lock.locked():
                    continue
                await _expire_session(key)

    async def _get_session(key: str) -> ClaudeSDKClient:
        meta = session_meta.get(key, {})
        idle = time.time() - meta.get("last_used", 0)
        if key in sessions and idle > session_max_age_seconds:
            await _expire_session(key)

        if key not in sessions:
            options = build_opts(sender_key=key, sender_name=None)
            client = ClaudeSDKClient(options=options)
            await client.connect()
            sessions[key] = client
            session_meta[key] = {"last_used": time.time(), "turns": 0}
            log.info(f"Created session for peer={key[:8]}...")
        else:
            session_meta[key]["last_used"] = time.time()
            session_meta[key]["turns"] += 1
        return sessions[key]

    def _get_lock(key: str) -> asyncio.Lock:
        if key not in locks:
            locks[key] = asyncio.Lock()
        return locks[key]

    @app.on_event("startup")
    async def _start_sweeper():
        asyncio.create_task(_sweep_idle_sessions())

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat_completions(req: _ChatRequest, request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != token:
            raise HTTPException(status_code=401, detail="unauthorized")

        key = _derive_peer_key(req)
        if not key:
            raise HTTPException(status_code=400, detail="unable to derive caller identity")

        log.info(f"Request: peer={key[:8]}... msgs={len(req.messages)} stream={req.stream}")

        user_msgs = [m for m in req.messages if m.role == "user"]
        if not user_msgs:
            raise HTTPException(status_code=400, detail="no user message")
        latest = _content_to_text(user_msgs[-1].content)

        lock = _get_lock(key)
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model_name = req.model or model_id
        now = int(time.time())

        def _delta_chunk(delta: dict) -> str:
            chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": model_name,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            return f"data: {json.dumps(chunk)}\n\n"

        if req.stream:
            async def _stream():
                yield _delta_chunk({"role": "assistant"})
                line_buf = ""
                total_chars = 0
                result_msg = None
                async with lock:
                    confirm_cb_ctx.set(confirm_callback)
                    client = await _get_session(key)
                    await client.query(latest)
                    async for msg in client.receive_response():
                        if isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    line_buf += block.text
                                    while "\n" in line_buf:
                                        line, line_buf = line_buf.split("\n", 1)
                                        if IMAGE_MARKER in line:
                                            continue
                                        out = line + "\n"
                                        total_chars += len(out)
                                        yield _delta_chunk({"content": out})
                                elif isinstance(block, ToolUseBlock):
                                    log.info(f"tool: {block.name}")
                                    yield f": tool {block.name}\n\n"
                        elif isinstance(msg, ResultMessage):
                            result_msg = msg
                            break
                if line_buf and IMAGE_MARKER not in line_buf:
                    total_chars += len(line_buf)
                    yield _delta_chunk({"content": line_buf})
                if total_chars == 0:
                    yield _delta_chunk({"content": "(no reply)"})
                cost = (result_msg.total_cost_usd or 0) if result_msg else 0
                turns = result_msg.num_turns if result_msg else 0
                log.info(f"Reply: peer={key[:8]}... chars={total_chars} turns={turns} cost=${cost:.4f} (stream)")
                try:
                    cost_log.log_turn(log_channel, key, turns, cost, getattr(result_msg, "usage", None))
                except Exception:
                    log.exception("cost_log failed")
                done = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": now,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(done)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_stream(), media_type="text/event-stream")

        async with lock:
            confirm_cb_ctx.set(confirm_callback)
            client = await _get_session(key)
            await client.query(latest)
            text_buf = ""
            result_msg = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_buf += block.text
                        elif isinstance(block, ToolUseBlock):
                            log.info(f"tool: {block.name}")
                elif isinstance(msg, ResultMessage):
                    result_msg = msg
                    break
            reply = "\n".join(l for l in text_buf.splitlines() if IMAGE_MARKER not in l).strip()
            if not reply:
                reply = "(no reply)"

        cost = (result_msg.total_cost_usd or 0) if result_msg else 0
        turns = result_msg.num_turns if result_msg else 0
        log.info(f"Reply: peer={key[:8]}... chars={len(reply)} turns={turns} cost=${cost:.4f}")
        try:
            cost_log.log_turn(log_channel, key, turns, cost)
        except Exception:
            log.exception("cost_log failed")

        return {
            "id": cmpl_id,
            "object": "chat.completion",
            "created": now,
            "model": model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [{"id": model_id, "object": "model", "owned_by": "local"}],
        }

    @app.get("/health")
    async def health():
        return {"ok": True}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
