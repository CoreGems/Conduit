"""Pydantic models matching Anthropic's /v1/messages wire format.

Re-exports the official `anthropic` package types as the source of truth for
the wire format. The only Conduit-specific addition is `MessageCreateRequest`,
which extends Anthropic's request body with an optional `session_id` field for
server-side history.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from anthropic.types import (  # noqa: F401  re-exported for callers
    Message,
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolUnionParam,
    ToolUseBlock,
    ToolResultBlockParam,
    Usage,
    RawMessageStreamEvent,
    RawMessageStartEvent,
    RawContentBlockStartEvent,
    RawContentBlockDeltaEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStopEvent,
)


def _deep_materialise(value: Any) -> Any:
    """Recursively turn pydantic's lazy iterators into lists and copy dicts.

    Strings/bytes pass through; any other iterable (pydantic's
    ``ValidatorIterator``, tuples, generators) becomes a list. Iterating a
    ``ValidatorIterator`` runs the deferred validation, so malformed nested
    blocks still surface as a 422 instead of a later 500.
    """
    if isinstance(value, BaseModel):
        # pydantic may resolve a block to one of the anthropic SDK's *response*
        # models (e.g. a text block becomes ``TextBlock``). Dump only the keys
        # the client actually sent, so the wire shape round-trips unchanged.
        return _deep_materialise(value.model_dump(exclude_unset=True))
    if isinstance(value, dict):
        return {k: _deep_materialise(x) for k, x in value.items()}
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, (list, tuple)):
        return [_deep_materialise(x) for x in value]
    try:
        it = iter(value)
    except TypeError:
        return value
    return [_deep_materialise(x) for x in it]


EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class OutputConfig(BaseModel):
    """Anthropic's `output_config` object.

    Two members are honoured:

    * ``effort`` — the upstream-canonical home of the effort enum. Same values
      and binding rules as Conduit's top-level ``effort`` extension.
    * ``format`` — structured output: ``{"type": "json_schema", "schema": {...}}``.
      Forwarded to the Agent SDK's ``output_format`` option. The response then
      carries exactly one ``text`` block whose text is the JSON document.

    Unknown members (``task_budget`` etc.) are accepted and ignored.
    """

    model_config = ConfigDict(extra="allow")

    effort: EffortLevel | None = None
    format: dict[str, Any] | None = None

    @field_validator("format", mode="after")
    @classmethod
    def _validate_format(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return None
        if v.get("type") != "json_schema":
            raise ValueError("output_config.format.type must be 'json_schema'")
        schema = v.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("output_config.format.schema must be a JSON Schema object")
        return v


class MessageCreateRequest(BaseModel):
    """Anthropic-compatible request body + Conduit's `session_id` extension."""

    model: str
    messages: list[MessageParam]
    system: str | list[TextBlockParam] | None = None
    max_tokens: int = 1024
    stream: bool = False
    temperature: float | None = None
    stop_sequences: list[str] | None = None

    # ToolUnionParam covers both custom function tools and hosted server tools
    # (WebSearch/WebFetch/Bash/TextEditor/CodeExecution variants).
    tools: list[ToolUnionParam] | None = None
    tool_choice: dict | None = None

    # Conduit extension — Claude API "effort" enum, controls reasoning budget.
    # Bound at session creation; ignored on subsequent turns through a stateful session.
    # The upstream-canonical spelling is `output_config.effort`; both are accepted.
    effort: EffortLevel | None = None

    # Upstream Anthropic `output_config`: `effort` and structured-output `format`.
    # Both are bound at session creation, like `effort` / `model`.
    output_config: OutputConfig | None = None

    # Conduit extension — when true, the response stream includes the model's
    # `thinking` content blocks (Anthropic-canonical wire format) instead of
    # stripping them. Bound at session creation. The model still does thinking
    # based on its own `effort`/`thinking` settings; this only controls forwarding.
    include_thinking: bool = False

    @field_validator("messages", mode="after")
    @classmethod
    def _materialise_message_contents(cls, v: Any) -> list[dict]:
        """pydantic v2 returns content fields as lazy ``ValidatorIterator``s --
        at every nesting level, not just the top-level block list: a
        ``document`` block's ``source.content`` and a ``tool_result``'s block
        list arrive as iterators too, which neither ``isinstance(x, list)``
        checks nor ``json.dumps`` accept. Force the whole tree into plain
        lists/dicts so route helpers can iterate repeatedly and blocks
        serialise verbatim to the SDK (without changing the wire schema)."""
        return [_deep_materialise(m) for m in v]

    session_id: str | None = Field(
        default=None,
        description="Conduit extension. When provided, server keeps history; "
        "send only the new user turn. When omitted, behaves like upstream "
        "Anthropic API: the server replays the supplied `messages` array in "
        "an ephemeral session. For tool-use turns, the server auto-allocates "
        "a session and surfaces the id via the `x-conduit-session-id` header.",
    )

    @property
    def resolved_effort(self) -> str | None:
        """Effort to bind: upstream `output_config.effort` wins over the
        Conduit top-level alias; None defers to the server default."""
        if self.output_config is not None and self.output_config.effort is not None:
            return self.output_config.effort
        return self.effort

    @property
    def resolved_output_format(self) -> dict[str, Any] | None:
        """Validated `output_config.format` to forward as the SDK's `output_format`."""
        if self.output_config is None:
            return None
        return self.output_config.format


class SessionInfo(BaseModel):
    session_id: str
    created_at: float
    last_used_at: float
    message_count: int


class SessionList(BaseModel):
    sessions: list[SessionInfo]


class CreateSessionResponse(BaseModel):
    session_id: str


class ErrorEnvelope(BaseModel):
    """Matches Anthropic's error envelope shape."""

    type: Literal["error"] = "error"
    error: dict
