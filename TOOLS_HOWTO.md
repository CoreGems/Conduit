# TOOLS_HOWTO — adding client-defined tool use to Conduit

## The gap

Two protocols, opposite shapes:

| | Anthropic Messages API | claude-agent-sdk |
|---|---|---|
| Shape | **Pause-based** | **Loop-based** |
| Tool call | Model emits `tool_use` block → response ends with `stop_reason: tool_use` → client executes tool → client sends `tool_result` in a *new* request | SDK invokes registered tool handlers internally as the model emits them; `receive_response()` only finishes after the whole multi-turn-with-tools is done |
| Where execution lives | In the **client** | Inside the **SDK process** |

Conduit today forwards the SDK's final assistant text and is wire-compatible with the *result*, but it has no mechanism to **pause mid-stream** when the model wants a client-defined tool. To get parity with the Anthropic API for tool use, we need to interrupt the SDK loop, surface the `tool_use` block to the HTTP client, end the response, and resume the loop on the client's follow-up request that carries the `tool_result`.

## Architecture

```
                         ┌──────────────────────────────────────────┐
                         │  Conduit process                         │
                         │                                          │
   Client (Rust/Tauri)   │  ┌─────────┐    ┌───────────────────┐   │
                         │  │ HTTP /  │    │ Tool-use Session  │   │
   POST /v1/messages ───▶│  │ SSE    ─┼───▶│                   │   │
   {tools:[...]}         │  │ writer  │    │  Event Queue ◀────┼──┐│
                         │  └─────────┘    │  Pending Futures  │  ││
   ◀── tool_use block ───│                 │     {id: Future}  │  ││
   ◀── stop_reason:      │                 └────────┬──────────┘  ││
       tool_use          │                          │             ││
                         │                          ▼             ││
   POST /v1/messages ───▶│                 ┌─────────────────────┐││
   {messages:[..,        │                 │ ClaudeSDKClient     │││
     tool_result],       │                 │   ├─ MCP bridge ────┘│
     session_id: X}      │                 │   │  (mirrors tools  │
                         │                 │   │   from request)  │
   ◀── more text ────────│                 │   └─ receive_response│
   ◀── (maybe more       │                 │      pumps queue ────┘
        tool_use)        │                 └─────────────────────┘
   ◀── stop_reason:      │
       end_turn          │
                         └──────────────────────────────────────────┘
```

Three new pieces, one new piece of session state.

## Components

### 1. Per-session MCP bridge

When a request arrives with `tools: [...]` declared, build an in-process MCP server whose tool list **mirrors** the client's declaration. The handlers don't execute the work — they suspend.

```python
# conduit/tool_bridge.py  (sketch)
from claude_agent_sdk import tool, create_sdk_mcp_server

def build_bridge(tool_schemas: list[dict], session: ToolSession):
    """Return an SDK MCP server that suspends each call as a Future."""
    tools = []
    for spec in tool_schemas:
        name = spec["name"]
        description = spec.get("description", "")
        input_schema = spec["input_schema"]

        async def handler(args, _spec=spec):
            tool_use_id = args.pop("__tool_use_id")    # injected by us
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            session.pending[tool_use_id] = fut
            session.events.put_nowait({"type": "tool_call_pending", "id": tool_use_id})
            try:
                result = await asyncio.wait_for(fut, timeout=session.tool_timeout_s)
            except asyncio.TimeoutError:
                session.pending.pop(tool_use_id, None)
                raise
            return result      # the SDK feeds this back to Claude

        tools.append(tool(name=name, description=description, input_schema=input_schema)(handler))

    return create_sdk_mcp_server(name="conduit-bridge", tools=tools)
```

Bind the bridge into `ClaudeAgentOptions.mcp_servers` when creating the `ClaudeSDKClient`. The client's `tools` field in the request is what the model sees; the bridge is what gets invoked when the model picks one.

**Note on `__tool_use_id`:** the SDK calls our handler with parsed args from the model's `tool_use.input`. We don't get the `tool_use_id` for free in the handler — we have to correlate by reading the upstream SSE events. When the SDK emits `content_block_start` with `{"type": "tool_use", "id": "toolu_...", "name": "...", ...}` we record `(name → id)`, and the handler picks the most recent pending id for its name. (See open question #1 below for a cleaner correlation if the SDK exposes it.)

### 2. Auto-allocated session + `x-conduit-session-id` header

Today, omitting `session_id` means "create ephemeral, tear down after this request." That breaks for tool use because we need state to survive between the initial turn (parked) and the resume (delivers `tool_result`).

New rule: **if `tools` is present and `session_id` is absent, auto-allocate one and surface it in the response header `x-conduit-session-id`.** The client uses that on the follow-up `tool_result` request.

```python
# conduit/routes/messages.py  (additions, sketch)
auto_session = req.session_id is None and bool(req.tools)
if auto_session:
    session = await manager.create(model=req.model, tools_spec=req.tools)
    response_headers = {"x-conduit-session-id": session.id}
else:
    ...
```

Sessions auto-allocated this way are marked `ephemeral_unless_pending`: if the client never returns with a `tool_result`, the timeout sweeper (see #4) reaps them.

### 3. Resume logic

A follow-up request looks like:

```json
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 1024,
  "session_id": "auto-allocated-id",
  "tools": [/* same tool list as initial request */],
  "messages": [
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": [
       {"type": "text",     "text": "Let me check that."},
       {"type": "tool_use", "id": "toolu_01...", "name": "get_weather", "input": {...}}
    ]},
    {"role": "user", "content": [
       {"type": "tool_result", "tool_use_id": "toolu_01...", "content": "72°F sunny"}
    ]}
  ]
}
```

Server logic on receipt:

1. Look up `session_id`. If session has pending Futures matching the `tool_use_id`s in the request's trailing `tool_result` blocks, this is a **resume**, not a new turn.
2. For each `tool_result` block: resolve `session.pending.pop(tool_use_id).set_result(content)`.
3. Open a fresh SSE response and **continue draining** `session.events` — the SDK's queue resumes pumping as soon as the Future resolves.
4. If the resume contains a `tool_result` for a `tool_use_id` we don't know about → 400 `{"type": "invalid_request_error", "message": "unknown tool_use_id"}`.

```python
def _is_resume(req: MessageCreateRequest) -> list[ToolResultBlock] | None:
    last = req.messages[-1] if req.messages else None
    if not last or last.get("role") != "user":
        return None
    content = last.get("content")
    if not isinstance(content, list):
        return None
    results = [b for b in content if b.get("type") == "tool_result"]
    return results or None
```

### 4. Timeout & cleanup

New setting:

```python
# conduit/config.py
tool_result_timeout_s: int = 120       # CONDUIT_TOOL_RESULT_TIMEOUT_S
```

Two enforcement points:

1. **Inside the suspended handler:** `asyncio.wait_for(fut, timeout=...)` raises `TimeoutError` after `tool_result_timeout_s`. The SDK propagates this back to Claude as a tool error.
2. **Session sweeper:** existing idle-eviction loop checks for sessions whose `pending` dict is non-empty *and* `last_used_at < now - tool_result_timeout_s`. Reap them — `__aexit__` on the SDK client unwinds the suspended task.

## SSE event sequence (verbatim Anthropic format)

What Conduit emits when the model decides to use a tool. The Rust parser at `commands/chat.rs:483` already handles each of these.

```
event: message_start
data: {"type":"message_start","message":{
  "id":"msg_...", "role":"assistant", "model":"...",
  "content":[], "stop_reason":null, "stop_sequence":null,
  "usage":{"input_tokens":N,"output_tokens":1}}}

# optional pre-tool reasoning text
event: content_block_start
data: {"type":"content_block_start","index":0,
  "content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,
  "delta":{"type":"text_delta","text":"Let me check the weather."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

# the tool_use block
event: content_block_start
data: {"type":"content_block_start","index":1,
  "content_block":{"type":"tool_use","id":"toolu_01...",
    "name":"get_weather","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,
  "delta":{"type":"input_json_delta","partial_json":"{\"city\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,
  "delta":{"type":"input_json_delta","partial_json":"\"NYC\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

# the pause
event: message_delta
data: {"type":"message_delta",
  "delta":{"stop_reason":"tool_use","stop_sequence":null},
  "usage":{"output_tokens":M}}

event: message_stop
data: {"type":"message_stop"}
```

Then the HTTP response **ends**. The same session is parked, waiting for a `tool_result`. Resume emits a fresh `message_start … message_stop` sequence — typically ending in `stop_reason: end_turn` or another `stop_reason: tool_use` round.

### Forwarding-vs-synthesizing rules

For tool support, the existing streaming translator changes from "filter everything that isn't text" to:

| SDK event type | Action |
|---|---|
| `message_start` | Forward (with model rewritten as today) |
| `content_block_start` type=`text` | Forward, renumber index |
| `content_block_start` type=`thinking` | Skip block entirely (unchanged) |
| `content_block_start` type=`tool_use` | **Forward, renumber index, remember `(name → id, sdk_idx → out_idx)`** |
| `content_block_delta` type=`text_delta` | Forward with renumbered index |
| `content_block_delta` type=`thinking_delta`/`signature_delta` | Skip |
| `content_block_delta` type=`input_json_delta` | **Forward with renumbered index** |
| `content_block_stop` | Forward only if its block was forwarded; otherwise skip |
| `message_delta` `stop_reason=tool_use` | **Forward, then close the SSE response** — session stays alive |
| `message_delta` other | Forward |
| `message_stop` | Forward |

## Schema additions

```python
# conduit/schema.py
from anthropic.types import ToolParam, ToolUseBlock, ToolResultBlockParam

class MessageCreateRequest(BaseModel):
    # existing fields...
    tools: list[ToolParam] | None = None
    tool_choice: dict | None = None    # {"type": "auto" | "any" | "tool", "name": ...}
```

We rely on `anthropic.types.ToolParam` for input-schema validation parity, same pattern as the rest of `schema.py`.

## Test plan (the 7 cases referenced)

Add to `tests/test_tool_use.py`. All require the running server + Claude Max OAuth.

1. **`test_no_tool_use_when_not_needed`** — declare tools, ask a question that doesn't need them. Response is plain text, `stop_reason: end_turn`, no `tool_use` block.
2. **`test_single_tool_call_round_trip`** — declare one tool, ask a question that needs it. Verify first response has `stop_reason: tool_use`, second request with `tool_result` produces final text with `stop_reason: end_turn`.
3. **`test_parallel_tool_calls`** — model emits multiple `tool_use` blocks in one turn. Resume request must include matching `tool_result` for each id.
4. **`test_unknown_tool_use_id_rejected`** — resume request with a fabricated `tool_use_id`. Server returns 400 invalid_request_error.
5. **`test_tool_result_timeout`** — initial request triggers `tool_use`, client never resumes. After `CONDUIT_TOOL_RESULT_TIMEOUT_S`, session is reaped from `/v1/sessions`.
6. **`test_x_conduit_session_id_header`** — initial request omits `session_id`. Response carries `x-conduit-session-id`. Using it on the resume continues correctly.
7. **`test_compat_with_official_sdk_tool_use`** — exercise the full round trip via the official `anthropic` Python SDK's `tool_use` helpers, not raw httpx. This is the killer compat test, same spirit as `test_compatibility.py`.

Optional 8th if we ship `tool_choice`: **`test_tool_choice_forces_specific_tool`**.

## Probe findings (Phase 1, complete)

`scratch/sdk_tool_probe.py` answered all four open questions plus uncovered one mandatory setting:

1. **`tool_use_id` correlation — resolved by upstream-event correlation.** The `@tool` handler receives a plain `dict` of the model's parsed input (e.g. `{"city": "NYC"}`). No context object, no `tool_use_id` kwarg. We must correlate ourselves: when we see `content_block_start` with `{"type": "tool_use", "id": "toolu_...", "name": "X"}`, record `(name → queue of ids)`. When the handler for `X` fires, pop the next pending id from that queue. Single-tool-per-turn is trivial; parallel calls need the queue.

2. **`receive_response()` keeps emitting while the handler is parked — yes.** Events `content_block_stop` (tool_use block), `message_delta` with `stop_reason: tool_use`, and `message_stop` all flow *after* the handler is invoked but *before* it returns. The SDK's loop then idles until our Future resolves. **Implication:** the HTTP SSE response can complete naturally — no synthesis needed, no early break — and the SDK stays alive in the background.

3. **`message_delta` ordering — favourable.** Sequence is exactly: `content_block_start (tool_use)` → `input_json_delta`s → handler invoked → `content_block_stop` → `message_delta (stop_reason: tool_use)` → `message_stop`. Matches Anthropic's wire format. **Forwarding 1:1 just works.**

4. **Tool name format — confirmed `mcp__<server>__<tool>`.** Whatever string we put in `ClaudeAgentOptions.tools` must match exactly.

5. **Mandatory: `permission_mode='bypassPermissions'`.** Without this, the SDK's permission gate silently intercepts MCP tool calls before reaching the handler; the model receives a permission denial and the response is "this tool requires authorization." Conduit must set `permission_mode='bypassPermissions'` whenever a request declares `tools` — the trust boundary is the *client*, which already owns its tool list.

### Engineering choice triggered by the findings

Since events keep flowing while the handler is parked, we have two viable shapes:

- **(A) Single shared iterator on the session.** `receive_response()` is one async generator; we iterate it from the HTTP handler until `message_stop` after a tool_use, return from HTTP, then on resume the next HTTP handler resumes iterating the same generator.
- **(B) Background pump into a queue.** A task always drains `receive_response()` into `session.events`. HTTP handlers read from the queue.

(A) is fewer moving parts but couples the SDK lifecycle to the HTTP request lifecycle in subtle ways (cancellation, exception propagation). **(B) is the safer choice** and is what the architecture diagram already implies. Go with (B).

### Session lock during pause

The current `Session.lock` serializes turns end-to-end. For tool-use the resume request must take the same lock — but the SDK is mid-loop, still holding session state. Replace `lock: asyncio.Lock` with a state machine on the session:

```python
class SessionState(StrEnum):
    IDLE = "idle"
    STREAMING = "streaming"        # HTTP holding the response open
    PAUSED_FOR_TOOLS = "paused"    # SDK parked on Futures, no HTTP attached
```

Transitions:
- `IDLE → STREAMING`           — new turn begins
- `STREAMING → IDLE`           — final `message_stop` with `stop_reason: end_turn`
- `STREAMING → PAUSED_FOR_TOOLS` — `message_stop` after `stop_reason: tool_use`, HTTP closes
- `PAUSED_FOR_TOOLS → STREAMING` — resume request arrives with matching `tool_result`s, Futures resolved
- any → reaped by timeout sweeper

## Phased rollout

1. **Phase 1 — probe** (1 hour). Write `scratch/sdk_tool_probe.py` that defines one `@tool` handler, queries Claude with a tool-requiring prompt, and prints every event/argument the SDK exposes. Answer open questions #1–3. ✅ Done.
2. **Phase 2 — single-tool, single-call** (half day). Implement bridge + auto session + resume for one tool. ✅ Done.
3. **Phase 2.5 — multi-cycle** (free, already worked). The pump-stays-alive design naturally supports N sequential `tool_use → tool_result → tool_use → ... → end_turn` cycles within one user turn. Pinned by `tests/test_multicycle_tools.py`. See AGENTIC_INTEGRATION.md §5.6.
4. **Phase 3 — parallel + tool_choice** (half day). Parallel `tool_use` blocks in a single message (per-name FIFO is in place, just needs end-to-end test). Wire `tool_choice` to the SDK if there's a way.
5. **Phase 4 — compat parity** (1 hour). Run the official `anthropic` SDK's tool-use flow against Conduit. ✅ Done.
