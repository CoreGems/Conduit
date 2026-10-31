"""Document-block passthrough: a user turn carrying Anthropic `document`
content blocks must reach the SDK as content blocks instead of being
discarded.

Previously only `text` and `image` blocks survived prompt building, so a
RAG-style request (question + N retrieved windows as `document` blocks)
reached the model as the bare question. Now `document` blocks ride along
exactly like images: the newest user turn is passed to
``ClaudeSDKClient.query()`` as a content-block message.
"""
import pytest
from fastapi import HTTPException

from claude_agent_sdk import ResultMessage, StreamEvent

from conduit.routes.messages import _build_replay_prompt, _last_user_content
from conduit.schema import MessageCreateRequest
from conduit.streaming import collect_non_streaming


_DOC = {
    "type": "document",
    "source": {"type": "text", "media_type": "text/plain", "data": "The CFO is Priya."},
    "title": "Memo",
    "citations": {"enabled": True},
}
_DOC_CONTENT = {
    "type": "document",
    "source": {"type": "content", "content": [
        {"type": "text", "text": "Window 1: warehouse moved to Tallinn."},
        {"type": "text", "text": "Window 2: headcount is 37."},
    ]},
    "title": "Retrieved windows",
    "citations": {"enabled": True},
}
_IMG = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
}
_TXT = {"type": "text", "text": "who is the CFO?"}


def _req(messages, **kw) -> MessageCreateRequest:
    base = dict(model="claude-sonnet-4-6", max_tokens=64, messages=messages)
    base.update(kw)
    return MessageCreateRequest(**base)


# --- schema: document blocks survive request validation ------------------------

def test_schema_accepts_text_document_block():
    req = _req([{"role": "user", "content": [_DOC, _TXT]}])
    content = req.messages[0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["data"] == "The CFO is Priya."
    assert content[0]["citations"] == {"enabled": True}


def test_schema_accepts_content_source_document_block():
    req = _req([{"role": "user", "content": [_DOC_CONTENT, _TXT]}])
    doc = req.messages[0]["content"][0]
    assert doc["source"]["type"] == "content"
    assert len(doc["source"]["content"]) == 2


# --- _last_user_content (stateful / session_id path) ---------------------------

def test_last_user_content_returns_blocks_when_document_present():
    req = _req([{"role": "user", "content": [_DOC, _TXT]}])
    out = _last_user_content(req)
    assert isinstance(out, list)
    assert [b["type"] for b in out] == ["document", "text"]
    assert out[0] == _DOC


def test_last_user_content_keeps_many_documents_in_order():
    docs = [dict(_DOC, title=f"w{i}") for i in range(4)]
    req = _req([{"role": "user", "content": docs + [_TXT]}])
    out = _last_user_content(req)
    assert [b.get("title") for b in out[:4]] == ["w0", "w1", "w2", "w3"]
    assert out[-1]["type"] == "text" and out[-1]["text"] == _TXT["text"]


def test_last_user_content_document_only_turn_is_not_a_400():
    req = _req([{"role": "user", "content": [_DOC]}])
    out = _last_user_content(req)
    assert out == [_DOC]


def test_last_user_content_mixes_documents_and_images():
    req = _req([{"role": "user", "content": [_DOC, _IMG, _TXT]}])
    out = _last_user_content(req)
    assert [b["type"] for b in out] == ["document", "image", "text"]


def test_last_user_content_still_drops_foreign_blocks():
    stray = {"type": "tool_result", "tool_use_id": "toolu_x", "content": "72F"}
    req = _req([{"role": "user", "content": [stray, _DOC, _TXT]}])
    out = _last_user_content(req)
    assert [b["type"] for b in out] == ["document", "text"]


def test_last_user_content_text_only_still_flattens_to_str():
    req = _req([{"role": "user", "content": [_TXT]}])
    assert _last_user_content(req) == "who is the CFO?"


# --- _build_replay_prompt (stateless path) --------------------------------------

def test_replay_single_turn_document_returns_blocks():
    req = _req([{"role": "user", "content": [_DOC, _TXT]}])
    out = _build_replay_prompt(req)
    assert out[0] == _DOC
    # pydantic adds `citations: None` to text blocks (optional key, harmless
    # on the wire); compare the fields that matter.
    assert out[1]["type"] == "text" and out[1]["text"] == _TXT["text"]
    assert len(out) == 2


def test_replay_multi_turn_prepends_history_before_documents():
    req = _req([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi!"},
        {"role": "user", "content": [_DOC, _TXT]},
    ])
    out = _build_replay_prompt(req)
    assert [b["type"] for b in out] == ["text", "document", "text"]
    assert "User: hello" in out[0]["text"]
    assert "Assistant: hi!" in out[0]["text"]
    assert out[0]["text"].endswith("User:")
    assert out[1] == _DOC


def test_replay_history_documents_are_dropped():
    # Only the newest turn's documents pass through — same rule as images.
    req = _req([
        {"role": "user", "content": [_DOC, {"type": "text", "text": "first"}]},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "and now?"},
    ])
    out = _build_replay_prompt(req)
    assert isinstance(out, str)
    assert "User: first" in out
    assert "Priya" not in out


def test_replay_still_400s_when_nothing_usable():
    req = _req([{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "x"},
    ]}])
    with pytest.raises(HTTPException) as ei:
        _build_replay_prompt(req)
    assert ei.value.status_code == 400


# --- end-to-end through collect_non_streaming ------------------------------------

class _RecordingClient:
    def __init__(self, messages):
        self._messages = messages
        self.query_arg = None

    async def query(self, prompt):
        if isinstance(prompt, str):
            self.query_arg = prompt
        else:
            self.query_arg = [m async for m in prompt]

    async def receive_response(self):
        for m in self._messages:
            yield m


class _RecordingSession:
    def __init__(self, messages):
        self.client = _RecordingClient(messages)
        self.message_count = 0
        self.include_thinking = False


def _clean_turn():
    return [
        StreamEvent(
            uuid="u", session_id="s",
            event={"type": "message_start",
                   "message": {"id": "msg_1", "usage": {"input_tokens": 5}}},
        ),
        StreamEvent(
            uuid="u", session_id="s",
            event={"type": "message_delta",
                   "delta": {"stop_reason": "end_turn"},
                   "usage": {"output_tokens": 3}},
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="s",
        ),
    ]


async def test_collect_non_streaming_sends_document_blocks_to_sdk():
    sess = _RecordingSession(_clean_turn())
    blocks = [_DOC_CONTENT, _DOC, _TXT]
    await collect_non_streaming(sess, blocks, "claude-sonnet-4-6")
    assert sess.client.query_arg == [{
        "type": "user",
        "message": {"role": "user", "content": blocks},
        "parent_tool_use_id": None,
    }]
