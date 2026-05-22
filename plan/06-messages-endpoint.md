# Step 6 — `POST /v1/messages`

## Goal
Anthropic-compatible chat endpoint. Streams or returns full message depending
on `stream` field. Stateful when `session_id` is supplied.

## Files
- `conduit/app.py`              (extend placeholder)
- `conduit/routes/__init__.py`
- `conduit/routes/messages.py`

## Behavior

| `session_id` | `stream` | Server action                                                |
| ------------ | -------- | ------------------------------------------------------------ |
| present      | true     | Use existing session, send last user msg, SSE stream         |
| present      | false    | Use existing session, send last user msg, return Message     |
| absent       | true     | Create ephemeral session, replay full `messages`, stream     |
| absent       | false    | Create ephemeral session, replay full `messages`, return msg |

"Replay full `messages`" — for true Anthropic stateless parity, when no
`session_id` is given we create a one-shot session, send all prior user
messages as a single concatenated prompt (or sequential turns), then tear it
down. Simple v1: take the last user message only and rely on the Agent SDK to
carry it; document the limitation in README.

## conduit/routes/messages.py

```python
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from conduit.schema import MessageCreateRequest
from conduit.sessions import manager
from conduit.streaming import stream_anthropic_events, collect_non_streaming

router = APIRouter(prefix="/v1", tags=["messages"])


def _last_user_text(req: MessageCreateRequest) -> str:
    for m in reversed(req.messages):
        if m["role"] == "user":
            content = m["content"]
            if isinstance(content, str):
                return content
            # list of blocks
            return "\n".join(
                b["text"] for b in content if b.get("type") == "text"
            )
    raise HTTPException(400, "no user message in request")


@router.post("/messages")
async def create_message(req: MessageCreateRequest):
    user_text = _last_user_text(req)

    # Resolve session
    ephemeral = False
    if req.session_id:
        session = await manager.get(req.session_id)
        if session is None:
            raise HTTPException(404, f"session {req.session_id} not found")
    else:
        sys_prompt = req.system if isinstance(req.system, str) else None
        session = await manager.create(system_prompt=sys_prompt)
        ephemeral = True

    async def _cleanup_ephemeral():
        if ephemeral:
            await manager.delete(session.id)

    if req.stream:
        async def event_gen():
            try:
                async with session.lock:
                    async for ev in stream_anthropic_events(session, user_text, req.model):
                        yield ev
            finally:
                await _cleanup_ephemeral()
        return EventSourceResponse(event_gen())

    try:
        async with session.lock:
            return await collect_non_streaming(session, user_text, req.model)
    finally:
        await _cleanup_ephemeral()
```

## conduit/app.py (final form)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

from conduit.config import settings
from conduit.sessions import manager
from conduit.routes.messages import router as messages_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="Conduit", version="0.1.0", lifespan=lifespan)
app.include_router(messages_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn
    s = settings()
    uvicorn.run("conduit.app:app", host=s.host, port=s.port, reload=True)
```

## Verify (non-streaming)

```powershell
curl -X POST http://127.0.0.1:8787/v1/messages `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"claude-sonnet-4-6\",\"max_tokens\":256,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi in 3 words.\"}]}'
```

Expect a JSON Message with `content: [{type: "text", text: "..."}]`.

## Verify (streaming)

```powershell
curl -N -X POST http://127.0.0.1:8787/v1/messages `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"claude-sonnet-4-6\",\"max_tokens\":256,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Count to 5 with reasons.\"}]}'
```

Expect SSE events: `message_start`, `content_block_start`, one or more
`content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`.

## Depends on
Steps 1–5.
