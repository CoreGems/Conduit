# Step 4 — Sessions

## Goal
`SessionManager` that owns `ClaudeSDKClient` instances keyed by `session_id`,
with idle eviction and a hard cap.

## Files
- `conduit/sessions.py`

## Design notes
- `ClaudeSDKClient` is an async context manager — the manager holds the
  underlying open client and explicitly calls `__aenter__` / `__aexit__`.
- One asyncio.Lock per session — Anthropic-style turn-based chat means only
  one request per session can be in flight at a time. The lock enforces that.
- LRU eviction when over `max_sessions`.
- Background sweeper task for idle timeout.

## Implementation

```python
# conduit/sessions.py
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

from conduit.config import settings


@dataclass
class Session:
    id: str
    client: ClaudeSDKClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    message_count: int = 0


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()       # protects the dict
        self._sweeper_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._sweeper_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweeper_task:
            self._sweeper_task.cancel()
        async with self._lock:
            ids = list(self._sessions.keys())
        for sid in ids:
            await self.delete(sid)

    async def create(self, *, system_prompt: str | None = None) -> Session:
        s = settings()
        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or s.default_system_prompt,
            allowed_tools=s.allowed_tools,
        )
        client = ClaudeSDKClient(options=opts)
        await client.__aenter__()
        sid = str(uuid.uuid4())
        sess = Session(id=sid, client=client)

        async with self._lock:
            await self._evict_if_needed_locked()
            self._sessions[sid] = sess
        return sess

    async def get(self, sid: str) -> Session | None:
        async with self._lock:
            sess = self._sessions.get(sid)
            if sess:
                sess.last_used_at = time.time()
            return sess

    async def delete(self, sid: str) -> bool:
        async with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess is None:
            return False
        try:
            await sess.client.__aexit__(None, None, None)
        except Exception:
            pass
        return True

    async def list(self) -> list[Session]:
        async with self._lock:
            return list(self._sessions.values())

    async def _evict_if_needed_locked(self) -> None:
        s = settings()
        while len(self._sessions) >= s.max_sessions:
            oldest = min(self._sessions.values(), key=lambda x: x.last_used_at)
            self._sessions.pop(oldest.id, None)
            try:
                await oldest.client.__aexit__(None, None, None)
            except Exception:
                pass

    async def _sweep_loop(self) -> None:
        s = settings()
        while True:
            await asyncio.sleep(60)
            cutoff = time.time() - s.session_idle_timeout_s
            async with self._lock:
                stale = [sid for sid, sess in self._sessions.items() if sess.last_used_at < cutoff]
            for sid in stale:
                await self.delete(sid)


# Module-level singleton — wired up in app.py lifespan
manager = SessionManager()
```

## Verify
No standalone verification — exercised via endpoints in steps 6–7.
A simple smoke test you can drop into a scratch script:

```python
import asyncio
from conduit.sessions import manager

async def main():
    await manager.start()
    s = await manager.create()
    print("created", s.id)
    assert await manager.get(s.id) is not None
    assert await manager.delete(s.id)
    await manager.stop()

asyncio.run(main())
```

## Depends on
Steps 1, 2.
