# Step 8 — Tests

## Goal
Two test layers:
1. **Unit-ish** — schema parses, session manager basic ops (no real SDK).
2. **Integration** — point the official `anthropic` SDK at the local server and
   verify drop-in compatibility. This is the killer test.

## Files
- `tests/__init__.py`
- `tests/conftest.py`
- `tests/test_schema.py`
- `tests/test_compatibility.py`

## tests/conftest.py

```python
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

from conduit.app import app

@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

`pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

## tests/test_schema.py

```python
from conduit.schema import MessageCreateRequest

def test_parses_anthropic_minimal():
    req = MessageCreateRequest.model_validate({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.session_id is None
    assert req.stream is False
    assert req.messages[0]["role"] == "user"

def test_session_id_extension():
    req = MessageCreateRequest.model_validate({
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "session_id": "abc-123",
    })
    assert req.session_id == "abc-123"
```

## tests/test_compatibility.py (the headline test)

```python
"""Drop-in compatibility: official anthropic SDK hitting Conduit must work."""
import pytest
from anthropic import Anthropic

# Requires the server to be running locally — run separately from unit tests
SERVER_URL = "http://127.0.0.1:8787"

@pytest.mark.integration
def test_anthropic_sdk_against_conduit():
    client = Anthropic(base_url=SERVER_URL, api_key="not-used")
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[{"role": "user", "content": "Reply with the single word: PONG"}],
    )
    assert msg.content
    assert any("PONG" in b.text for b in msg.content if hasattr(b, "text"))

@pytest.mark.integration
def test_anthropic_sdk_streaming():
    client = Anthropic(base_url=SERVER_URL, api_key="not-used")
    chunks = []
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[{"role": "user", "content": "Count 1 to 3."}],
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)
    full = "".join(chunks)
    assert any(d in full for d in ("1", "2", "3"))
```

Run as:
```powershell
uv run pytest                                    # unit only (no marker filter)
uv run pytest -m integration                     # against running server
```

Mark `integration` in `pyproject.toml`:
```toml
[tool.pytest.ini_options]
markers = ["integration: requires running Conduit server"]
```

## Verify
- `uv run pytest tests/test_schema.py` passes offline.
- Start server, then `uv run pytest -m integration` passes — this proves
  wire-format compatibility with the official Anthropic Python SDK.

## Depends on
Steps 1–7.
