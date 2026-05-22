"""Session lifecycle endpoints — Conduit's extension over the upstream Messages API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from conduit.schema import CreateSessionResponse, SessionInfo, SessionList
from conduit.sessions import manager

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    system_prompt: str | None = None
    model: str | None = None


@router.get("", response_model=SessionList)
async def list_sessions() -> SessionList:
    return SessionList(
        sessions=[
            SessionInfo(
                session_id=s.id,
                created_at=s.created_at,
                last_used_at=s.last_used_at,
                message_count=s.message_count,
            )
            for s in await manager.list()
        ]
    )


@router.post("", response_model=CreateSessionResponse)
async def create_session(req: CreateSessionRequest | None = None) -> CreateSessionResponse:
    req = req or CreateSessionRequest()
    s = await manager.create(system_prompt=req.system_prompt, model=req.model)
    return CreateSessionResponse(session_id=s.id)


@router.delete("/{session_id}")
async def delete_session(session_id: str) -> dict[str, bool]:
    ok = await manager.delete(session_id)
    if not ok:
        raise HTTPException(
            404,
            detail={"type": "invalid_request_error", "message": f"session {session_id} not found"},
        )
    return {"deleted": True}
