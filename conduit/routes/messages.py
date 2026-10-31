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

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse  # noqa: F401  (used in route)

from conduit.schema import MessageCreateRequest
from conduit.sessions import manager
from conduit.streaming import (
    collect_message_from_sse,
    collect_non_streaming,
    guard_sse,
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


# Block types that carry user prompt data and pass through to the SDK.
# `document` blocks (text / PDF / custom-content sources, with or without
# `citations: {enabled: true}`) are forwarded verbatim: the Agent SDK hands
# them to the API unchanged and cited answers come back as `citations_delta`.
_PROMPT_BLOCK_TYPES = ("text", "image", "document")


def _prompt_blocks(content: list) -> list[dict]:
    return [
        b for b in content
        if isinstance(b, dict) and b.get("type") in _PROMPT_BLOCK_TYPES
    ]


def _has_rich_blocks(blocks: list[dict]) -> bool:
    """True if the turn carries anything that can't be flattened to a string
    (image or document blocks) and must reach the SDK in content-block form."""
    return any(b.get("type") != "text" for b in blocks)


def _last_user_content(req: MessageCreateRequest) -> str | list[dict]:
    """Prompt for the stateful (session_id) path: the newest user turn.

    Text-only turns flatten to a plain string (the SDK's string fast path).
    A turn carrying image or document blocks returns its block list, passed
    through to the SDK verbatim.
    """
    for m in reversed(list(req.messages)):
        if m.get("role") != "user":
            continue
        content = _materialize_content(m.get("content"))
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            blocks = _prompt_blocks(content)
            if _has_rich_blocks(blocks):
                return blocks
            parts = [b["text"] for b in blocks if b.get("type") == "text" and "text" in b]
            if parts:
                return "\n".join(parts)
    raise HTTPException(400, detail={"type": "invalid_request_error", "message": "no user text, image or document content in messages"})


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


def _text_of_content(content) -> str:
    """Flatten a message content (str OR list of blocks) to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"] for b in content
            if isinstance(b, dict) and b.get("type") == "text" and "text" in b
        ]
        return "\n".join(parts)
    return ""


def _build_replay_prompt(req: MessageCreateRequest) -> str | list[dict]:
    """For stateless multi-turn (Pattern A): the SDK session is fresh and has
    no context, so concatenate the conversation history into the user prompt
    we send. Single-turn requests fall back to just the user text.

    A final user turn carrying image or document blocks is returned as a
    content-block list, with the flattened history (if any) prepended as a
    text block. Images and documents in *earlier* turns are dropped along
    with every other non-text block — only the newest turn's reach the model.
    """
    msgs = list(req.messages)
    if not msgs:
        raise HTTPException(400, detail={"type": "invalid_request_error", "message": "messages array is empty"})

    # History = everything before the final message, flattened to text.
    history_lines: list[str] = []
    for m in msgs[:-1]:
        role = m.get("role", "")
        text = _text_of_content(m.get("content"))
        if not text:
            continue
        if role == "user":
            history_lines.append(f"User: {text}")
        elif role == "assistant":
            history_lines.append(f"Assistant: {text}")
    history = "\n\n".join(history_lines)

    last = msgs[-1]
    if last.get("role") != "user":
        raise HTTPException(400, detail={"type": "invalid_request_error", "message": "last message must be from the user"})
    last_content = _materialize_content(last.get("content"))

    if isinstance(last_content, list):
        blocks = _prompt_blocks(last_content)
        if _has_rich_blocks(blocks):
            if history:
                return [{"type": "text", "text": f"{history}\n\nUser:"}] + blocks
            return blocks

    last_text = _text_of_content(last_content)
    if not last_text:
        raise HTTPException(400, detail={"type": "invalid_request_error", "message": "no user text, image or document content in final message"})

    if not history:
        return last_text
    return f"{history}\n\nUser: {last_text}"


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

async def _collect_tool_message(session, user_content, model) -> dict:
    return await collect_message_from_sse(
        stream_tool_events(session, user_content, model), model
    )


# ---------------------------------------------------------------------------
# main route
# ---------------------------------------------------------------------------

@router.post("/messages")
async def create_message(req: MessageCreateRequest):
    has_tools = bool(req.tools)

    if has_tools:
        return await _handle_tool_use_request(req)

    # ----- plain chat path -----------------------------------------------
    if req.session_id:
        # Stateful: server owns history; client sends only the new user turn.
        session = await manager.get(req.session_id)
        if session is None:
            raise HTTPException(
                404,
                detail={"type": "invalid_request_error", "message": f"session {req.session_id} not found"},
            )
        user_content = _last_user_content(req)
    else:
        # Stateless / auto-allocated: create a fresh session and replay the
        # client's full message history into the prompt so the model has
        # context. Sessions are non-ephemeral now — the client can reuse the
        # `x-conduit-session-id` header value on subsequent turns to switch
        # to Pattern B (server-managed history). Idle sweeper reaps unused.
        session = await manager.create(
            system_prompt=_system_prompt_text(req),
            model=req.model,
            effort=req.resolved_effort,
            include_thinking=req.include_thinking,
            output_format=req.resolved_output_format,
        )
        user_content = _build_replay_prompt(req)

    effective_model = session.model or req.model
    headers = {"x-conduit-session-id": session.id}

    if req.stream:
        async def event_source():
            async with session.lock:
                # guard_sse: the response head is already sent once we're
                # iterating, so failures must go out as an `error` event.
                async for ev in guard_sse(stream_anthropic_events(session, user_content, effective_model)):
                    yield ev

        return EventSourceResponse(event_source(), headers=headers)

    async with session.lock:
        body = await collect_non_streaming(session, user_content, effective_model)
    return JSONResponse(content=body, headers=headers)


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
            effort=req.resolved_effort,
            include_thinking=req.include_thinking,
            output_format=req.resolved_output_format,
        )
        # All tool-use sessions are kept alive after the response — client may
        # want to reuse the session_id from the x-conduit-session-id header for
        # follow-up turns (works for both custom and hosted-only tool sessions).
        # The idle sweeper reaps unused ones after CONDUIT_SESSION_IDLE_TIMEOUT_S.

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
            # is_pending_tool_use_id covers both the "Future is live" case and
            # the "handler hasn't fired yet" case (parallel tools). The id
            # must have been observed by the stream layer for this session.
            if not session.is_pending_tool_use_id(tool_use_id):
                raise HTTPException(
                    400,
                    detail={"type": "invalid_request_error", "message": f"unknown tool_use_id: {tool_use_id}"},
                )
        for block in tool_results:  # type: ignore[union-attr]
            session.deliver_tool_result(block["tool_use_id"], block.get("content"))
        user_content: str | list[dict] | None = None  # don't call query() on resume
    else:
        # Choose user_content the same way plain chat does:
        #   - With session_id (Pattern B): session has state, only need new turn.
        #   - Without session_id (Pattern A): replay client's full history into prompt.
        if req.session_id:
            user_content = _last_user_content(req)
        else:
            user_content = _build_replay_prompt(req)

    headers = {"x-conduit-session-id": session.id}

    async def _drain_trailing_pump() -> None:
        """After the HTTP response ends, give the pump a brief window to
        finish draining receive_response() (e.g. the trailing ResultMessage
        after end_turn). This keeps the session in a clean state so the next
        turn can start a fresh receive_response iterator. If the pump is
        actually paused for tools (pending Futures present), don't wait —
        that pump is supposed to stay alive across HTTP requests."""
        import asyncio
        if session.pending_futures:
            return
        if session.pump_task is None or session.pump_task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(session.pump_task), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, BaseException):
            pass

    if req.stream:
        async def event_source():
            try:
                # guard_sse: the response head is already sent once we're
                # iterating, so failures must go out as an `error` event.
                async for ev in guard_sse(stream_tool_events(session, user_content, effective_model)):
                    yield ev
            finally:
                await _drain_trailing_pump()

        return EventSourceResponse(event_source(), headers=headers)

    try:
        body = await _collect_tool_message(session, user_content, effective_model)
    finally:
        await _drain_trailing_pump()
    return JSONResponse(content=body, headers=headers)
