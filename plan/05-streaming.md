# Step 5 — Streaming translator

## Goal
Translate the Agent SDK's async message stream into Anthropic's exact SSE
event sequence:

```
message_start
  content_block_start (index 0, type=text)
    content_block_delta (text_delta) × N
  content_block_stop (index 0)
message_delta (stop_reason, usage.output_tokens)
message_stop
```

## Files
- `conduit/streaming.py`

## SDK behavior (verify against installed SDK before implementing)
`ClaudeSDKClient.receive_response()` yields message objects:
- `UserMessage`         — echo of what we sent (skip)
- `AssistantMessage`    — has `content: list[ContentBlock]`; for pure chat,
                         each block is `TextBlock(text=str)`
- `SystemMessage`       — metadata (skip for now)
- `ResultMessage`       — final, has `usage`, `total_cost_usd`, etc.

**Open question:** does the SDK emit partial `AssistantMessage`s for token
streaming, or only complete ones? Run the smoke script at the bottom of this
file first. Adjust the translator accordingly:
- If complete-only → emit a single `content_block_delta` per AssistantMessage
  text block. Still streams; just larger chunks.
- If partial deltas → emit one `content_block_delta` per chunk.

## Implementation (assumes complete-message yields; safe baseline)

```python
# conduit/streaming.py
from __future__ import annotations

import json
import time
import uuid
from typing import AsyncIterator

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from conduit.sessions import Session


def _sse(event: str, data: dict) -> dict:
    """sse-starlette EventSourceResponse expects {'event': ..., 'data': ...}."""
    return {"event": event, "data": json.dumps(data, separators=(",", ":"))}


async def stream_anthropic_events(
    session: Session,
    user_text: str,
    model: str,
) -> AsyncIterator[dict]:
    """Drive one chat turn and emit Anthropic-format SSE events."""
    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    # message_start
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    await session.client.query(user_text)

    block_index = -1
    block_open = False
    output_text_len = 0
    stop_reason = "end_turn"
    input_tokens = 0
    output_tokens = 0

    async for msg in session.client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if not block_open:
                        block_index += 1
                        block_open = True
                        yield _sse("content_block_start", {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {"type": "text", "text": ""},
                        })
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {"type": "text_delta", "text": block.text},
                    })
                    output_text_len += len(block.text)
            if block_open:
                yield _sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": block_index,
                })
                block_open = False

        elif isinstance(msg, ResultMessage):
            usage = getattr(msg, "usage", None) or {}
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            stop_reason = getattr(msg, "stop_reason", None) or "end_turn"
            break

    session.message_count += 1

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens or output_text_len},
    })
    yield _sse("message_stop", {"type": "message_stop"})


async def collect_non_streaming(
    session: Session,
    user_text: str,
    model: str,
) -> dict:
    """Same loop but returns a single Anthropic `Message` dict (stream=false)."""
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    blocks: list[dict] = []
    stop_reason = "end_turn"
    input_tokens = 0
    output_tokens = 0

    await session.client.query(user_text)

    async for msg in session.client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    blocks.append({"type": "text", "text": block.text})
        elif isinstance(msg, ResultMessage):
            usage = getattr(msg, "usage", None) or {}
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            stop_reason = getattr(msg, "stop_reason", None) or "end_turn"
            break

    session.message_count += 1

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
```

## SDK smoke (run first, adapt if SDK shape differs)

```python
# scratch/sdk_probe.py
import asyncio
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

async def main():
    async with ClaudeSDKClient(options=ClaudeAgentOptions(allowed_tools=[])) as c:
        await c.query("Say hi in 3 words.")
        async for msg in c.receive_response():
            print(type(msg).__name__, repr(msg)[:200])

asyncio.run(main())
```

If the printed shape differs (different class names, partial-delta events),
adjust the `isinstance` checks and content extraction in this module before
moving on.

## Verify
Step 6 wires this into the endpoint and exercises both paths.

## Depends on
Steps 1, 2, 3, 4.
