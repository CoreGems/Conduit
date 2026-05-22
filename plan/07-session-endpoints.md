# Step 7 — Session endpoints

## Goal
Lifecycle endpoints for the Conduit `session_id` extension.

## Files
- `conduit/routes/sessions.py`
- update `conduit/app.py` to register the router

## Implementation

```python
# conduit/routes/sessions.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from conduit.schema import SessionInfo, SessionList, CreateSessionResponse
from conduit.sessions import manager

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    system_prompt: str | None = None


@router.get("", response_model=SessionList)
async def list_sessions() -> SessionList:
    return SessionList(sessions=[
        SessionInfo(
            session_id=s.id,
            created_at=s.created_at,
            last_used_at=s.last_used_at,
            message_count=s.message_count,
        )
        for s in await manager.list()
    ])


@router.post("", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest | None = None) -> CreateSessionResponse:
    s = await manager.create(system_prompt=(req.system_prompt if req else None))
    return CreateSessionResponse(session_id=s.id)


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    ok = await manager.delete(session_id)
    if not ok:
        raise HTTPException(404, f"session {session_id} not found")
    return {"deleted": True}
```

## conduit/app.py — add the router

```python
from conduit.routes.sessions import router as sessions_router
app.include_router(sessions_router)
```

## Verify

```powershell
# Create
$sid = (Invoke-RestMethod -Method Post http://127.0.0.1:8787/v1/sessions -ContentType 'application/json' -Body '{}').session_id

# Use it
$body = @{
  model = "claude-sonnet-4-6"
  max_tokens = 256
  session_id = $sid
  messages = @(@{role="user"; content="Remember the number 42."})
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post http://127.0.0.1:8787/v1/messages -ContentType 'application/json' -Body $body

$body2 = @{
  model = "claude-sonnet-4-6"
  max_tokens = 256
  session_id = $sid
  messages = @(@{role="user"; content="What number did I ask you to remember?"})
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post http://127.0.0.1:8787/v1/messages -ContentType 'application/json' -Body $body2
# Response should reference 42 → proves server-side history works

# Cleanup
Invoke-RestMethod -Method Delete "http://127.0.0.1:8787/v1/sessions/$sid"
```

## Depends on
Steps 1–6.
