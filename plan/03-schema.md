# Step 3 — Schema

## Goal
Pydantic request/response models matching Anthropic's `/v1/messages` wire
format **exactly**, plus the `session_id` extension.

## Files
- `conduit/schema.py`

## Strategy
Re-export the `anthropic` package's pydantic types where possible — they are
the source of truth for the wire format. Define our own `MessageCreateRequest`
subclass only to add the optional `session_id` field.

## Implementation

```python
# conduit/schema.py
from typing import Literal
from pydantic import BaseModel, Field

# Anthropic upstream types — the wire-format contract
from anthropic.types import (
    Message,                  # full response object
    MessageParam,             # one entry in `messages: [...]`
    TextBlockParam,
    Usage,
    RawMessageStreamEvent,    # SSE event union
    RawMessageStartEvent,
    RawContentBlockStartEvent,
    RawContentBlockDeltaEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStopEvent,
)

class MessageCreateRequest(BaseModel):
    """Anthropic-compatible request body + Conduit's session_id extension."""
    model: str
    messages: list[MessageParam]
    system: str | list[TextBlockParam] | None = None
    max_tokens: int = 1024
    stream: bool = False
    temperature: float | None = None
    stop_sequences: list[str] | None = None
    # Conduit extension — when present, server keeps history
    session_id: str | None = Field(default=None, description="Conduit-only: stateful session id")

class SessionInfo(BaseModel):
    session_id: str
    created_at: float
    last_used_at: float
    message_count: int

class SessionList(BaseModel):
    sessions: list[SessionInfo]

class CreateSessionResponse(BaseModel):
    session_id: str

class ErrorResponse(BaseModel):
    """Matches Anthropic's error envelope."""
    type: Literal["error"] = "error"
    error: dict  # {"type": "invalid_request_error", "message": "..."}
```

## Verify

```powershell
uv run python -c "from conduit.schema import MessageCreateRequest; print(MessageCreateRequest.model_json_schema()['properties'].keys())"
# → dict_keys(['model', 'messages', 'system', 'max_tokens', 'stream', ...])
```

Visit `http://127.0.0.1:8787/docs` later — once endpoints are added in step 6
they will render this exact schema in Swagger.

## Depends on
Step 1 (for `anthropic` package).
