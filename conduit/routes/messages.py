"""POST /v1/messages — Anthropic-compatible chat endpoint."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from conduit.schema import MessageCreateRequest
from conduit.sessions import manager
from conduit.streaming import collect_non_streaming, stream_anthropic_events

router = APIRouter(prefix="/v1", tags=["messages"])


def _last_user_text(req: MessageCreateRequest) -> str:
    """Pull the last user message out of the Anthropic-format messages array."""
    for m in reversed(req.messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
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
    if isinstance(req.system, list):
        parts = [
            b["text"] for b in req.system
            if isinstance(b, dict) and b.get("type") == "text" and "text" in b
        ]
        return "\n".join(parts) if parts else None
    return None


@router.post("/messages")
async def create_message(req: MessageCreateRequest):
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
        )
        ephemeral = True

    async def _cleanup() -> None:
        if ephemeral:
            await manager.delete(session.id)

    # The model is bound at session creation. For ephemeral sessions this equals
    # req.model; for stateful ones it's whatever the session was created with.
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
