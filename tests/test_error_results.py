"""Regression tests: an upstream/SDK error must not surface as a hollow 200.

The SDK reports a recoverable upstream API error (429 / 5xx / 529) as a normal
ResultMessage with ``is_error=True`` / ``api_error_status`` set and NO assistant
content. A subscription usage-limit ("out of quota") arrives the same way, with
the reason carried as error text. Conduit used to break on that ResultMessage
and return a 200 with empty content and zero tokens, which clients read as "no
content in LLM response". These tests pin the new behaviour: a proper
HTTPException with a meaningful message is raised instead.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from claude_agent_sdk import ResultMessage, StreamEvent

from conduit.streaming import (
    _looks_like_quota,
    _result_error,
    collect_non_streaming,
    guard_sse,
    stream_anthropic_events,
    stream_tool_events,
)


def _result(**kw) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
    )
    base.update(kw)
    return ResultMessage(**base)


def test_result_error_none_on_clean_turn():
    assert _result_error(_result()) is None


def test_result_error_maps_overloaded():
    err = _result_error(_result(is_error=True, api_error_status=529))
    assert isinstance(err, HTTPException)
    assert err.status_code == 529
    assert err.detail["type"] == "overloaded_error"


def test_result_error_maps_rate_limit():
    err = _result_error(_result(is_error=True, api_error_status=429))
    assert err.status_code == 429
    assert err.detail["type"] == "rate_limit_error"


def test_result_error_defaults_to_502_api_error():
    # is_error with no usable status → generic 502 api_error.
    err = _result_error(_result(is_error=True, subtype="error_during_execution"))
    assert err.status_code == 502
    assert err.detail["type"] == "api_error"


# --- subscription quota / usage-limit detection --------------------------------

def test_looks_like_quota_matches_usage_limit():
    assert _looks_like_quota("Claude AI usage limit reached|1751850000")
    assert _looks_like_quota("Your credit balance is too low")
    assert not _looks_like_quota("here is your weather forecast")


def test_result_error_detects_quota_and_maps_to_429():
    err = _result_error(_result(
        is_error=True, result="Claude AI usage limit reached|1751850000"))
    assert isinstance(err, HTTPException)
    assert err.status_code == 429
    assert err.detail["type"] == "rate_limit_error"
    msg = err.detail["message"].lower()
    assert "quota" in msg
    assert "1751850000" in err.detail["message"]  # reset timestamp surfaced


def test_result_error_detects_quota_via_error_subtype():
    # Some SDK builds signal the limit via subtype rather than is_error.
    err = _result_error(_result(
        is_error=False, subtype="error_during_execution",
        result="usage limit reached"))
    assert err is not None
    assert err.status_code == 429


def test_result_error_no_false_positive_on_clean_answer():
    # A successful turn whose answer legitimately mentions "usage limit" must
    # NOT be misread as a quota failure — is_error is False and subtype success.
    assert _result_error(_result(
        result="Sure — your API usage limit resets monthly.")) is None


class _FakeClient:
    def __init__(self, messages):
        self._messages = messages

    async def query(self, _user_text):
        return None

    async def receive_response(self):
        for m in self._messages:
            yield m


class _FakeSession:
    def __init__(self, messages):
        self.client = _FakeClient(messages)
        self.message_count = 0
        self.include_thinking = False


async def test_collect_non_streaming_raises_on_error_result():
    sess = _FakeSession([_result(is_error=True, api_error_status=529)])
    with pytest.raises(HTTPException) as ei:
        await collect_non_streaming(sess, "hi", "claude-opus-4-8")
    assert ei.value.status_code == 529


async def test_collect_non_streaming_raises_on_empty_turn():
    # A bare success result with no message_start / content is the exact symptom.
    sess = _FakeSession([_result()])
    with pytest.raises(HTTPException) as ei:
        await collect_non_streaming(sess, "hi", "claude-opus-4-8")
    assert ei.value.status_code == 502


async def test_collect_non_streaming_raises_quota_with_meaningful_message():
    sess = _FakeSession([_result(
        is_error=True, result="Claude AI usage limit reached|1751850000")])
    with pytest.raises(HTTPException) as ei:
        await collect_non_streaming(sess, "hi", "claude-opus-4-8")
    assert ei.value.status_code == 429
    assert "quota" in ei.value.detail["message"].lower()


async def test_collect_non_streaming_ok_when_content_present():
    msgs = [
        StreamEvent(
            uuid="u",
            session_id="s",
            event={"type": "message_start",
                   "message": {"usage": {"input_tokens": 5}}},
        ),
        StreamEvent(
            uuid="u",
            session_id="s",
            event={"type": "message_delta",
                   "delta": {"stop_reason": "end_turn"},
                   "usage": {"output_tokens": 3}},
        ),
        _result(),
    ]
    sess = _FakeSession(msgs)
    body = await collect_non_streaming(sess, "hi", "claude-opus-4-8")
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 5
    assert body["usage"]["output_tokens"] == 3


# --- tool-use path: an empty/quota turn must not become a hollow 200 -----------

class _FakeToolSession:
    """Minimal stand-in exercising stream_tool_events' initial-turn path."""

    def __init__(self, messages):
        self.client = _FakeClient(messages)
        self.events_queue = asyncio.Queue()   # non-None so the guard passes
        self.pump_task = None                 # None → treated as a fresh turn
        self.state = None
        self.include_thinking = False
        self.message_count = 0


async def _drain_tool_events(sess):
    async for _ in stream_tool_events(sess, "hi", "claude-opus-4-8"):
        pass


async def test_stream_tool_events_raises_on_empty_turn():
    # Clean ResultMessage, no content: the tool path used to return content:[].
    sess = _FakeToolSession([_result()])
    with pytest.raises(HTTPException) as ei:
        await _drain_tool_events(sess)
    assert ei.value.status_code == 502


async def test_stream_tool_events_raises_quota():
    sess = _FakeToolSession([_result(
        is_error=True, result="Claude AI usage limit reached|1751850000")])
    with pytest.raises(HTTPException) as ei:
        await _drain_tool_events(sess)
    assert ei.value.status_code == 429
    assert "quota" in ei.value.detail["message"].lower()


# --- plain streaming path ------------------------------------------------------

def _message_start() -> StreamEvent:
    return StreamEvent(
        uuid="u", session_id="s",
        event={"type": "message_start",
               "message": {"id": "msg_1", "usage": {"input_tokens": 5}}},
    )


async def _collect(agen) -> list[dict]:
    return [ev async for ev in agen]


async def test_stream_anthropic_events_raises_on_empty_turn():
    sess = _FakeSession([_result()])
    with pytest.raises(HTTPException) as ei:
        await _collect(stream_anthropic_events(sess, "hi", "claude-opus-4-8"))
    assert ei.value.status_code == 502


async def test_stream_anthropic_events_raises_error_even_after_content():
    # A flagged failure must surface even once message_start went out — the
    # route's guard_sse turns it into a mid-stream `error` event, like the
    # real API does. Previously it was swallowed into a truncated stream.
    sess = _FakeSession([
        _message_start(),
        _result(is_error=True, api_error_status=529),
    ])
    with pytest.raises(HTTPException) as ei:
        await _collect(stream_anthropic_events(sess, "hi", "claude-opus-4-8"))
    assert ei.value.status_code == 529


# --- guard_sse: failures after the response head is sent -----------------------
#
# sse_starlette sends the 200 + SSE headers before pulling the first event, so
# an HTTPException raised inside the generator can never become an HTTP error
# status. The observed failure was Starlette's "Caught handled exception, but
# response already started" and a dropped connection with no error visible to
# the client. guard_sse must translate it into an Anthropic `error` event.

def _parse_error(ev: dict) -> dict:
    assert ev["event"] == "error"
    data = json.loads(ev["data"])
    assert data["type"] == "error"
    return data["error"]


async def test_guard_sse_turns_quota_into_error_event():
    sess = _FakeToolSession([_result(
        is_error=True, result="Claude AI usage limit reached|1751850000")])
    events = await _collect(guard_sse(stream_tool_events(sess, "hi", "claude-opus-4-8")))
    assert len(events) == 1
    err = _parse_error(events[0])
    assert err["type"] == "rate_limit_error"
    assert "quota" in err["message"].lower()
    assert "1751850000" in err["message"]


async def test_guard_sse_keeps_prior_events_and_appends_error():
    sess = _FakeSession([
        _message_start(),
        _result(is_error=True, api_error_status=400,
                result="API Error: 400 Claude Code 2.1.147 does not support this model"),
    ])
    events = await _collect(guard_sse(stream_anthropic_events(sess, "hi", "claude-fable-5-1")))
    assert [e["event"] for e in events] == ["message_start", "error"]
    err = _parse_error(events[-1])
    assert err["type"] == "invalid_request_error"
    assert "does not support this model" in err["message"]


async def test_guard_sse_passes_clean_stream_through():
    async def ok():
        yield {"event": "message_start", "data": "{}"}
        yield {"event": "message_stop", "data": "{}"}

    events = await _collect(guard_sse(ok()))
    assert [e["event"] for e in events] == ["message_start", "message_stop"]


async def test_guard_sse_reports_unexpected_exception_as_api_error():
    async def boom():
        yield {"event": "message_start", "data": "{}"}
        raise RuntimeError("pump died")

    events = await _collect(guard_sse(boom()))
    err = _parse_error(events[-1])
    assert err["type"] == "api_error"
    assert "RuntimeError" in err["message"] and "pump died" in err["message"]


async def test_guard_sse_through_asgi_stack_does_not_drop_connection():
    # End-to-end through FastAPI + sse_starlette: the body must end with the
    # error event and the request must complete without an exception.
    import httpx
    from fastapi import FastAPI
    from sse_starlette.sse import EventSourceResponse

    async def failing():
        yield {"event": "message_start", "data": "{}"}
        raise HTTPException(
            status_code=529,
            detail={"type": "overloaded_error", "message": "Overloaded"},
        )

    app = FastAPI()

    @app.get("/stream")
    async def stream():
        return EventSourceResponse(guard_sse(failing()))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/stream")

    assert resp.status_code == 200
    assert "event: message_start" in resp.text
    assert "event: error" in resp.text
    assert '"overloaded_error"' in resp.text
