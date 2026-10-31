"""`output_config` — the upstream Anthropic home of `effort` and the
structured-output `format`.

Conduit's request model used to drop unknown fields, so a canonical request
(`output_config: {effort: "high", format: {...}}`) silently lost both. Now
`output_config` is parsed, `effort` inside it is honoured (it wins over the
Conduit top-level alias), and `format` is validated and forwarded to the
session as the SDK's `output_format`.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from conduit.app import app
from conduit.routes import messages as messages_route
from conduit.routes import sessions as sessions_route
from conduit.schema import MessageCreateRequest


_FORMAT = {
    "type": "json_schema",
    "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
}


def _req(**kw) -> MessageCreateRequest:
    base = dict(model="claude-sonnet-4-6", max_tokens=64,
                messages=[{"role": "user", "content": "hi"}])
    base.update(kw)
    return MessageCreateRequest(**base)


# --- schema / resolution ----------------------------------------------------------

def test_output_config_effort_is_parsed_and_resolved():
    req = _req(output_config={"effort": "high"})
    assert req.output_config.effort == "high"
    assert req.resolved_effort == "high"
    assert req.resolved_output_format is None


def test_output_config_effort_wins_over_top_level_alias():
    req = _req(effort="low", output_config={"effort": "xhigh"})
    assert req.resolved_effort == "xhigh"


def test_top_level_effort_still_works_alone():
    req = _req(effort="medium")
    assert req.resolved_effort == "medium"


def test_top_level_effort_is_fallback_when_output_config_has_no_effort():
    req = _req(effort="medium", output_config={"format": _FORMAT})
    assert req.resolved_effort == "medium"
    assert req.resolved_output_format == _FORMAT


def test_no_effort_anywhere_resolves_none():
    assert _req().resolved_effort is None
    assert _req().resolved_output_format is None


def test_output_config_format_is_validated_and_exposed():
    req = _req(output_config={"format": _FORMAT})
    assert req.resolved_output_format == _FORMAT


def test_output_config_unknown_members_are_ignored():
    req = _req(output_config={"effort": "low", "task_budget": {"type": "tokens", "total": 20000}})
    assert req.resolved_effort == "low"


def test_output_config_effort_rejects_bad_value():
    with pytest.raises(ValidationError):
        _req(output_config={"effort": "extreme"})


def test_output_config_format_rejects_non_json_schema_type():
    with pytest.raises(ValidationError) as ei:
        _req(output_config={"format": {"type": "json", "schema": {}}})
    assert "json_schema" in str(ei.value)


def test_output_config_format_rejects_missing_schema():
    with pytest.raises(ValidationError):
        _req(output_config={"format": {"type": "json_schema"}})


# --- route plumbing: what reaches SessionManager.create ---------------------------

class _Captured(Exception):
    def __init__(self, kwargs):
        self.kwargs = kwargs


async def _capturing_create(**kwargs):
    raise _Captured(kwargs)


async def _post(path: str, body: dict):
    # Default transport re-raises app exceptions, so the `_Captured` short-
    # circuit raised by the patched `manager.create` propagates to the test.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body)


async def test_messages_route_binds_output_config_on_plain_session(monkeypatch):
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        raise _Captured(kwargs)

    monkeypatch.setattr(messages_route.manager, "create", fake_create)
    with pytest.raises(_Captured):
        await _post("/v1/messages", {
            "model": "claude-sonnet-4-6", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {"effort": "high", "format": _FORMAT},
        })
    assert captured["effort"] == "high"
    assert captured["output_format"] == _FORMAT


async def test_messages_route_binds_output_config_on_tool_session(monkeypatch):
    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        raise _Captured(kwargs)

    monkeypatch.setattr(messages_route.manager, "create", fake_create)
    with pytest.raises(_Captured):
        await _post("/v1/messages", {
            "model": "claude-sonnet-4-6", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "get_weather", "description": "w",
                       "input_schema": {"type": "object", "properties": {}}}],
            "effort": "low",
            "output_config": {"effort": "xhigh", "format": _FORMAT},
        })
    assert captured["effort"] == "xhigh"          # canonical field wins
    assert captured["output_format"] == _FORMAT
    assert captured["tools_spec"][0]["name"] == "get_weather"


async def test_messages_route_422s_on_bad_format():
    r = await _post("/v1/messages", {
        "model": "claude-sonnet-4-6", "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "output_config": {"format": {"type": "text"}},
    })
    assert r.status_code == 422
    assert "json_schema" in r.text


async def test_sessions_route_binds_output_config(monkeypatch):
    captured: dict = {}

    class _S:
        id = "sess-1"

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _S()

    monkeypatch.setattr(sessions_route.manager, "create", fake_create)
    r = await _post("/v1/sessions", {
        "model": "claude-sonnet-4-6",
        "effort": "low",
        "output_config": {"effort": "high", "format": _FORMAT},
    })
    assert r.status_code == 200
    assert r.json() == {"session_id": "sess-1"}
    assert captured["effort"] == "high"
    assert captured["output_format"] == _FORMAT


async def test_sessions_route_without_output_config_keeps_legacy_effort(monkeypatch):
    captured: dict = {}

    class _S:
        id = "sess-2"

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return _S()

    monkeypatch.setattr(sessions_route.manager, "create", fake_create)
    r = await _post("/v1/sessions", {"effort": "medium"})
    assert r.status_code == 200
    assert captured["effort"] == "medium"
    assert captured["output_format"] is None
