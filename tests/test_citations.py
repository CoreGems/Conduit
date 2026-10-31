"""Citations passthrough.

When the request carries `document` blocks with `citations: {enabled: true}`,
the API answers with `text` blocks that start life as
``{"type": "text", "text": "", "citations": []}`` and receive
``citations_delta`` events alongside their ``text_delta``s. The Agent SDK
forwards those raw stream events; Conduit used to drop every delta type it
didn't know, so citations never reached the client. Now ``citations_delta``
is forwarded on both stream paths, and the non-streaming collectors fold it
into a ``citations`` array on the text block — the upstream response shape.
"""
import asyncio
import json

from claude_agent_sdk import ResultMessage, StreamEvent

from conduit.streaming import (
    collect_non_streaming,
    stream_anthropic_events,
    stream_tool_events,
)


_CITATION = {
    "type": "char_location",
    "cited_text": "The CFO is Priya Natarajan. ",
    "document_index": 0,
    "document_title": "Memo Q3",
    "start_char_index": 46,
    "end_char_index": 74,
}


def _ev(event: dict) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event=event)


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="s",
    )


def _cited_turn() -> list:
    """message_start → uncited text block → cited text block → end_turn."""
    return [
        _ev({"type": "message_start",
             "message": {"id": "msg_1", "usage": {"input_tokens": 9}}}),
        # Block 0: plain prose, no citations key at all.
        _ev({"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}}),
        _ev({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Per the memo, "}}),
        _ev({"type": "content_block_stop", "index": 0}),
        # Block 1: cited — starts with an empty citations list.
        _ev({"type": "content_block_start", "index": 1,
             "content_block": {"citations": [], "type": "text", "text": ""}}),
        _ev({"type": "content_block_delta", "index": 1,
             "delta": {"type": "citations_delta", "citation": _CITATION}}),
        _ev({"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "the CFO is Priya Natarajan."}}),
        _ev({"type": "content_block_stop", "index": 1}),
        _ev({"type": "message_delta",
             "delta": {"stop_reason": "end_turn", "stop_sequence": None},
             "usage": {"output_tokens": 12}}),
        _ev({"type": "message_stop"}),
        _result(),
    ]


class _FakeClient:
    def __init__(self, messages):
        self._messages = messages

    async def query(self, _prompt):
        return None

    async def receive_response(self):
        for m in self._messages:
            yield m


class _FakeSession:
    def __init__(self, messages):
        self.client = _FakeClient(messages)
        self.message_count = 0
        self.include_thinking = False
        self.output_format = None


class _FakeToolSession(_FakeSession):
    def __init__(self, messages):
        super().__init__(messages)
        self.events_queue = asyncio.Queue()
        self.pump_task = None
        self.state = None


async def _collect(agen) -> list[dict]:
    return [ev async for ev in agen]


def _data(ev: dict) -> dict:
    return json.loads(ev["data"])


# --- plain streaming path ------------------------------------------------------

async def test_stream_forwards_citations_delta():
    sess = _FakeSession(_cited_turn())
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))

    deltas = [_data(e) for e in events if e["event"] == "content_block_delta"]
    cites = [d for d in deltas if d["delta"]["type"] == "citations_delta"]
    assert len(cites) == 1
    assert cites[0]["index"] == 1
    assert cites[0]["delta"]["citation"] == _CITATION


async def test_stream_keeps_empty_citations_list_on_block_start():
    # Upstream signals "this block will be cited" via `citations: []` on the
    # content_block_start; clients key off it, so it must survive.
    sess = _FakeSession(_cited_turn())
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))
    starts = [_data(e) for e in events if e["event"] == "content_block_start"]
    assert starts[0]["content_block"] == {"type": "text", "text": ""}
    assert starts[1]["content_block"] == {"citations": [], "type": "text", "text": ""}


# --- plain non-streaming: citations folded onto the text block -----------------

async def test_collect_non_streaming_builds_citations_array():
    sess = _FakeSession(_cited_turn())
    body = await collect_non_streaming(sess, "q", "claude-sonnet-4-6")

    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {
        "input_tokens": 9, "output_tokens": 12,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    assert body["content"] == [
        {"type": "text", "text": "Per the memo, "},
        {"type": "text", "text": "the CFO is Priya Natarajan.", "citations": [_CITATION]},
    ]


async def test_collect_non_streaming_accumulates_multiple_citations():
    second = dict(_CITATION, cited_text="Q3 revenue was 4.2M.", start_char_index=74,
                  end_char_index=94)
    turn = _cited_turn()
    # Insert another citations_delta on block 1 before its text.
    turn.insert(6, _ev({"type": "content_block_delta", "index": 1,
                        "delta": {"type": "citations_delta", "citation": second}}))
    sess = _FakeSession(turn)
    body = await collect_non_streaming(sess, "q", "claude-sonnet-4-6")
    assert body["content"][1]["citations"] == [_CITATION, second]


# --- tool-use path forwards them too -------------------------------------------

async def test_tool_stream_forwards_citations_delta():
    sess = _FakeToolSession(_cited_turn())
    events = await _collect(stream_tool_events(sess, "q", "claude-sonnet-4-6"))
    deltas = [_data(e) for e in events if e["event"] == "content_block_delta"]
    cites = [d for d in deltas if d["delta"]["type"] == "citations_delta"]
    assert len(cites) == 1
    assert cites[0]["delta"]["citation"] == _CITATION
    # Still a normal end_turn — citations don't affect the pause logic.
    stop = [_data(e) for e in events if e["event"] == "message_delta"][-1]
    assert stop["delta"]["stop_reason"] == "end_turn"
