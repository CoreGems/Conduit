"""Route-level unit tests using ASGI transport — no real SDK subprocess."""
from httpx import ASGITransport, AsyncClient

from conduit.app import app


async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


async def test_messages_rejects_empty_user_content():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/v1/messages", json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 64,
            "messages": [{"role": "assistant", "content": "stray assistant"}],
        })
        assert r.status_code == 400
        body = r.json()
        assert body["detail"]["type"] == "invalid_request_error"


async def test_unknown_session_id_returns_404():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/v1/messages", json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 64,
            "session_id": "does-not-exist",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 404
