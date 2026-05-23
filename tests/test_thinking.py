"""Thinking pass-through tests.

Verifies that `include_thinking` controls whether `thinking` content blocks
appear in the response. The model still does thinking based on its own
effort/thinking settings; this flag only controls whether Conduit forwards
those blocks vs strips them.

Requires:
  * Conduit server running on http://127.0.0.1:8765
  * `claude` CLI logged in (Max OAuth)
  * ANTHROPIC_API_KEY UNSET
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("CONDUIT_TEST_URL", "http://127.0.0.1:8765")
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")


def test_default_strips_thinking():
    """Without include_thinking, response has only text blocks."""
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 96,
        "messages": [{"role": "user", "content": "What is 2+2? Just the number."}],
    }, timeout=60)
    r.raise_for_status()
    msg = r.json()

    block_types = [b["type"] for b in msg["content"]]
    assert "thinking" not in block_types, f"thinking leaked through with default: {block_types}"
    assert "text" in block_types


def test_include_thinking_true_returns_thinking_blocks():
    """With include_thinking=true, response includes thinking blocks before text."""
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 200,
        "include_thinking": True,
        "messages": [{"role": "user", "content": "What is 2+2? Just the number."}],
    }, timeout=60)
    r.raise_for_status()
    msg = r.json()

    block_types = [b["type"] for b in msg["content"]]
    assert "thinking" in block_types, f"include_thinking=true but no thinking block: {block_types}"
    assert "text" in block_types

    # Thinking should come before text (ordering matches Anthropic's wire format).
    first_thinking = block_types.index("thinking")
    first_text = block_types.index("text")
    assert first_thinking < first_text, "thinking should precede text in content[]"

    # Thinking block must have actual content + a signature.
    t_block = next(b for b in msg["content"] if b["type"] == "thinking")
    assert t_block.get("thinking"), "thinking block has empty 'thinking' field"
    assert t_block.get("signature"), "thinking block has empty 'signature' field"


def test_thinking_pass_through_streaming():
    """Stream form: thinking_delta and signature_delta arrive when opted in."""
    saw_thinking_start = False
    saw_thinking_delta = False
    saw_signature_delta = False
    saw_text_delta = False
    saw_message_stop = False

    with httpx.stream("POST", f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 200,
        "stream": True,
        "include_thinking": True,
        "messages": [{"role": "user", "content": "What is 5*7? Just the number."}],
    }, timeout=60) as resp:
        resp.raise_for_status()
        import json
        event_name = None
        for line in resp.iter_lines():
            if not line:
                event_name = None
                continue
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                t = d.get("type")
                if t == "content_block_start" and d.get("content_block", {}).get("type") == "thinking":
                    saw_thinking_start = True
                elif t == "content_block_delta":
                    dt = d.get("delta", {}).get("type")
                    if dt == "thinking_delta":
                        saw_thinking_delta = True
                    elif dt == "signature_delta":
                        saw_signature_delta = True
                    elif dt == "text_delta":
                        saw_text_delta = True
                elif t == "message_stop":
                    saw_message_stop = True

    assert saw_thinking_start, "missing content_block_start type=thinking"
    assert saw_thinking_delta, "missing thinking_delta"
    assert saw_signature_delta, "missing signature_delta"
    assert saw_text_delta, "missing text_delta"
    assert saw_message_stop, "stream didn't reach message_stop"
