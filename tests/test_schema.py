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
