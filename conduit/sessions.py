"""SessionManager — owns ClaudeSDKClient instances keyed by session_id.

Two flavours of session:

  * **Plain chat session** (no tools). Uses the original fast path: HTTP
    handler calls ``query()`` and iterates ``receive_response()`` directly
    under ``Session.lock``.

  * **Tool-use session** (`tools_spec` non-empty). A per-session MCP bridge
    is attached to the SDK client. A background "pump" task drains
    ``receive_response()`` into ``Session.events_queue``. HTTP handlers
    consume events from the queue, which lets us close the HTTP response
    while the SDK loop is parked inside a `@tool` handler awaiting a
    Future that resolves on the next resume request.

See TOOLS_HOWTO.md for the full design.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from conduit.config import settings


class SessionState(str, Enum):
    IDLE = "idle"                          # no turn in progress
    STREAMING = "streaming"                # HTTP holding an open SSE response
    PAUSED_FOR_TOOLS = "paused_for_tools"  # SDK loop parked on tool Future(s)
    CLOSED = "closed"


@dataclass
class Session:
    id: str
    client: ClaudeSDKClient | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    message_count: int = 0
    model: str | None = None
    effort: str | None = None
    include_thinking: bool = False

    # --- Tool-use extensions (None / empty when not a tool session) ---
    tools_spec: list[dict[str, Any]] | None = None
    hosted_sdk_tools: list[str] | None = None
    state: SessionState = SessionState.IDLE
    events_queue: asyncio.Queue | None = None
    pump_task: asyncio.Task | None = None
    pending_ids_by_name: dict[str, deque[str]] = field(default_factory=dict)
    pending_futures: dict[str, asyncio.Future] = field(default_factory=dict)
    # Results delivered for tool_use_ids whose handler hasn't been called yet.
    # Happens with parallel tool calls: the SDK calls @tool handlers serially,
    # so when the client returns N tool_results at once only the first id has
    # a live Future. Later ids get stashed here and consumed by their handler
    # when it eventually fires.
    deferred_results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def is_tool_session(self) -> bool:
        return self.tools_spec is not None

    @property
    def uses_pump(self) -> bool:
        """True if the session uses the background pump (any tools, custom or hosted)."""
        return self.events_queue is not None

    def is_pending_tool_use_id(self, tool_use_id: str) -> bool:
        """True if `tool_use_id` is known to this session — either has a live
        Future awaiting result, or has been observed in the stream and is
        waiting for its handler to fire (parallel-tool case)."""
        if tool_use_id in self.pending_futures:
            return True
        if tool_use_id in self.deferred_results:
            return True
        for q in self.pending_ids_by_name.values():
            if tool_use_id in q:
                return True
        return False

    # --- Tool-use plumbing (called from the bridge + the streaming layer) ---

    def record_pending_id(self, name: str, tool_use_id: str) -> None:
        """Called by the streaming/pump layer when a tool_use content_block_start
        is observed. Buffers the id under its name for the handler to pop."""
        self.pending_ids_by_name.setdefault(name, deque()).append(tool_use_id)

    async def await_tool_result(self, name: str, arg: dict) -> dict:
        """Called from the MCP bridge handler. Returns the matching tool_result
        immediately if the client has already delivered it (parallel-tool case),
        otherwise parks the SDK loop until the client returns one."""
        q = self.pending_ids_by_name.get(name)
        if not q:
            return {"content": [{"type": "text", "text": f"[conduit] no pending tool_use_id for {name}"}]}
        tool_use_id = q.popleft()

        # Fast path: client already delivered this id (e.g. parallel tools —
        # client sent N results in one resume; only the first handler had a
        # live Future at delivery time, the rest were stashed here).
        deferred = self.deferred_results.pop(tool_use_id, None)
        if deferred is not None:
            return deferred

        # Slow path: park on a Future for the client to deliver later.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending_futures[tool_use_id] = fut
        try:
            return await fut
        finally:
            self.pending_futures.pop(tool_use_id, None)

    def deliver_tool_result(self, tool_use_id: str, content: Any) -> bool:
        """Deliver a tool_result for `tool_use_id`. Returns True if accepted.

        Two paths:
          * The handler already fired and is parked on a Future → resolve it.
          * The handler hasn't been called yet (parallel-tool case: SDK invokes
            handlers serially, so only the first id of a batch has a live Future
            at the time the client returns N results) → stash in deferred_results
            for the handler to find when it eventually pops its id.
        """
        if isinstance(content, str):
            payload = {"content": [{"type": "text", "text": content}]}
        elif isinstance(content, list):
            payload = {"content": content}
        else:
            payload = {"content": [{"type": "text", "text": str(content)}]}

        fut = self.pending_futures.get(tool_use_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)
            return True

        # No live Future yet → stash. await_tool_result will consume it.
        self.deferred_results[tool_use_id] = payload
        return True


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
        tools_spec: list[dict[str, Any]] | None = None,
        hosted_sdk_tools: list[str] | None = None,
        effort: str | None = None,
        include_thinking: bool = False,
    ) -> Session:
        """Create a new session.

        If `tools_spec` is non-empty, builds the per-session MCP bridge for
        client-defined tools (pause/resume flow).
        If `hosted_sdk_tools` is non-empty, adds those SDK built-in tool names
        to ClaudeAgentOptions.tools (the SDK executes them internally).
        Either or both triggers the pump-based streaming path.

        `effort` (low/medium/high/xhigh/max) maps to ClaudeAgentOptions.effort.
        """
        s = settings()
        chosen_model = model or s.default_model
        chosen_system = system_prompt or s.default_system_prompt
        chosen_effort = effort or s.default_effort

        sid = str(uuid.uuid4())
        sess = Session(id=sid, client=None, model=chosen_model)
        sess.effort = chosen_effort
        sess.include_thinking = bool(include_thinking)

        has_custom = bool(tools_spec)
        has_hosted = bool(hosted_sdk_tools)

        if has_custom or has_hosted:
            # Lazy import — tool_bridge depends on Session, avoid module-load
            # circular import.
            from conduit.tool_bridge import build_bridge

            sdk_tools: list[str] = []
            mcp_servers: dict | None = None
            if has_custom:
                server, custom_allowed = build_bridge(tools_spec, sess)
                mcp_servers = {"conduit": server}
                sdk_tools.extend(custom_allowed)
                sess.tools_spec = list(tools_spec)
            if has_hosted:
                for sdk_name in hosted_sdk_tools:
                    if sdk_name not in sdk_tools:
                        sdk_tools.append(sdk_name)
                sess.hosted_sdk_tools = list(hosted_sdk_tools)

            opts = ClaudeAgentOptions(
                system_prompt=chosen_system,
                model=chosen_model,
                mcp_servers=mcp_servers,
                tools=sdk_tools,
                include_partial_messages=True,
                permission_mode="bypassPermissions",
                effort=chosen_effort,
            )
            sess.events_queue = asyncio.Queue()
        else:
            opts = ClaudeAgentOptions(
                system_prompt=chosen_system,
                tools=s.allowed_tools,
                model=chosen_model,
                include_partial_messages=True,
                effort=chosen_effort,
            )

        client = ClaudeSDKClient(options=opts)
        await client.__aenter__()
        sess.client = client

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
        sess.state = SessionState.CLOSED

        # Cancel any pump
        if sess.pump_task is not None:
            sess.pump_task.cancel()
            try:
                await sess.pump_task
            except BaseException:
                pass
            sess.pump_task = None

        # Fail any outstanding tool Futures so the SDK unwinds
        for fut in list(sess.pending_futures.values()):
            if not fut.done():
                fut.cancel()
        sess.pending_futures.clear()

        try:
            if sess.client is not None:
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
            if oldest.pump_task is not None:
                oldest.pump_task.cancel()
            try:
                if oldest.client is not None:
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
