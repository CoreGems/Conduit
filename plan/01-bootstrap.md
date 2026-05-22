# Step 1 — Bootstrap

## Goal
Python project skeleton with `uv`, deps installed, placeholder app that runs.

## Files
- `pyproject.toml`
- `conduit/__init__.py`
- `conduit/app.py`        (placeholder)
- `.python-version`       (`3.11`)
- `.gitignore`            (add `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`)

## Commands

```powershell
uv init --package conduit --python 3.11
uv add fastapi "uvicorn[standard]" sse-starlette claude-agent-sdk anthropic pydantic
uv add --dev pytest pytest-asyncio httpx
```

## pyproject.toml (key fields)

```toml
[project]
name = "conduit"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "sse-starlette>=2.1",
  "claude-agent-sdk>=0.1",
  "anthropic>=0.40",
  "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "httpx>=0.27"]

[project.scripts]
conduit = "conduit.app:main"
```

## conduit/app.py (placeholder)

```python
from fastapi import FastAPI

app = FastAPI(title="Conduit", version="0.1.0")

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

def main() -> None:
    import uvicorn
    uvicorn.run("conduit.app:app", host="127.0.0.1", port=8787, reload=True)
```

## Verify

```powershell
uv run conduit
# in another shell:
curl http://127.0.0.1:8787/health           # → {"status":"ok"}
start http://127.0.0.1:8787/docs            # Swagger UI loads
```

## Depends on
Nothing.
