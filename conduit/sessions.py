"""SessionManager — owns ClaudeSDKClient instances keyed by session_id."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from conduit.config import settings


@dataclass
class Session:
    id: str
    client: ClaudeSDKClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    message_count: int = 0
    model: str | None = None


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._dict_lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._sweeper_task is None:
            self._sweeper_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except (asyncio.CancelledError, BaseException):
                pass
            self._sweeper_task = None

        async with self._dict_lock:
            ids = list(self._sessions.keys())
        for sid in ids:
            await self.delete(sid)

    async def create(
        self,
        *,
        system_prompt: str | None = None,
        model: str | None = None,
    ) -> Session:
        s = settings()
        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or s.default_system_prompt,
            tools=s.allowed_tools,
            model=model or s.default_model,
            include_partial_messages=True,
        )
        client = ClaudeSDKClient(options=opts)
        await client.__aenter__()

        sid = str(uuid.uuid4())
        sess = Session(id=sid, client=client, model=opts.model)

        async with self._dict_lock:
            await self._evict_if_needed_locked()
            self._sessions[sid] = sess
        return sess

    async def get(self, sid: str) -> Session | None:
        async with self._dict_lock:
            sess = self._sessions.get(sid)
            if sess is not None:
                sess.last_used_at = time.time()
            return sess

    async def delete(self, sid: str) -> bool:
        async with self._dict_lock:
            sess = self._sessions.pop(sid, None)
        if sess is None:
            return False
        try:
            await sess.client.__aexit__(None, None, None)
        except Exception:
            pass
        return True

    async def list(self) -> list[Session]:
        async with self._dict_lock:
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
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            s = settings()
            cutoff = time.time() - s.session_idle_timeout_s
            async with self._dict_lock:
                stale = [sid for sid, sess in self._sessions.items() if sess.last_used_at < cutoff]
            for sid in stale:
                await self.delete(sid)


manager = SessionManager()
