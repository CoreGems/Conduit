"""Multi-turn chat: Pattern A (stateless history) and Pattern B (server session)."""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

URL = os.environ.get("CONDUIT_TEST_URL", "http://127.0.0.1:8765")
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")


def _text_of(msg: dict) -> str:
    return "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")


def test_plain_chat_surfaces_session_id_header():
    """Every plain-chat response includes x-conduit-session-id (auto-allocated)."""
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Reply OK."}],
    }, timeout=60)
    r.raise_for_status()
    sid = r.headers.get("x-conduit-session-id")
    assert sid, "missing x-conduit-session-id header on plain chat response"
    # Session is reusable (not torn down ephemerally)
    sessions = httpx.get(f"{URL}/v1/sessions", timeout=10).json()["sessions"]
    sids = [s["session_id"] for s in sessions]
    assert sid in sids, "auto-allocated session was torn down — Pattern B can't work"
    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)


def test_pattern_a_stateless_history_replay():
    """Client sends full history each turn; no session_id. Model maintains context."""
    history: list[dict] = []
    cities_per_turn: list[str] = []

    for q in [
        "Pick a single random European capital. Just the name on one line, nothing else.",
        "Good. What country is that city in? One short sentence.",
    ]:
        history.append({"role": "user", "content": q})
        r = httpx.post(f"{URL}/v1/messages", json={
            "model": MODEL,
            "max_tokens": 64,
            "messages": history,    # full history, no session_id
        }, timeout=60)
        r.raise_for_status()
        ans = _text_of(r.json())
        history.append({"role": "assistant", "content": ans})
        cities_per_turn.append(ans)

    # The country in turn 2 should reference the city from turn 1.
    city_word = cities_per_turn[0].strip().split()[0].rstrip(".,!?")
    assert city_word.lower() in cities_per_turn[1].lower(), (
        f"Pattern A broken: turn-2 reply {cities_per_turn[1]!r} doesn't reference city {city_word!r} from turn 1"
    )


def test_pattern_b_stateful_session_reuse():
    """Reuse session_id from header on subsequent turns; send only new user message."""
    sid: str | None = None
    answers: list[str] = []

    for q in [
        "Pick a single random European capital. Just the name on one line, nothing else.",
        "Good. What country is that city in? One short sentence.",
    ]:
        body = {
            "model": MODEL,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": q}],   # only new turn
        }
        if sid is not None:
            body["session_id"] = sid
        r = httpx.post(f"{URL}/v1/messages", json=body, timeout=60)
        r.raise_for_status()
        if sid is None:
            sid = r.headers["x-conduit-session-id"]
        answers.append(_text_of(r.json()))

    city_word = answers[0].strip().split()[0].rstrip(".,!?")
    assert city_word.lower() in answers[1].lower(), (
        f"Pattern B broken: session reuse didn't preserve context. "
        f"turn-1 city={city_word!r} turn-2 reply={answers[1]!r}"
    )
    httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
