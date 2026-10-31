"""Schema unit tests — no server, no SDK subprocess."""
from conduit.schema import MessageCreateRequest


def test_parses_anthropic_minimal_request():
    req = MessageCreateRequest.model_validate({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.session_id is None
    assert req.stream is False
    assert req.max_tokens == 1024
    assert req.messages[0]["role"] == "user"


def test_session_id_extension_round_trips():
    req = MessageCreateRequest.model_validate({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "session_id": "abc-123",
    })
    assert req.session_id == "abc-123"


def test_system_can_be_string_or_blocks():
    s_req = MessageCreateRequest.model_validate({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": "be brief",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert s_req.system == "be brief"

    b_req = MessageCreateRequest.model_validate({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "be brief"}],
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert isinstance(b_req.system, list)


def test_stream_default_false():
    req = MessageCreateRequest.model_validate({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.stream is False


def test_nested_block_lists_are_materialised_to_real_lists():
    """pydantic hands back lazy ValidatorIterators for nested lists (a
    document's `source.content`, a tool_result's block list). They must come
    out as real lists: route code does isinstance(list) checks and the blocks
    are json-serialised verbatim to the SDK."""
    import json

    req = MessageCreateRequest.model_validate({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": [
                {"type": "document",
                 "source": {"type": "content", "content": [
                     {"type": "text", "text": "w1"}, {"type": "text", "text": "w2"}]},
                 "title": "windows"},
                {"type": "text", "text": "q"},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "f", "input": {"a": 1}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                    {"type": "text", "text": "72F"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}},
                ]},
            ]},
        ],
    })
    doc = req.messages[0]["content"][0]
    assert isinstance(doc["source"]["content"], list)
    assert [b["text"] for b in doc["source"]["content"]] == ["w1", "w2"]
    tr = req.messages[2]["content"][0]
    assert isinstance(tr["content"], list)
    assert [b["type"] for b in tr["content"]] == ["text", "image"]
    json.dumps(req.messages)  # whole tree is plain data
