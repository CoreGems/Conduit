"""Pydantic models matching Anthropic's /v1/messages wire format.

Re-exports the official `anthropic` package types as the source of truth for
the wire format. The only Conduit-specific addition is `MessageCreateRequest`,
which extends Anthropic's request body with an optional `session_id` field for
server-side history.
"""
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    # Conduit extension — when true, the response stream includes the model's
    # `thinking` content blocks (Anthropic-canonical wire format) instead of
    # stripping them. Bound at session creation. The model still does thinking
    # based on its own `effort`/`thinking` settings; this only controls forwarding.
    include_thinking: bool = False

    @field_validator("messages", mode="after")
    @classmethod
    def _materialise_message_contents(cls, v: Any) -> list[dict]:
        """pydantic v2 returns content fields as lazy ``ValidatorIterator``s.
        Force them into plain lists so route helpers can iterate repeatedly
        without exhausting the iterator (and without changing the wire schema)."""
        out: list[dict] = []
        for m in v:
            md = dict(m) if not isinstance(m, dict) else m
            content = md.get("content")
            if content is not None and not isinstance(content, str):
                try:
                    md["content"] = [dict(b) if not isinstance(b, dict) else b
                                     for b in content]
                except TypeError:
                    pass
            out.append(md)
        return out

    session_id: str | None = Field(
        default=None,
        description="Conduit extension. When provided, server keeps history; "
        "send only the new user turn. When omitted, behaves like upstream "
        "Anthropic API: the server replays the supplied `messages` array in "
        "an ephemeral session. For tool-use turns, the server auto-allocates "
        "a session and surfaces the id via the `x-conduit-session-id` header.",
    )


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
