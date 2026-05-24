# [Update] Conduit now does real agentic loops — N sequential tool calls in one user turn

Quick update on Conduit (the local Anthropic-compatible server that routes through Claude Max via the Agent SDK). A client developer flagged that their agentic flow needed the model to chain 2–4 custom tools per user turn (`list_topics → topic_coverage → list_recent_questions → answer`), and the docs claimed multi-cycle was "not exercised." Turned out it already worked — just wasn't tested or documented. Now it's both.

## What multi-cycle means

The standard Anthropic single-tool flow is two HTTP requests: client sends the user message, server replies with `tool_use`, client executes and replies with `tool_result`, server returns final `end_turn`. Two round-trips.

Multi-cycle is the same shape, repeated until the model is satisfied:

```
client → user("How am I doing on warehouses?")
server → tool_use(list_topics)            stop_reason: tool_use
client → tool_result(["snowflake-basics", "warehouses", ...])
server → tool_use(topic_coverage, {topic: "warehouses"})   stop_reason: tool_use
client → tool_result({accuracy: 0.83, questions_answered: 12, ...})
server → text("You're doing well — 83% on 12 questions...")   stop_reason: end_turn
```

Three HTTP round-trips, all the same `session_id`. Three distinct `tool_use_id`s, one per cycle.

## What changed

Nothing in the engine. The pause/resume machinery already supports arbitrary cycle counts — `await_tool_result` is re-entrant, the pump stays alive parked in the `@tool` handler across HTTP requests, `pending_futures` correctly tracks per-id Futures. The only "fix" was honesty in the docs and a regression-pinning test:

- New `tests/test_multicycle_tools.py` runs a 3-cycle scenario end-to-end, asserts unique `tool_use_id`s per cycle, asserts the final answer references data from the earlier tool calls.
- New `scripts/multicycle_demo.py` runnable demo with three fake tools and a system prompt that encourages chaining.
- `AGENTIC_INTEGRATION.md` §5.6 fully documents the cycle-2+ resume body shape, with a generic Python recipe that handles any N:

```python
history = [{"role": "user", "content": "<user prompt>"}]
sid = None
r = httpx.post(URL + "/v1/messages", json={
    "model": MODEL, "max_tokens": 600, "tools": TOOLS,
    "system": "...", "messages": history,
}).raise_for_status()
sid = r.headers["x-conduit-session-id"]
msg = r.json()

while msg["stop_reason"] == "tool_use":
    tu = next(b for b in msg["content"] if b["type"] == "tool_use")
    result = execute_tool(tu["name"], tu["input"])
    history.append({"role": "assistant", "content": msg["content"]})
    history.append({"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": tu["id"], "content": result,
    }]})
    r = httpx.post(URL + "/v1/messages", json={
        "model": MODEL, "max_tokens": 600, "tools": TOOLS,
        "session_id": sid,            # ← same id every cycle
        "messages": history,
    })
    msg = r.json()

# stop_reason is now "end_turn"
```

The `while` is the only delta from single-cycle. Cap `max_cycles` for safety.

## What was actually broken (client-side)

The client developer had been hitting `400 tool_result requires session_id`. They were resending `tools` and the message history correctly but forgetting to wire `x-conduit-session-id` through from the initial response. Once captured and sent in the body of every resume, the multi-cycle flow worked first try.

Documented as pitfall #22 and debug recipe §10.12 so the next person doesn't lose a day to it.

## Still on the to-do list

- **Parallel tool calls in one cycle** — the model emitting two `tool_use` blocks in a single message. The per-name FIFO id queue is in place, but the end-to-end test isn't written. Constrain via prompting if you need strict serialization.
- **`tool_choice` enforcement** — accepted but ignored. SDK doesn't expose a clean way through yet.
- **`CONDUIT_TOOL_RESULT_TIMEOUT_S`** — the env var is defined but not enforced; sessions just sit on the idle sweeper's 30-min timer.
- **Persistent session storage** — sessions die with the server process. Redis/SQLite backend would survive restarts.

## Repo

`<link>` — full docs in `AGENTIC_INTEGRATION.md`, demos in `scripts/`. 24 integration tests passing; multi-cycle pinned by the new test.

Local single-user, bound to `127.0.0.1`, no auth — same disclaimers as previous posts. From 2026-06-15 Agent SDK draws from its own monthly Pro/Max credit pool, separate from the main Claude Code subscription.
