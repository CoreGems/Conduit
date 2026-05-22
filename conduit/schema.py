"""Pydantic models matching Anthropic's /v1/messages wire format.

Re-exports the official `anthropic` package types as the source of truth for
the wire format. The only Conduit-specific addition is `MessageCreateRequest`,
which extends Anthropic's request body with an optional `session_id` field for
server-side history.
"""
from typing import Literal

from pydantic import BaseModel, Field

from anthropic.types import (  # noqa: F401  re-exported for callers
    Message,
    MessageParam,
    TextBlockParam,
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

    session_id: str | None = Field(
        default=None,
        description="Conduit extension. When provided, server keeps history; "
        "send only the new user turn. When omitted, behaves like upstream "
        "Anthropic API: the server replays the supplied `messages` array in "
        "an ephemeral session.",
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
