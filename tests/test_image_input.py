"""Image-block passthrough: a user turn carrying `image` content blocks must
reach the SDK as content blocks instead of being flattened away to text.

Previously every prompt-building path kept only `text` blocks, so an image
alongside text was silently dropped and an image-only message 400'd with
"no user text content". Now the newest user turn's text+image block list is
passed to ``ClaudeSDKClient.query()`` via its async-iterable message form.
"""
import pytest
from fastapi import HTTPException

from claude_agent_sdk import ResultMessage, StreamEvent

from conduit.routes.messages import _build_replay_prompt, _last_user_content
from conduit.schema import MessageCreateRequest
from conduit.streaming import _as_sdk_prompt, collect_non_streaming


_IMG = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
}
_TXT = {"type": "text", "text": "describe this image"}


def _req(messages, **kw) -> MessageCreateRequest:
    base = dict(model="claude-sonnet-4-6", max_tokens=64, messages=messages)
    base.update(kw)
    return MessageCreateRequest(**base)


# --- schema: image blocks survive request validation ---------------------------

def test_schema_accepts_image_block():
    req = _req([{"role": "user", "content": [_IMG, _TXT]}])
    content = req.messages[0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["data"] == "aGVsbG8="


# --- _last_user_content (stateful / session_id path) ---------------------------

def test_last_user_content_text_only_still_flattens_to_str():
    req = _req([{"role": "user", "content": [_TXT]}])
    assert _last_user_content(req) == "describe this image"


def test_last_user_content_returns_blocks_when_image_present():
    req = _req([{"role": "user", "content": [_IMG, _TXT]}])
    out = _last_user_content(req)
    assert isinstance(out, list)
    assert [b["type"] for b in out] == ["image", "text"]


def test_last_user_content_image_only_no_longer_400s():
    req = _req([{"role": "user", "content": [_IMG]}])
    out = _last_user_content(req)
    assert isinstance(out, list)
    assert out[0]["type"] == "image"


def test_last_user_content_drops_non_prompt_blocks():
    stray = {"type": "tool_result", "tool_use_id": "toolu_x", "content": "72F"}
    req = _req([{"role": "user", "content": [stray, _IMG, _TXT]}])
    out = _last_user_content(req)
    assert [b["type"] for b in out] == ["image", "text"]


# --- _build_replay_prompt (stateless path) --------------------------------------

def test_replay_single_turn_image_returns_blocks():
    req = _req([{"role": "user", "content": [_IMG, _TXT]}])
    out = _build_replay_prompt(req)
    assert isinstance(out, list)
    assert [b["type"] for b in out] == ["image", "text"]


def test_replay_multi_turn_prepends_history_as_text_block():
    req = _req([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi! how can I help?"},
        {"role": "user", "content": [_TXT, _IMG]},
    ])
    out = _build_replay_prompt(req)
    assert isinstance(out, list)
    assert [b["type"] for b in out] == ["text", "text", "image"]
    history = out[0]["text"]
    assert "User: hello" in history
    assert "Assistant: hi! how can I help?" in history
    assert history.endswith("User:")


def test_replay_history_images_dropped_final_text_turn_stays_str():
    # Only the newest turn's images pass through; an image in history is
    # dropped along with every other non-text block.
    req = _req([
        {"role": "user", "content": [_IMG, {"type": "text", "text": "first"}]},
        {"role": "assistant", "content": "noted"},
        {"role": "user", "content": "and now?"},
    ])
    out = _build_replay_prompt(req)
    assert isinstance(out, str)
    assert "User: first" in out
    assert out.endswith("User: and now?")


def test_replay_still_400s_when_no_text_and_no_image():
    req = _req([{"role": "user", "content": []}])
    with pytest.raises(HTTPException) as ei:
        _build_replay_prompt(req)
    assert ei.value.status_code == 400


# --- _as_sdk_prompt: adapting to ClaudeSDKClient.query() ------------------------

def test_as_sdk_prompt_passes_string_through():
    assert _as_sdk_prompt("hi") == "hi"


async def test_as_sdk_prompt_wraps_blocks_in_one_user_message():
    blocks = [_IMG, _TXT]
    msgs = [m async for m in _as_sdk_prompt(blocks)]
    assert msgs == [{
        "type": "user",
        "message": {"role": "user", "content": blocks},
        "parent_tool_use_id": None,
    }]


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


async def test_collect_non_streaming_sends_blocks_to_sdk():
    sess = _RecordingSession(_clean_turn())
    blocks = [_IMG, _TXT]
    await collect_non_streaming(sess, blocks, "claude-sonnet-4-6")
    assert sess.client.query_arg == [{
        "type": "user",
        "message": {"role": "user", "content": blocks},
        "parent_tool_use_id": None,
    }]


async def test_collect_non_streaming_still_sends_plain_string():
    sess = _RecordingSession(_clean_turn())
    await collect_non_streaming(sess, "hi", "claude-sonnet-4-6")
    assert sess.client.query_arg == "hi"
