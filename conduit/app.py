"""Conduit FastAPI app — Anthropic-compatible local Messages API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from conduit.config import settings
from conduit.routes.messages import router as messages_router
from conduit.routes.sessions import router as sessions_router
from conduit.sessions import manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(
    title="Conduit",
    version="0.1.0",
    description="Anthropic-compatible local Messages API, powered by claude-agent-sdk.",
    lifespan=lifespan,
)
app.include_router(messages_router)
app.include_router(sessions_router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    import uvicorn

    s = settings()
    uvicorn.run("conduit.app:app", host=s.host, port=s.port, reload=True)
