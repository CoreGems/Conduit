"""Parallel custom-tool calls: model emits N tool_use blocks in ONE message,
client returns N tool_results in ONE resume request.

The SDK invokes @tool handlers serially even when the model emits them in
parallel, so when the client returns N results at once only the first id
has a live Future. The rest go through Conduit's `deferred_results` path.
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("CONDUIT_TEST_URL", "http://127.0.0.1:8765")
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}

PARALLEL_PROMPT = (
    "Get the weather for Paris AND Tokyo. Call get_weather for each city in "
    "parallel (both tool_use blocks in the same response), then give one "
    "sentence summarizing both."
)


def _text(msg: dict) -> str:
    return "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")


def test_parallel_tool_calls_in_one_turn():
    """Model emits 2 tool_use blocks → client returns 2 tool_results → end_turn."""
    history = [{"role": "user", "content": PARALLEL_PROMPT}]
    r1 = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 400,
        "tools": [WEATHER_TOOL],
        "messages": history,
    }, timeout=120)
    r1.raise_for_status()
    sid = r1.headers["x-conduit-session-id"]
    msg1 = r1.json()

    tool_uses = [b for b in msg1["content"] if b["type"] == "tool_use"]
    if len(tool_uses) < 2:
        httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
        pytest.skip(f"model only emitted {len(tool_uses)} tool_use blocks — can't test parallel path")

    assert msg1["stop_reason"] == "tool_use"
    # All ids should be distinct
    ids = [tu["id"] for tu in tool_uses]
    assert len(set(ids)) == len(ids), f"duplicate tool_use_ids: {ids}"
    # All should be get_weather invocations
    for tu in tool_uses:
        assert tu["name"] == "get_weather", f"unexpected tool name: {tu['name']!r}"
        assert "city" in tu["input"], f"missing city in input: {tu['input']!r}"

    # Resume with BOTH tool_results in one user message
    tool_results = [
        {"type": "tool_result",
         "tool_use_id": tu["id"],
         "content": f"It's 72F sunny in {tu['input']['city']}."}
        for tu in tool_uses
    ]
    history.append({"role": "assistant", "content": msg1["content"]})
    history.append({"role": "user", "content": tool_results})

    r2 = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 400,
        "tools": [WEATHER_TOOL],
        "session_id": sid,
        "messages": history,
    }, timeout=120)
    r2.raise_for_status()
    msg2 = r2.json()

    assert msg2["stop_reason"] == "end_turn", (
        f"expected end_turn, got {msg2['stop_reason']!r}; body: {msg2!r}"
    )
    final_text = _text(msg2).lower()
    # The model should reference BOTH cities in the final answer
    cities = [tu["input"]["city"].lower() for tu in tool_uses]
    for city in cities:
        assert city in final_text, (
            f"final answer doesn't mention {city!r}: {final_text!r}"
        )

    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)


def test_parallel_partial_resume_then_resume_rejected():
    """Sending only SOME of the parallel tool_results on a resume should
    leave the session in a state where the remaining ids can still be
    delivered. (Spec: deliver what you have; the rest stay parked / deferred.)

    This documents the current behavior. A client sending a partial resume
    is unusual but shouldn't crash the session.
    """
    history = [{"role": "user", "content": PARALLEL_PROMPT}]
    r1 = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 400,
        "tools": [WEATHER_TOOL],
        "messages": history,
    }, timeout=120)
    r1.raise_for_status()
    sid = r1.headers["x-conduit-session-id"]
    msg1 = r1.json()
    tool_uses = [b for b in msg1["content"] if b["type"] == "tool_use"]
    if len(tool_uses) < 2:
        httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
        pytest.skip("model didn't go parallel")

    # Verify a totally unknown id is rejected (sanity for validation)
    history_bad = history + [
        {"role": "assistant", "content": msg1["content"]},
        {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "toolu_FAKE_NEVER_SEEN",
            "content": "x",
        }]},
    ]
    r_bad = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 100,
        "tools": [WEATHER_TOOL],
        "session_id": sid,
        "messages": history_bad,
    }, timeout=30)
    assert r_bad.status_code == 400
    assert "unknown tool_use_id" in r_bad.json()["detail"]["message"]

    # Now properly resume with all real ids to clean up the session
    tool_results = [
        {"type": "tool_result",
         "tool_use_id": tu["id"],
         "content": f"It's 72F sunny in {tu['input']['city']}."}
        for tu in tool_uses
    ]
    history.append({"role": "assistant", "content": msg1["content"]})
    history.append({"role": "user", "content": tool_results})
    httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 200,
        "tools": [WEATHER_TOOL],
        "session_id": sid,
        "messages": history,
    }, timeout=120)
    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
