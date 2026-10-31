"""Structured output (`output_config.format`).

The Agent SDK implements structured output by registering a CLI-internal
end-turn tool named ``StructuredOutput``: the model "calls" it with the JSON
document as the tool input, the turn ends with ``stop_reason: tool_use``, and
the SDK reports the parsed object on ``ResultMessage.structured_output``.

Upstream Anthropic returns structured output as a single ``text`` block whose
text is the JSON document, with ``stop_reason: end_turn``. Conduit translates
the SDK shape into the upstream shape on every path:

  * the StructuredOutput ``tool_use`` block becomes a ``text`` block;
  * its ``input_json_delta`` chunks become ``text_delta`` chunks;
  * the trailing ``stop_reason: tool_use`` becomes ``end_turn`` (it is not a
    client pause — there is nothing to resume);
  * prose ``text`` blocks the model emits alongside are suppressed, so
    ``content[]`` holds exactly the JSON (what ``client.messages.parse()``
    and ``json.loads(content[0].text)`` both expect).
"""
import asyncio
import json

from claude_agent_sdk import ResultMessage, StreamEvent

from conduit.streaming import (
    collect_message_from_sse,
    collect_non_streaming,
    stream_anthropic_events,
    stream_tool_events,
)


_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
        "additionalProperties": False,
    },
}
_JSON_CHUNKS = ["", '{"name": "Marcus', ' Chen"', ', "age": 34', "}"]
_JSON = "".join(_JSON_CHUNKS)


def _ev(event: dict) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event=event)


def _result(**kw) -> ResultMessage:
    base = dict(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="s",
        structured_output={"name": "Marcus Chen", "age": 34},
    )
    base.update(kw)
    return ResultMessage(**base)


def _structured_turn(with_prose: bool = True) -> list:
    """Mirror of a live Haiku trace: thinking → (prose) → StructuredOutput call."""
    evs = [
        _ev({"type": "message_start",
             "message": {"id": "msg_so", "usage": {"input_tokens": 10}}}),
        _ev({"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
        _ev({"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "sig"}}),
        _ev({"type": "content_block_stop", "index": 0}),
    ]
    idx = 1
    if with_prose:
        evs += [
            _ev({"type": "content_block_start", "index": idx,
                 "content_block": {"type": "text", "text": ""}}),
            _ev({"type": "content_block_delta", "index": idx,
                 "delta": {"type": "text_delta", "text": "Here is a person:"}}),
            _ev({"type": "content_block_stop", "index": idx}),
        ]
        idx += 1
    evs += [
        _ev({"type": "content_block_start", "index": idx,
             "content_block": {"type": "tool_use", "id": "toolu_so", "name": "StructuredOutput",
                               "input": {}, "caller": {"type": "direct"}}}),
    ]
    for chunk in _JSON_CHUNKS:
        evs.append(_ev({"type": "content_block_delta", "index": idx,
                        "delta": {"type": "input_json_delta", "partial_json": chunk}}))
    evs += [
        _ev({"type": "content_block_stop", "index": idx}),
        _ev({"type": "message_delta",
             "delta": {"stop_reason": "tool_use", "stop_sequence": None},
             "usage": {"output_tokens": 149}}),
        _ev({"type": "message_stop"}),
        _result(),
    ]
    return evs


class _FakeClient:
    def __init__(self, messages):
        self._messages = messages

    async def query(self, _prompt):
        return None

    async def receive_response(self):
        for m in self._messages:
            yield m


class _FakeSession:
    def __init__(self, messages, output_format=_SCHEMA):
        self.client = _FakeClient(messages)
        self.message_count = 0
        self.include_thinking = False
        self.output_format = output_format


class _FakeToolSession(_FakeSession):
    def __init__(self, messages, output_format=_SCHEMA):
        super().__init__(messages, output_format)
        self.events_queue = asyncio.Queue()
        self.pump_task = None
        self.state = None
        self.recorded: list = []

    def record_pending_id(self, name, tool_use_id):  # must NOT be hit for StructuredOutput
        self.recorded.append((name, tool_use_id))


async def _collect(agen) -> list[dict]:
    return [ev async for ev in agen]


def _data(ev: dict) -> dict:
    return json.loads(ev["data"])


# --- plain streaming path ------------------------------------------------------

async def test_stream_presents_structured_output_as_text_block():
    sess = _FakeSession(_structured_turn())
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))

    assert [e["event"] for e in events] == [
        "message_start",
        "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_delta", "content_block_delta", "content_block_delta",
        "content_block_stop",
        "message_delta", "message_stop",
    ]
    start = _data(events[1])
    assert start["index"] == 0
    assert start["content_block"] == {"type": "text", "text": ""}

    deltas = [_data(e)["delta"] for e in events if e["event"] == "content_block_delta"]
    assert all(d["type"] == "text_delta" for d in deltas)
    assert "".join(d["text"] for d in deltas) == _JSON
    assert json.loads("".join(d["text"] for d in deltas)) == {"name": "Marcus Chen", "age": 34}


async def test_stream_rewrites_stop_reason_to_end_turn():
    sess = _FakeSession(_structured_turn())
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))
    md = _data([e for e in events if e["event"] == "message_delta"][0])
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["output_tokens"] == 149


async def test_stream_suppresses_prose_when_structured_mode():
    sess = _FakeSession(_structured_turn(with_prose=True))
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))
    texts = [_data(e)["delta"].get("text", "") for e in events if e["event"] == "content_block_delta"]
    assert "Here is a person:" not in "".join(texts)
    # Exactly one content block went out: the JSON one.
    assert sum(1 for e in events if e["event"] == "content_block_start") == 1


async def test_stream_without_output_format_leaves_tool_use_hidden_and_prose_visible():
    # No output_config.format on the session: the plain path behaves as before —
    # prose text is forwarded, and the (unexpected) tool_use block is not.
    sess = _FakeSession(_structured_turn(), output_format=None)
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))
    starts = [_data(e) for e in events if e["event"] == "content_block_start"]
    # StructuredOutput is still recognised by name and rendered as text — the
    # name only exists when the CLI runs with a schema, so this is safe.
    types = [s["content_block"]["type"] for s in starts]
    assert types == ["text", "text"]
    texts = "".join(_data(e)["delta"].get("text", "") for e in events
                    if e["event"] == "content_block_delta")
    assert "Here is a person:" in texts and _JSON in texts


# --- non-streaming: a single JSON text block -----------------------------------

async def test_collect_non_streaming_returns_single_json_text_block():
    sess = _FakeSession(_structured_turn())
    body = await collect_non_streaming(sess, "q", "claude-sonnet-4-6")
    assert body["stop_reason"] == "end_turn"
    assert body["content"] == [{"type": "text", "text": _JSON}]
    assert json.loads(body["content"][0]["text"]) == {"name": "Marcus Chen", "age": 34}
    assert body["usage"]["input_tokens"] == 10
    assert body["usage"]["output_tokens"] == 149


async def test_collect_message_from_sse_has_no_leftover_internal_fields():
    sess = _FakeSession(_structured_turn())
    body = await collect_message_from_sse(
        stream_anthropic_events(sess, "q", "claude-sonnet-4-6"), "claude-sonnet-4-6")
    for block in body["content"]:
        assert not any(k.startswith("_") for k in block)
        assert "caller" not in block and "input" not in block


# --- tool-use path: not a client pause -----------------------------------------

async def test_tool_stream_does_not_pause_on_structured_output():
    sess = _FakeToolSession(_structured_turn())
    events = await _collect(stream_tool_events(sess, "q", "claude-sonnet-4-6"))

    assert sess.recorded == []                      # never registered as a pending tool call
    assert str(sess.state) .endswith("idle") or sess.state.value == "idle"

    starts = [_data(e)["content_block"] for e in events if e["event"] == "content_block_start"]
    assert starts == [{"type": "text", "text": ""}]  # prose suppressed, JSON as text
    md = _data([e for e in events if e["event"] == "message_delta"][0])
    assert md["delta"]["stop_reason"] == "end_turn"
    assert events[-1]["event"] == "message_stop"


async def test_tool_stream_collects_to_json_text_block():
    sess = _FakeToolSession(_structured_turn())
    body = await collect_message_from_sse(
        stream_tool_events(sess, "q", "claude-sonnet-4-6"), "claude-sonnet-4-6")
    assert body["stop_reason"] == "end_turn"
    assert body["content"] == [{"type": "text", "text": _JSON}]


async def test_tool_stream_real_custom_tool_still_pauses():
    # Regression guard: a genuine client tool call is still a pause with
    # stop_reason tool_use, structured mode or not.
    turn = [
        _ev({"type": "message_start", "message": {"id": "msg_t", "usage": {"input_tokens": 1}}}),
        _ev({"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_w",
                               "name": "mcp__conduit__get_weather", "input": {}}}),
        _ev({"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"city":"Oslo"}'}}),
        _ev({"type": "content_block_stop", "index": 0}),
        _ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}),
        _ev({"type": "message_stop"}),
    ]
    sess = _FakeToolSession(turn)
    events = await _collect(stream_tool_events(sess, "q", "claude-sonnet-4-6"))
    assert sess.recorded == [("get_weather", "toolu_w")]
    md = _data([e for e in events if e["event"] == "message_delta"][0])
    assert md["delta"]["stop_reason"] == "tool_use"
    start = _data([e for e in events if e["event"] == "content_block_start"][0])
    assert start["content_block"]["name"] == "get_weather"


# --- the model answers in prose first; the JSON arrives in a follow-up message --
#
# Observed live: with a schema set, Haiku sometimes ends its first message with
# plain text. The CLI then prompts it again and the StructuredOutput call comes
# in a second message of the same turn. The first, JSON-less ending must not
# leak (two end_turns) and must not end the tool-path stream early.

def _prose_then_json_turn(json_follow_up: bool = True) -> list:
    evs = [
        _ev({"type": "message_start", "message": {"id": "msg_a", "usage": {"input_tokens": 10}}}),
        _ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        _ev({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Paris is a European capital."}}),
        _ev({"type": "content_block_stop", "index": 0}),
        _ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}),
        _ev({"type": "message_stop"}),
    ]
    if json_follow_up:
        evs += [
            _ev({"type": "message_start", "message": {"id": "msg_b", "usage": {"input_tokens": 12}}}),
            _ev({"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": "toolu_so2", "name": "StructuredOutput", "input": {}}}),
            _ev({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta", "partial_json": '{"city": "Paris"}'}}),
            _ev({"type": "content_block_stop", "index": 0}),
            _ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}}),
            _ev({"type": "message_stop"}),
        ]
    evs.append(_result(structured_output={"city": "Paris"} if json_follow_up else None))
    return evs


async def test_stream_holds_prose_ending_until_json_arrives():
    sess = _FakeSession(_prose_then_json_turn())
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))
    assert [e["event"] for e in events] == [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert _data(events[1])["content_block"] == {"type": "text", "text": ""}
    assert _data(events[2])["delta"] == {"type": "text_delta", "text": '{"city": "Paris"}'}
    md = _data(events[4])
    assert md["delta"]["stop_reason"] == "end_turn" and md["usage"]["output_tokens"] == 9


async def test_collect_non_streaming_two_message_turn_yields_single_json_block():
    sess = _FakeSession(_prose_then_json_turn())
    body = await collect_non_streaming(sess, "q", "claude-sonnet-4-6")
    assert body["content"] == [{"type": "text", "text": '{"city": "Paris"}'}]
    assert body["stop_reason"] == "end_turn"


async def test_stream_flushes_held_ending_when_json_never_arrives():
    # Clean result, no JSON: the stream must still terminate properly.
    sess = _FakeSession(_prose_then_json_turn(json_follow_up=False))
    events = await _collect(stream_anthropic_events(sess, "q", "claude-sonnet-4-6"))
    assert [e["event"] for e in events] == ["message_start", "message_delta", "message_stop"]
    assert _data(events[1])["delta"]["stop_reason"] == "end_turn"


async def test_tool_stream_continues_past_prose_ending_to_json():
    sess = _FakeToolSession(_prose_then_json_turn())
    events = await _collect(stream_tool_events(sess, "q", "claude-sonnet-4-6"))
    kinds = [e["event"] for e in events]
    assert kinds.count("message_start") == 1
    assert kinds.count("message_delta") == 1 and kinds.count("message_stop") == 1
    starts = [_data(e)["content_block"] for e in events if e["event"] == "content_block_start"]
    assert starts == [{"type": "text", "text": ""}]
    md = _data([e for e in events if e["event"] == "message_delta"][0])
    assert md["delta"]["stop_reason"] == "end_turn"
    assert sess.state.value == "idle"


async def test_tool_stream_flushes_held_ending_when_json_never_arrives():
    sess = _FakeToolSession(_prose_then_json_turn(json_follow_up=False))
    events = await _collect(stream_tool_events(sess, "q", "claude-sonnet-4-6"))
    assert [e["event"] for e in events] == ["message_start", "message_delta", "message_stop"]
    assert sess.state.value == "idle"
