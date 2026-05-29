# Step 2 — Config

## Goal
Centralized env-driven settings. Single import surface for the rest of the app.

## Files
- `conduit/config.py`

## Implementation

```python
# conduit/config.py
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONDUIT_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8787

    # Agent SDK defaults — overridable per-request via the messages API
    default_model: str = "claude-sonnet-4-6"
    default_system_prompt: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)  # [] = pure chat

    # Session lifecycle
    session_idle_timeout_s: int = 30 * 60   # 30 min
    max_sessions: int = 100

@lru_cache
def settings() -> Settings:
    return Settings()
```

Also add `pydantic-settings` to deps:
```powershell
uv add pydantic-settings
```

## Update `conduit/app.py`

```python
from conduit.config import settings

def main() -> None:
    import uvicorn
    s = settings()
    uvicorn.run("conduit.app:app", host=s.host, port=s.port, reload=True)
```

## Verify

```powershell
$env:CONDUIT_PORT = "9999"
uv run conduit                              # binds to 9999
Remove-Item env:CONDUIT_PORT
```

`.env` file at project root also works:
```
CONDUIT_PORT=9000
CONDUIT_DEFAULT_MODEL=claude-opus-4-8
```

## Depends on
Step 1.
