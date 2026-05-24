"""Multi-cycle custom tool calls within one user turn.

Each cycle is a separate HTTP round-trip but uses the same session_id.
The model is allowed to emit tool_use → end_turn → ... → tool_use → end_turn
patterns of arbitrary length before settling on a final text answer.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("CONDUIT_TEST_URL", "http://127.0.0.1:8765")
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")

TOOLS = [
    {
        "name": "list_topics",
        "description": "List all topics. No input.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "topic_coverage",
        "description": "Get coverage stats for a specific topic.",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
        },
    },
]

SYSTEM = (
    "You are a study assistant. To answer the user's question, first call "
    "list_topics, then call topic_coverage with the most relevant topic, "
    "then answer based on the coverage data. Use the tools in order; do "
    "not answer before you have the coverage data."
)


def _fake_tool(name: str, inp: dict) -> str:
    if name == "list_topics":
        return json.dumps(["snowflake-basics", "warehouses", "data-loading"])
    if name == "topic_coverage":
        return json.dumps({"topic": inp.get("topic"), "questions_answered": 12, "accuracy": 0.83})
    return "{}"


def _text(msg: dict) -> str:
    return "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")


def test_multicycle_sequential_tool_calls():
    """N sequential tool calls in one user turn, same session_id across all cycles."""
    history = [{"role": "user", "content": "How am I doing on warehouses?"}]
    sid: str | None = None
    cycles_seen = 0
    tool_use_ids: list[str] = []

    # Initial request
    body = {
        "model": MODEL,
        "max_tokens": 600,
        "system": SYSTEM,
        "tools": TOOLS,
        "messages": history,
    }
    r = httpx.post(f"{URL}/v1/messages", json=body, timeout=180)
    r.raise_for_status()
    sid = r.headers["x-conduit-session-id"]
    msg = r.json()
    cycles_seen += 1

    MAX_CYCLES = 6
    while msg["stop_reason"] == "tool_use" and cycles_seen <= MAX_CYCLES:
        tu = next(b for b in msg["content"] if b["type"] == "tool_use")
        tool_use_ids.append(tu["id"])
        tool_result = _fake_tool(tu["name"], tu["input"])

        # Standard Anthropic protocol: append assistant turn + user(tool_result)
        history.append({"role": "assistant", "content": msg["content"]})
        history.append({"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": tu["id"],
            "content": tool_result,
        }]})

        body = {
            "model": MODEL,
            "max_tokens": 600,
            "tools": TOOLS,
            "session_id": sid,                # ← reuse same session across all cycles
            "messages": history,
        }
        r = httpx.post(f"{URL}/v1/messages", json=body, timeout=180)
        r.raise_for_status()
        # Server should keep the same session id (we sent it; just echoes back).
        assert r.headers.get("x-conduit-session-id") == sid
        msg = r.json()
        cycles_seen += 1

    # Cleanup
    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)

    # --- Assertions on what should have happened ---
    assert cycles_seen >= 3, (
        f"expected at least 3 cycles (2 tool calls + 1 final answer), got {cycles_seen}; "
        f"tool_use_ids: {tool_use_ids}"
    )
    # Each cycle should have produced a UNIQUE tool_use_id
    assert len(set(tool_use_ids)) == len(tool_use_ids), (
        f"tool_use_ids not unique across cycles: {tool_use_ids}"
    )
    # Final message must be end_turn with text content
    assert msg["stop_reason"] == "end_turn", (
        f"never reached end_turn after {cycles_seen} cycles; final stop={msg['stop_reason']!r}"
    )
    final_text = _text(msg)
    assert final_text, "final assistant text is empty"
    # The model should have used the coverage data in its final answer.
    # Loose check: it mentions accuracy / questions / a percentage.
    answer_lower = final_text.lower()
    assert any(k in answer_lower for k in ("83", "accuracy", "questions", "warehouse")), (
        f"final answer doesn't reference coverage data: {final_text!r}"
    )


def test_multicycle_unknown_tool_use_id_on_resume_rejected():
    """Resume with a tool_use_id that's not pending on this session → 400."""
    history = [{"role": "user", "content": "List topics."}]
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 200,
        "system": "Call list_topics, then answer.",
        "tools": TOOLS,
        "messages": history,
    }, timeout=180)
    r.raise_for_status()
    sid = r.headers["x-conduit-session-id"]
    msg = r.json()

    # If model went straight to text (no tool_use), this test is moot — just clean up
    if msg["stop_reason"] != "tool_use":
        httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
        pytest.skip("model didn't call a tool — can't exercise unknown tool_use_id path")

    real_tu = next(b for b in msg["content"] if b["type"] == "tool_use")
    history.append({"role": "assistant", "content": msg["content"]})
    history.append({"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "toolu_FAKE_NEVER_SEEN",   # ← not on this session
        "content": "irrelevant",
    }]})

    r2 = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 200,
        "tools": TOOLS,
        "session_id": sid,
        "messages": history,
    }, timeout=30)
    assert r2.status_code == 400, f"expected 400, got {r2.status_code}: {r2.text[:300]}"
    body = r2.json()
    assert "unknown tool_use_id" in body["detail"]["message"], body

    # Real id still parked — clean up by delivering it so the session can close.
    httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 60,
        "tools": TOOLS,
        "session_id": sid,
        "messages": [
            {"role": "user", "content": "List topics."},
            {"role": "assistant", "content": msg["content"]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": real_tu["id"],
                "content": "[]",
            }]},
        ],
    }, timeout=120)
    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
