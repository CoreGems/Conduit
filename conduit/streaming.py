"""Agent SDK message stream → Anthropic-compatible SSE event stream.

The Agent SDK's `include_partial_messages=True` mode yields `StreamEvent`
objects whose `.event` is already a raw Anthropic wire-format event dict
(message_start, content_block_start, content_block_delta, content_block_stop,
message_delta, message_stop). This module forwards those events while:

  * filtering out non-text content blocks (thinking, tool_use) so callers
    see a pure chat stream,
  * renumbering text-block indices so they stay contiguous starting at 0
    after filtering,
  * gating the lifecycle so we cleanly stop after the SDK's ResultMessage.

For non-streaming requests, `collect_non_streaming` consumes the same stream
and synthesises a single Anthropic `Message` object.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
)

from conduit.sessions import Session


def _sse(event: str, data: dict) -> dict:
    """sse-starlette `EventSourceResponse` expects {'event': ..., 'data': str}."""
    return {"event": event, "data": json.dumps(data, separators=(",", ":"))}


def _rewrite_message_start(ev: dict, model: str) -> dict:
    """Force the model field to the one the caller asked for."""
    msg = dict(ev.get("message", {}))
    msg["model"] = model
    return {**ev, "message": msg}


async def stream_anthropic_events(
    session: Session,
    user_text: str,
    model: str,
) -> AsyncIterator[dict]:
    await session.client.query(user_text)

    # SDK content-block index -> {"out_index": int|None}
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
                    yield _sse(
                        "content_block_start",
                        {**ev, "index": out_idx},
                    )
                else:
                    # Skip thinking, tool_use, etc.
                    block_map[sdk_idx] = {"out_index": None}

            elif etype == "content_block_delta":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx)
                if info and info["out_index"] is not None:
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield _sse(
                            "content_block_delta",
                            {**ev, "index": info["out_index"]},
                        )

            elif etype == "content_block_stop":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx)
                if info and info["out_index"] is not None:
                    yield _sse(
                        "content_block_stop",
                        {**ev, "index": info["out_index"]},
                    )

            elif etype == "message_delta":
                yield _sse("message_delta", ev)

            elif etype == "message_stop":
                yield _sse("message_stop", ev)
                # SDK keeps emitting bookkeeping after message_stop; stop forwarding
                # but stay in the loop to let receive_response finish naturally.

        elif isinstance(msg, ResultMessage):
            session.message_count += 1
            return


async def collect_non_streaming(
    session: Session,
    user_text: str,
    model: str,
) -> dict:
    """Run one turn and return a single Anthropic Message dict (no streaming)."""
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
            # Track the latest id reported by the SDK if any
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
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
