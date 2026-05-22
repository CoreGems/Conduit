"""Drop-in compatibility test — official anthropic SDK pointed at Conduit.

Marked `integration` because it requires:
  * the Conduit server running on http://127.0.0.1:8765
  * `claude` CLI logged in (Claude Max OAuth)
  * `ANTHROPIC_API_KEY` UNSET in the env (else SDK uses metered API)

Run with:  uv run pytest -m integration   (or: pytest -m integration)
"""
import os

import pytest

pytestmark = pytest.mark.integration

CONDUIT_URL = os.environ.get("CONDUIT_TEST_URL", "http://127.0.0.1:8765")
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")


def _client():
    from anthropic import Anthropic
    return Anthropic(base_url=CONDUIT_URL, api_key="not-used")


def test_official_sdk_non_streaming():
    client = _client()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Reply with exactly: COMPAT OK"}],
    )
    assert msg.role == "assistant"
    assert msg.stop_reason == "end_turn"
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    assert "COMPAT OK" in text


def test_official_sdk_streaming():
    client = _client()
    chunks: list[str] = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": "Count 1, 2, 3"}],
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)
        final = stream.get_final_message()
    joined = "".join(chunks)
    for d in ("1", "2", "3"):
        assert d in joined
    assert final.stop_reason == "end_turn"
