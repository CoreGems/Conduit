"""POST /v1/messages — Anthropic-compatible chat endpoint.

Routes traffic between three modes:

  * **plain chat**   — no `tools` declared. Uses the original synchronous
    streaming path (`stream_anthropic_events` / `collect_non_streaming`).
  * **tool-use new turn**  — `tools` declared, no resume trailer. Auto-
    allocates a session, surfaces `x-conduit-session-id` header.
  * **tool-use resume**    — `tools` declared, request ends in user message
    whose content list contains `tool_result` blocks. Looks up the
    pending Futures by `tool_use_id`, resolves them, and continues
    streaming events from the same session.
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from conduit.schema import MessageCreateRequest
from conduit.sessions import manager
from conduit.streaming import (
    collect_non_streaming,
    stream_anthropic_events,
    stream_tool_events,
)
from conduit.tool_bridge import split_tools

router = APIRouter(prefix="/v1", tags=["messages"])


# ---------------------------------------------------------------------------
# small request helpers
# ---------------------------------------------------------------------------

def _materialize_content(content) -> list | str | None:
    """pydantic v2 returns a lazy ValidatorIterator for the message content
    field — eagerly materialise it to a list of dicts (or keep a str)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    try:
        return list(content)
    except TypeError:
        return None


def _last_user_text(req: MessageCreateRequest) -> str:
    for m in reversed(list(req.messages)):
        if m.get("role") != "user":
            continue
        content = _materialize_content(m.get("content"))
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                b["text"] for b in content
                if isinstance(b, dict) and b.get("type") == "text" and "text" in b
            ]
            if parts:
                return "\n".join(parts)
    raise HTTPException(400, detail={"type": "invalid_request_error", "message": "no user text content in messages"})


def _system_prompt_text(req: MessageCreateRequest) -> str | None:
    if isinstance(req.system, str):
        return req.system
    sys = _materialize_content(req.system) if req.system is not None else None
    if isinstance(sys, list):
        parts = [
            b["text"] for b in sys
            if isinstance(b, dict) and b.get("type") == "text" and "text" in b
        ]
        return "\n".join(parts) if parts else None
    return None


def _trailing_tool_results(req: MessageCreateRequest) -> list[dict] | None:
    """If the last message is a user message whose content list contains
    `tool_result` blocks, return those blocks. Otherwise None.
    """
    messages = list(req.messages) if req.messages else []
    if not messages:
        return None
    last = messages[-1]
    if last.get("role") != "user":
        return None
    content = _materialize_content(last.get("content"))
    if not isinstance(content, list):
        return None
    results = [
        b for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    return results or None


# ---------------------------------------------------------------------------
# tool-use non-streaming: collect the SSE stream into a single Message dict
# ---------------------------------------------------------------------------

async def _collect_tool_message(session, user_text, model) -> dict:
    msg: dict = {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    blocks: dict[int, dict] = {}

    async for sse in stream_tool_events(session, user_text, model):
        data = json.loads(sse["data"])
        t = data.get("type")
        if t == "message_start":
            m = data["message"]
            msg["id"] = m.get("id", msg["id"])
            msg["model"] = m.get("model", model)
            msg["usage"]["input_tokens"] = (m.get("usage") or {}).get("input_tokens", 0)
        elif t == "content_block_start":
            idx = data["index"]
            blocks[idx] = dict(data["content_block"])
            # Both custom tool_use and hosted server_tool_use stream their input
            # as input_json_delta chunks that we reassemble.
            if blocks[idx].get("type") in ("tool_use", "server_tool_use"):
                blocks[idx].setdefault("input", {})
                blocks[idx]["_input_partial"] = ""
        elif t == "content_block_delta":
            idx = data["index"]
            delta = data["delta"]
            b = blocks.get(idx)
            if b is None:
                continue
            if delta["type"] == "text_delta":
                b["text"] = b.get("text", "") + delta["text"]
            elif delta["type"] == "input_json_delta":
                b["_input_partial"] = b.get("_input_partial", "") + delta["partial_json"]
            elif delta["type"] == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + delta.get("thinking", "")
            elif delta["type"] == "signature_delta":
                b["signature"] = (b.get("signature", "") or "") + delta.get("signature", "")
        elif t == "content_block_stop":
            idx = data["index"]
            b = blocks.get(idx) or {}
            if b.get("type") in ("tool_use", "server_tool_use"):
                try:
                    b["input"] = json.loads(b.pop("_input_partial", "") or "{}")
                except json.JSONDecodeError:
                    b["input"] = {}
                    b.pop("_input_partial", None)
            # web_search_tool_result / web_fetch_tool_result blocks come with
            # their content already populated at content_block_start time
            # (synthesized from the SDK's UserMessage) — nothing to reassemble.
        elif t == "message_delta":
            d = data.get("delta") or {}
            msg["stop_reason"] = d.get("stop_reason", msg["stop_reason"])
            msg["stop_sequence"] = d.get("stop_sequence")
            u = data.get("usage") or {}
            if "output_tokens" in u:
                msg["usage"]["output_tokens"] = u["output_tokens"]
        # message_stop ends the iteration via stream_tool_events itself

    msg["content"] = [blocks[i] for i in sorted(blocks.keys())]
    return msg


# ---------------------------------------------------------------------------
# main route
# ---------------------------------------------------------------------------

@router.post("/messages")
async def create_message(req: MessageCreateRequest):
    has_tools = bool(req.tools)

    if has_tools:
        return await _handle_tool_use_request(req)

    # ----- plain chat path (unchanged) -----------------------------------
    user_text = _last_user_text(req)

    if req.session_id:
        session = await manager.get(req.session_id)
        if session is None:
            raise HTTPException(
                404,
                detail={"type": "invalid_request_error", "message": f"session {req.session_id} not found"},
            )
        ephemeral = False
    else:
        session = await manager.create(
            system_prompt=_system_prompt_text(req),
            model=req.model,
            effort=req.effort,
            include_thinking=req.include_thinking,
        )
        ephemeral = True

    async def _cleanup() -> None:
        if ephemeral:
            await manager.delete(session.id)

    effective_model = session.model or req.model

    if req.stream:
        async def event_source():
            try:
                async with session.lock:
                    async for ev in stream_anthropic_events(session, user_text, effective_model):
                        yield ev
            finally:
                await _cleanup()

        return EventSourceResponse(event_source())

    try:
        async with session.lock:
            return await collect_non_streaming(session, user_text, effective_model)
    finally:
        await _cleanup()


async def _handle_tool_use_request(req: MessageCreateRequest):
    # Split client's `tools` array: custom (need bridge + pause/resume) vs
    # hosted (SDK-executed: WebSearch, WebFetch).
    custom_tools, hosted_sdk_tools = split_tools(list(req.tools or []))
    has_custom = bool(custom_tools)
    has_hosted = bool(hosted_sdk_tools)

    tool_results = _trailing_tool_results(req)
    is_resume = tool_results is not None and req.session_id is not None

    if tool_results is not None and req.session_id is None:
        raise HTTPException(
            400,
            detail={"type": "invalid_request_error",
                    "message": "tool_result requires session_id (returned via x-conduit-session-id header on the initial response)"},
        )

    if req.session_id:
        session = await manager.get(req.session_id)
        if session is None:
            raise HTTPException(
                404,
                detail={"type": "invalid_request_error",
                        "message": f"session {req.session_id} not found (may have timed out)"},
            )
        if not session.uses_pump:
            raise HTTPException(
                400,
                detail={"type": "invalid_request_error",
                        "message": f"session {req.session_id} was not created with tools"},
            )
        ephemeral = False
    else:
        session = await manager.create(
            system_prompt=_system_prompt_text(req),
            model=req.model,
            tools_spec=custom_tools or None,
            hosted_sdk_tools=hosted_sdk_tools or None,
            effort=req.effort,
            include_thinking=req.include_thinking,
        )
        # Hosted-only sessions don't need pause/resume — clean up after the
        # response completes, same as a stateless plain-chat ephemeral.
        ephemeral = not has_custom

    effective_model = session.model or req.model

    # Deliver tool_results if this is a resume.
    if is_resume:
        for block in tool_results:  # type: ignore[union-attr]
            tool_use_id = block.get("tool_use_id")
            if not tool_use_id:
                raise HTTPException(
                    400,
                    detail={"type": "invalid_request_error", "message": "tool_result missing tool_use_id"},
                )
            if tool_use_id not in session.pending_futures:
                raise HTTPException(
                    400,
                    detail={"type": "invalid_request_error", "message": f"unknown tool_use_id: {tool_use_id}"},
                )
        for block in tool_results:  # type: ignore[union-attr]
            session.deliver_tool_result(block["tool_use_id"], block.get("content"))
        user_text: str | None = None  # don't call query() on resume
    else:
        user_text = _last_user_text(req)

    headers = {"x-conduit-session-id": session.id}

    async def _cleanup() -> None:
        if ephemeral:
            await manager.delete(session.id)

    if req.stream:
        async def event_source():
            try:
                async for ev in stream_tool_events(session, user_text, effective_model):
                    yield ev
            finally:
                await _cleanup()

        return EventSourceResponse(event_source(), headers=headers)

    try:
        body = await _collect_tool_message(session, user_text, effective_model)
        return JSONResponse(content=body, headers=headers)
    finally:
        await _cleanup()
