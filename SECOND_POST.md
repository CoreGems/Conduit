# [Update] Conduit now supports tool calling — pause-based Anthropic protocol on top of the loop-based Agent SDK

A few weeks back I shipped Conduit — a local server that exposes an Anthropic-compatible `/v1/messages` endpoint but routes through your Claude Max subscription via `claude-agent-sdk` instead of a metered API key.

Since then I've added what was the big missing piece: **client-defined tool calling**. The hard part isn't the SDK — it's the protocol mismatch.

```
Anthropic API           claude-agent-sdk
─────────────────       ──────────────────
pause-based             loop-based
   ↓                       ↓
client emits             SDK runs tools
tool_use → stops         internally → only
→ client executes        returns final text
→ client sends           (no client tool
tool_result → resumes    callbacks)
```

To make tool-use *look* like Anthropic to the client while *running* on the Agent SDK, Conduit:

1. **Builds a per-session MCP bridge** with handlers mirroring the client's declared tools. Each handler suspends on an `asyncio.Future` instead of executing.
2. **Auto-allocates a session** when `tools` are present, surfaced via `x-conduit-session-id` response header so the client can resume.
3. **Pumps `receive_response()` in a background task** so the HTTP response can close after the model pauses for a tool, while the SDK loop stays parked waiting for the result.
4. **Resumes on the next request** by validating the `tool_result.tool_use_id`, resolving the parked Future, and streaming the continuation.

The whole thing is byte-identical to Anthropic's wire format — the official `anthropic` SDK's tool-use helpers work against it unchanged.

**Round trip in 30 lines:**

```python
import httpx

WEATHER = {"name": "get_weather", "description": "...",
           "input_schema": {"type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"]}}

# Turn 1 — model decides to call the tool, response ends with stop_reason=tool_use
r1 = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6", "max_tokens": 512,
    "tools": [WEATHER],
    "messages": [{"role": "user", "content": "Weather in Paris?"}],
})
sid = r1.headers["x-conduit-session-id"]
r1 = r1.json()
tu = next(b for b in r1["content"] if b["type"] == "tool_use")

# Turn 2 — execute the tool, send result back with the same session_id
r2 = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6", "max_tokens": 512,
    "session_id": sid, "tools": [WEATHER],
    "messages": [
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": r1["content"]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tu["id"],
            "content": "68F partly cloudy"
        }]},
    ],
}).json()
print(r2["content"][0]["text"])
# → "The weather in Paris is 68°F and partly cloudy."
```

**Phase 2 limits (be honest):**

- Single tool call per turn (parallel tool_use not yet supported)
- One pause/resume cycle per session
- `tool_choice` accepted but ignored
- Session idle-evicts after 30 min — long-running tool execution needs careful client design

Phase 3 will add parallel calls, multi-round sessions, `tool_choice` enforcement, and a configurable result timeout.

**Bonus: `effort` is now exposed too.** Set `effort: "low"|"medium"|"high"|"xhigh"|"max"` on a request to control thinking budget. Conduit-only field — pass via `extra_body={"effort": "high"}` when using the official Anthropic SDK.

Docs in the repo (`TOOLS_HOWTO.md`, `AGENTIC_INTEGRATION.md`, `EFFORT_INTEGRATION.md`) cover the wire details for a Rust/Tauri or any-language client. Demo scripts: `scripts/tool_demo.py` and `scripts/effort_test.py`.

Same disclaimer as before: local single-user, bound to `127.0.0.1`, no auth. Don't expose without adding some. And from 2026-06-15 the Agent SDK draws from a separate monthly credit pool from your main Max plan.

Repo: `<link>`
