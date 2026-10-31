"""Session lifecycle endpoints — Conduit's extension over the upstream Messages API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from conduit.schema import CreateSessionResponse, OutputConfig, SessionInfo, SessionList
from conduit.sessions import manager

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    system_prompt: str | None = None
    model: str | None = None
    effort: str | None = None  # 'low' | 'medium' | 'high' | 'xhigh' | 'max'
    include_thinking: bool = False
    # Upstream `output_config`: `effort` (wins over the alias above) and the
    # structured-output `format`. Same shape as on POST /v1/messages.
    output_config: OutputConfig | None = None


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
    oc = req.output_config
    s = await manager.create(
        system_prompt=req.system_prompt,
        model=req.model,
        effort=(oc.effort if oc is not None and oc.effort is not None else req.effort),
        include_thinking=req.include_thinking,
        output_format=(oc.format if oc is not None else None),
    )
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
