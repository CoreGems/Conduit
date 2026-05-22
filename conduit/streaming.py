"""Agent SDK message stream → Anthropic-compatible SSE event stream.

Two stream functions:

  * ``stream_anthropic_events`` / ``collect_non_streaming`` — plain chat
    (no tools). Iterates ``receive_response()`` inline under the session
    lock. Existing behaviour, unchanged.

  * ``stream_tool_events`` — tool-use sessions. Uses a per-session
    background pump that drains ``receive_response()`` into a queue so
    that the HTTP response can complete after the SDK pauses inside a
    `@tool` handler. The next HTTP request resumes consumption from the
    same queue.

Both functions emit byte-identical Anthropic SSE wire format.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from conduit.sessions import Session, SessionState
from conduit.tool_bridge import local_name_from_full


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, separators=(",", ":"))}


def _rewrite_message_start(ev: dict, model: str) -> dict:
    msg = dict(ev.get("message", {}))
    msg["model"] = model
    return {**ev, "message": msg}


def _clean_tool_use_block(content_block: dict) -> dict:
    """Rewrite SDK-prefixed tool name back to its bare form and drop
    SDK-internal fields (`caller`, etc.) that aren't part of Anthropic's
    documented `tool_use` block shape."""
    out = dict(content_block)
    name = out.get("name")
    if isinstance(name, str):
        local = local_name_from_full(name)
        if local is not None:
            out["name"] = local
    # SDK adds fields like `caller` that aren't in the Anthropic wire format
    for k in ("caller",):
        out.pop(k, None)
    return out


# ---------------------------------------------------------------------------
# Plain-chat path (unchanged)
# ---------------------------------------------------------------------------

async def stream_anthropic_events(
    session: Session,
    user_text: str,
    model: str,
) -> AsyncIterator[dict]:
    await session.client.query(user_text)

    block_map: dict[int, dict[str, Any]] = {}
    next_out_index = 0
    sent_message_start = False

    async for msg in session.client.receive_response():
        if isinstance(msg, StreamEvent):
            ev = msg.event
            etype = ev.get("type")

            if etype == "message_start":
                if not sent_message_start:
                    yield _sse("message_start", _rewrite_message_start(ev, model))
                    sent_message_start = True

            elif etype == "content_block_start":
                sdk_idx = ev.get("index", 0)
                block_type = (ev.get("content_block") or {}).get("type")
                if block_type == "text":
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx}
                    yield _sse("content_block_start", {**ev, "index": out_idx})
                else:
                    block_map[sdk_idx] = {"out_index": None}

            elif etype == "content_block_delta":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx)
                if info and info["out_index"] is not None:
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield _sse("content_block_delta", {**ev, "index": info["out_index"]})

            elif etype == "content_block_stop":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx)
                if info and info["out_index"] is not None:
                    yield _sse("content_block_stop", {**ev, "index": info["out_index"]})

            elif etype == "message_delta":
                yield _sse("message_delta", ev)

            elif etype == "message_stop":
                yield _sse("message_stop", ev)

        elif isinstance(msg, ResultMessage):
            session.message_count += 1
            return


async def collect_non_streaming(
    session: Session,
    user_text: str,
    model: str,
) -> dict:
    await session.client.query(user_text)

    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    text_blocks: list[dict] = []
    stop_reason: str | None = None
    input_tokens = 0
    output_tokens = 0

    async for msg in session.client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_blocks.append({"type": "text", "text": block.text})
            sdk_msg_id = getattr(msg, "message_id", None)
            if sdk_msg_id:
                message_id = sdk_msg_id
        elif isinstance(msg, StreamEvent):
            ev = msg.event
            if ev.get("type") == "message_delta":
                delta = ev.get("delta") or {}
                stop_reason = delta.get("stop_reason") or stop_reason
                usage = ev.get("usage") or {}
                output_tokens = int(usage.get("output_tokens", output_tokens))
                input_tokens = int(usage.get("input_tokens", input_tokens))
            elif ev.get("type") == "message_start":
                usage = (ev.get("message") or {}).get("usage") or {}
                input_tokens = int(usage.get("input_tokens", input_tokens))
        elif isinstance(msg, ResultMessage):
            session.message_count += 1
            stop_reason = stop_reason or getattr(msg, "stop_reason", None) or "end_turn"
            break

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": text_blocks,
        "stop_reason": stop_reason or "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


# ---------------------------------------------------------------------------
# Tool-use path: pump + pause/resume
# ---------------------------------------------------------------------------

_PUMP_END_SENTINEL: Any = object()


async def _pump_session(session: Session) -> None:
    """Drain receive_response() into the session's event queue.

    Runs until the SDK's async generator returns (i.e. the multi-turn
    turn-with-tools is complete) or until cancelled by session deletion.
    """
    try:
        async for msg in session.client.receive_response():
            await session.events_queue.put(msg)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await session.events_queue.put(("ERROR", e))
    finally:
        await session.events_queue.put(_PUMP_END_SENTINEL)


async def stream_tool_events(
    session: Session,
    user_text: str | None,
    model: str,
) -> AsyncIterator[dict]:
    """Stream a turn (initial or resume) for a tool-use session.

    Args:
      user_text:
        - For an **initial** request, the new user prompt; we call query()
          to kick off the SDK turn before draining the queue.
        - For a **resume** request, pass None. The routes layer has already
          delivered the tool_result(s) to the parked Future(s); the pump is
          continuing to produce events.

    Stops streaming when:
      - We see `message_stop` after `stop_reason: tool_use`  → pause
      - We see `message_stop` after a terminal stop_reason   → done
      - The pump emits its end sentinel                       → done
    """
    if session.events_queue is None:
        raise RuntimeError("stream_tool_events called on non-tool session")

    # Make sure the pump is running. Start it lazily on first use.
    if session.pump_task is None or session.pump_task.done():
        session.pump_task = asyncio.create_task(_pump_session(session))

    # Kick off a fresh SDK turn if this is the initial request.
    if user_text is not None:
        await session.client.query(user_text)

    session.state = SessionState.STREAMING

    block_map: dict[int, dict[str, Any]] = {}
    next_out_index = 0
    last_stop_reason: str | None = None

    while True:
        msg = await session.events_queue.get()
        if msg is _PUMP_END_SENTINEL:
            session.state = SessionState.IDLE
            return
        if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "ERROR":
            session.state = SessionState.IDLE
            raise msg[1]

        if isinstance(msg, StreamEvent):
            ev = msg.event
            etype = ev.get("type")

            if etype == "message_start":
                # Reset per-message renumbering state
                block_map.clear()
                next_out_index = 0
                last_stop_reason = None
                yield _sse("message_start", _rewrite_message_start(ev, model))

            elif etype == "content_block_start":
                sdk_idx = ev.get("index", 0)
                cb = ev.get("content_block") or {}
                block_type = cb.get("type")

                if block_type == "text":
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": "text"}
                    yield _sse("content_block_start", {**ev, "index": out_idx})

                elif block_type == "tool_use":
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": "tool_use"}
                    cb_clean = _clean_tool_use_block(cb)
                    local_name = cb_clean.get("name")
                    tool_use_id = cb.get("id")
                    if local_name and tool_use_id:
                        session.record_pending_id(local_name, tool_use_id)
                    yield _sse(
                        "content_block_start",
                        {**ev, "index": out_idx, "content_block": cb_clean},
                    )

                else:
                    # thinking, etc. — drop the whole block
                    block_map[sdk_idx] = {"out_index": None, "type": block_type}

            elif etype == "content_block_delta":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx) or {}
                if info.get("out_index") is None:
                    continue
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype in ("text_delta", "input_json_delta"):
                    yield _sse("content_block_delta", {**ev, "index": info["out_index"]})

            elif etype == "content_block_stop":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx) or {}
                if info.get("out_index") is not None:
                    yield _sse("content_block_stop", {**ev, "index": info["out_index"]})

            elif etype == "message_delta":
                last_stop_reason = (ev.get("delta") or {}).get("stop_reason")
                yield _sse("message_delta", ev)

            elif etype == "message_stop":
                yield _sse("message_stop", ev)
                if last_stop_reason == "tool_use":
                    session.state = SessionState.PAUSED_FOR_TOOLS
                    return
                if last_stop_reason in ("end_turn", "stop_sequence", "max_tokens"):
                    session.state = SessionState.IDLE
                    return

        elif isinstance(msg, ResultMessage):
            session.message_count += 1
            session.state = SessionState.IDLE
            return

        # Other message types (HookEventMessage, SystemMessage, AssistantMessage,
        # RateLimitEvent, UserMessage) are not part of the Anthropic wire format
        # for chat — drop silently.
