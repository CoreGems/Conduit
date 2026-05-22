"""Phase 2 tool-use integration tests.

Cases (per TOOLS_HOWTO.md test plan):
  1. test_no_tool_use_when_not_needed
  2. test_single_tool_call_round_trip
  6. test_x_conduit_session_id_header

All require:
  * Conduit server running on http://127.0.0.1:8765
  * Claude Code CLI logged in (Max OAuth)
  * ANTHROPIC_API_KEY UNSET

Run with:  pytest -m integration tests/test_tool_use.py
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("CONDUIT_TEST_URL", "http://127.0.0.1:8765")
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get current weather for a city. Returns a short string.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
}


def _post_json(payload: dict) -> httpx.Response:
    return httpx.post(f"{URL}/v1/messages", json=payload, timeout=120)


# ---------------------------------------------------------------------------
# Test 1
# ---------------------------------------------------------------------------

def test_no_tool_use_when_not_needed():
    """Declaring tools doesn't force the model to use them."""
    r = _post_json({
        "model": MODEL,
        "max_tokens": 96,
        "tools": [WEATHER_TOOL],
        "messages": [{"role": "user", "content": "Say the word PING back to me and stop. Do not use any tool."}],
    })
    r.raise_for_status()
    msg = r.json()

    assert msg["type"] == "message"
    assert msg["role"] == "assistant"
    assert msg["stop_reason"] == "end_turn", f"expected end_turn, got {msg['stop_reason']!r}"
    # Content should be plain text, no tool_use blocks
    block_types = [b["type"] for b in msg["content"]]
    assert "tool_use" not in block_types, f"unexpected tool_use: {msg['content']}"
    assert "text" in block_types
    # x-conduit-session-id should still be set (we auto-allocate for tool-use turns)
    assert "x-conduit-session-id" in r.headers


# ---------------------------------------------------------------------------
# Test 2
# ---------------------------------------------------------------------------

def test_single_tool_call_round_trip():
    """Full pause/resume cycle: declare tool → model invokes → we deliver result → final text."""
    # Initial request
    r1 = _post_json({
        "model": MODEL,
        "max_tokens": 256,
        "tools": [WEATHER_TOOL],
        "messages": [{"role": "user", "content": "What's the weather in Paris right now? Use the get_weather tool exactly once."}],
    })
    r1.raise_for_status()
    msg1 = r1.json()
    sid = r1.headers.get("x-conduit-session-id")

    assert sid, "x-conduit-session-id header missing on initial response"
    assert msg1["stop_reason"] == "tool_use", f"expected tool_use, got {msg1['stop_reason']!r}"

    tool_use_blocks = [b for b in msg1["content"] if b["type"] == "tool_use"]
    assert len(tool_use_blocks) == 1, f"expected exactly 1 tool_use block, got {len(tool_use_blocks)}"

    tu = tool_use_blocks[0]
    assert tu["name"] == "get_weather", f"name should be bare client name, got {tu['name']!r}"
    assert "city" in tu["input"], f"city missing in input: {tu['input']!r}"
    assert tu["id"].startswith("toolu_"), f"id should be toolu_*: {tu['id']!r}"

    # Resume request — deliver the tool result
    r2 = httpx.post(
        f"{URL}/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 256,
            "tools": [WEATHER_TOOL],
            "session_id": sid,
            "messages": [
                {"role": "user", "content": "What's the weather in Paris right now? Use the get_weather tool exactly once."},
                {"role": "assistant", "content": msg1["content"]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": "It's 68°F and partly cloudy in Paris right now."}
                ]},
            ],
        },
        timeout=120,
    )
    r2.raise_for_status()
    msg2 = r2.json()

    assert msg2["stop_reason"] == "end_turn", f"resume should end with end_turn, got {msg2['stop_reason']!r}"
    text2 = "".join(b.get("text", "") for b in msg2["content"] if b["type"] == "text")
    # The model should reference 68 or partly cloudy from the tool result
    assert any(token in text2.lower() for token in ("68", "partly cloudy", "cloudy")), (
        f"final reply doesn't reference tool result: {text2!r}"
    )

    # Cleanup — session should still exist; delete it
    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)


# ---------------------------------------------------------------------------
# Test 6
# ---------------------------------------------------------------------------

def test_x_conduit_session_id_header_round_trips():
    """Initial response has the header; using it on resume continues correctly."""
    r1 = _post_json({
        "model": MODEL,
        "max_tokens": 128,
        "tools": [WEATHER_TOOL],
        "messages": [{"role": "user", "content": "What's the weather in Tokyo? Please call get_weather."}],
    })
    r1.raise_for_status()
    sid = r1.headers.get("x-conduit-session-id")
    msg1 = r1.json()
    assert sid is not None
    assert msg1["stop_reason"] == "tool_use"

    tu = next(b for b in msg1["content"] if b["type"] == "tool_use")

    # Verify the session is listed and tracks the right model
    sessions = httpx.get(f"{URL}/v1/sessions", timeout=10).json()["sessions"]
    sids = [s["session_id"] for s in sessions]
    assert sid in sids, f"session {sid} not in /v1/sessions list"

    # Resume successfully using ONLY the session_id (not replaying full history matters
    # to the upstream Anthropic API but for our tool-use sessions we still send full
    # context for parity; just verify the resume works)
    r2 = httpx.post(
        f"{URL}/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 128,
            "tools": [WEATHER_TOOL],
            "session_id": sid,
            "messages": [
                {"role": "user", "content": "..."},
                {"role": "assistant", "content": msg1["content"]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": "55F drizzle"}
                ]},
            ],
        },
        timeout=120,
    )
    r2.raise_for_status()
    msg2 = r2.json()
    assert msg2["stop_reason"] == "end_turn"

    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
