"""Phase 2.5 web-search (hosted SDK tool) integration tests.

Verifies the Anthropic-style hosted-tool pattern:
  - client declares web_search in `tools[]`
  - server enables WebSearch via the SDK
  - response is plain text with stop_reason=end_turn (no client-visible tool_use)
  - hidden SDK pause/continuation is fully transparent

Requires:
  * Conduit server running on http://127.0.0.1:8765
  * `claude` CLI logged in (Max OAuth)
  * ANTHROPIC_API_KEY UNSET

Run:  pytest -m integration tests/test_web_search.py
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("CONDUIT_TEST_URL", "http://127.0.0.1:8765")
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")

WEB_SEARCH_DECL = {"type": "web_search_20250305", "name": "web_search"}
CUSTOM_WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


def test_hosted_web_search_runs_transparently():
    """Declaring web_search runs the tool server-side; response includes
    server_tool_use + web_search_tool_result blocks (Anthropic-canonical) plus
    the final text, all in one response with stop_reason=end_turn."""
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 256,
        "tools": [WEB_SEARCH_DECL],
        "messages": [{"role": "user",
                      "content": "What is the current stable release version of FastAPI? "
                                 "Use web_search to look it up, then state just the version number."}],
    }, timeout=120)
    r.raise_for_status()
    msg = r.json()

    block_types = [b["type"] for b in msg["content"]]
    # No client-pause-style tool_use; that's reserved for custom tools.
    assert "tool_use" not in block_types, f"custom tool_use leaked: {msg['content']}"
    assert "text" in block_types, f"no text in response: {msg['content']}"

    # The hidden pause/continuation must be fully suppressed.
    assert msg["stop_reason"] == "end_turn", (
        f"expected end_turn, got {msg['stop_reason']!r} — hosted pause leaked through"
    )

    # Hosted visibility: client sees what was called and what came back.
    sts = [b for b in msg["content"] if b["type"] == "server_tool_use"]
    wsrs = [b for b in msg["content"] if b["type"] == "web_search_tool_result"]
    assert sts, f"missing server_tool_use block: {msg['content']}"
    assert wsrs, f"missing web_search_tool_result block: {msg['content']}"
    assert sts[0]["name"] == "WebSearch"
    assert "query" in sts[0]["input"], f"missing query in server_tool_use.input: {sts[0]!r}"
    # tool_use_id correlation between the call and the result
    assert wsrs[0]["tool_use_id"] == sts[0]["id"], (
        f"tool_use_id mismatch: call={sts[0]['id']!r} result={wsrs[0]['tool_use_id']!r}"
    )
    # Result content is non-empty (the SDK's raw search result string)
    assert wsrs[0]["content"], "empty web_search_tool_result content"


def test_hosted_only_session_does_not_require_resume():
    """A hosted-only session is ephemeral — server cleans it up after the response."""
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 200,
        "tools": [WEB_SEARCH_DECL],
        "messages": [{"role": "user", "content": "Search for: latest Python release. Reply with version only."}],
    }, timeout=120)
    r.raise_for_status()

    sid = r.headers.get("x-conduit-session-id")
    # Header is surfaced (auto-allocated), but the session should already be cleaned up.
    assert sid is not None
    sessions = httpx.get(f"{URL}/v1/sessions", timeout=10).json()["sessions"]
    sids = [s["session_id"] for s in sessions]
    assert sid not in sids, (
        f"hosted-only session {sid} not torn down after response — leak risk"
    )


def test_mixed_hosted_and_custom_tools_pauses_for_custom_only():
    """If both hosted and custom tools are declared, only the custom one pauses."""
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 256,
        "tools": [WEB_SEARCH_DECL, CUSTOM_WEATHER_TOOL],
        "messages": [{"role": "user",
                      "content": "Use the get_weather tool to check Paris. Don't search the web."}],
    }, timeout=120)
    r.raise_for_status()
    msg = r.json()

    # If model picked the custom tool, we should see a pause.
    if msg["stop_reason"] == "tool_use":
        tool_use_blocks = [b for b in msg["content"] if b["type"] == "tool_use"]
        assert tool_use_blocks, "stop_reason=tool_use but no tool_use block"
        # Should be the custom one, not the hosted one.
        for b in tool_use_blocks:
            assert b["name"] == "get_weather", (
                f"hosted tool_use leaked through as visible: name={b['name']!r}"
            )
        # Clean up the parked session.
        sid = r.headers["x-conduit-session-id"]
        # Deliver a result so the SDK can finish, then delete.
        httpx.post(f"{URL}/v1/messages", json={
            "model": MODEL,
            "max_tokens": 64,
            "session_id": sid,
            "tools": [WEB_SEARCH_DECL, CUSTOM_WEATHER_TOOL],
            "messages": [
                {"role": "user", "content": "ignored"},
                {"role": "assistant", "content": msg["content"]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": tool_use_blocks[0]["id"],
                    "content": "72F sunny"
                }]},
            ],
        }, timeout=120)
    else:
        # Model answered without any tool — also fine; just verify no hosted leak.
        block_types = [b["type"] for b in msg["content"]]
        assert "tool_use" not in block_types
