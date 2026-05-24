# AGENTIC_INTEGRATION

Everything an AI coding agent (or human dev) needs to integrate a client
against a running Conduit server. Self-contained — you should not need to
read the rest of the docs to build a working integration.

---

## 0. TL;DR card

| Fact | Value |
|---|---|
| Base URL (default) | `http://127.0.0.1:8765` |
| Auth | **None.** Bound to loopback, single-user. |
| Wire format | **Identical** to Anthropic's `https://api.anthropic.com/v1/messages` |
| Auth header on requests | Either omit, or send any non-empty `x-api-key` (the official Anthropic SDK requires one — value is ignored) |
| Content-Type for POSTs | `application/json` |
| Streaming format | Server-Sent Events (`text/event-stream`) |
| Conduit-specific extensions | `session_id` field, `effort` field, `include_thinking` field, `x-conduit-session-id` response header |
| Drop-in OK | Yes — pointing the official `anthropic` SDK at the base URL works for chat *and* streaming. For Conduit-only fields like `effort` / `include_thinking`, use `extra_body={...}` |
| Tool support | Custom tools (pause/resume) and hosted server tools (no pause). **Sequential multi-cycle** within one user turn is supported — see §5.6. Parallel `tool_use` blocks in a single message work in principle (per-name FIFO id queue) but not exhaustively tested. |
| Thinking budget | `effort: "low" \| "medium" \| "high" \| "xhigh" \| "max"` — Conduit extension, bound at session creation. See §4.4 and `EFFORT_INTEGRATION.md` for the deep dive. |
| Hosted server tools | `WebSearch` and `WebFetch` — declare Anthropic-style in `tools[]`, SDK executes internally. Response carries `server_tool_use` + `web_search_tool_result` / `web_fetch_tool_result` blocks alongside the final `text`, all with `stop_reason: end_turn`. **No pause/resume**. See §5.7 and `WEBSEARCH_INTEGRATION.md` / `PASSTHROUGH_INTEGRATION.md`. |

---

## 1. Server contract

### 1.1 Where it runs

- Bound to `127.0.0.1` only (LAN-invisible) by default.
- Port via `CONDUIT_PORT` env var on the server side. Default `8765`.
- Server is **single-user, single-host**. No rate limiting, no auth.

### 1.2 Health check

```
GET /health   →   200 {"status":"ok"}
```

Use this to verify the server is up before your first real request. A successful health check does **not** guarantee Claude Max OAuth is set up — that only fails on a real `/v1/messages` call.

### 1.3 Auto OpenAPI / Swagger UI

`GET /docs` serves the full interactive spec. Useful for manual testing. `GET /openapi.json` is the raw schema.

---

## 2. Endpoint reference

### 2.1 `POST /v1/messages`

The chat endpoint. Mirrors Anthropic's `/v1/messages` exactly, plus `session_id` extension.

**Request body (JSON):**

| Field | Type | Required | Notes |
|---|---|---|---|
| `model` | string | ✅ | e.g. `"claude-sonnet-4-6"`, `"claude-haiku-4-5-20251001"`. **Bound at session creation** for stateful sessions — see §4.2. |
| `messages` | `MessageParam[]` | ✅ | Standard Anthropic shape (see §3). |
| `max_tokens` | int | ✅ | Standard Anthropic. |
| `system` | string \| `TextBlockParam[]` | ❌ | System prompt. |
| `stream` | bool | ❌ default `false` | `true` → SSE; `false` → single JSON object. |
| `temperature` | float | ❌ | Standard Anthropic. |
| `stop_sequences` | string[] | ❌ | Standard Anthropic. |
| `tools` | `ToolUnionParam[]` | ❌ | Mix of custom function tools and/or hosted server tools (WebSearch/WebFetch). Conduit splits internally. See §5.2 (custom single-cycle), §5.6 (custom multi-cycle), §5.7 (hosted, no pause). |
| `tool_choice` | object | ❌ | Accepted but **currently ignored** in Phase 2. |
| **`effort`** | `"low" \| "medium" \| "high" \| "xhigh" \| "max"` | ❌ | **Conduit extension.** Maps to the Agent SDK's `effort` field — controls thinking budget. Bound at session creation; ignored on subsequent turns of a stateful session. |
| **`include_thinking`** | bool | ❌ default `false` | **Conduit extension.** When true, response includes `thinking` content blocks (Anthropic-canonical). The model thinks regardless; this flag only controls forwarding. Bound at session creation. See `THINKING_INTEGRATION.md`. |
| **`session_id`** | string | ❌ | **Conduit extension.** Without it: stateless or auto-allocated. With it: continue an existing session. See §4. |

**Response — `stream=false`:** the standard Anthropic `Message` object, JSON.

```json
{
  "id": "msg_01...",
  "type": "message",
  "role": "assistant",
  "model": "claude-haiku-4-5-20251001",
  "content": [{"type": "text", "text": "..."}],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {"input_tokens": 12, "output_tokens": 34}
}
```

**Response — `stream=true`:** SSE event stream. See §3.4.

**Response headers (Conduit-specific):**

- `x-conduit-session-id: <uuid>` — present on every response when a session is involved (stateful chat OR tool-use). Capture this on the **first** tool-use response so you can send `tool_result`s back on the resume request. See §5.

### 2.2 `GET /v1/sessions`

```
GET /v1/sessions  →  200 {"sessions": [SessionInfo, ...]}
```

```json
{"sessions": [
  {
    "session_id": "uuid",
    "created_at": 1779414987.85,
    "last_used_at": 1779414991.40,
    "message_count": 2
  }
]}
```

### 2.3 `POST /v1/sessions`

Explicit session creation (for stateful chat without tools). For tool-use you generally don't call this — the server auto-allocates.

**Request:**
```json
{
  "system_prompt": "be brief",
  "model": "claude-haiku-4-5-20251001",
  "effort": "high"
}
```
All fields optional. Empty body `{}` is valid. `effort` is locked at this moment and used for every turn through the resulting session.

**Response:**
```json
{"session_id": "uuid"}
```

### 2.4 `DELETE /v1/sessions/{session_id}`

```
DELETE /v1/sessions/abc-123  →  200 {"deleted": true}
                             →  404 {"detail": {"type": "invalid_request_error", "message": "session abc-123 not found"}}
```

Use to clean up sessions you're done with. Server also evicts sessions after `CONDUIT_SESSION_IDLE_TIMEOUT_S` seconds (default 1800) of no use.

### 2.5 `GET /health`

See §1.2.

---

## 3. Wire format details

### 3.1 `MessageParam` (entries in `messages`)

```json
{"role": "user" | "assistant", "content": <string OR list of ContentBlock>}
```

`ContentBlock` types you'll use:

| `type` | Other fields | Used by |
|---|---|---|
| `"text"` | `"text": str` | Either role |
| `"tool_use"` | `"id": str`, `"name": str`, `"input": obj` | Assistant only (server emits these) |
| `"tool_result"` | `"tool_use_id": str`, `"content": str OR ContentBlock[]` | **User only** (you send these on resume) |
| `"image"` | `"source": {...}` | User (not Phase 2 tested) |

### 3.2 Tool declarations (entries in `tools`)

Two kinds, both Anthropic-compatible:

**Custom function tool** (client implements; pauses for `tool_result`):

```json
{
  "name": "get_weather",
  "description": "Get current weather for a city.",
  "input_schema": {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"]
  }
}
```

**Hosted server tool** (SDK executes; no pause):

```json
// WebSearch — `name` must be the literal "web_search"
{"type": "web_search_20250305", "name": "web_search"}
{"type": "web_search_20260209", "name": "web_search"}

// WebFetch — `name` must be the literal "web_fetch"
{"type": "web_fetch_20250910", "name": "web_fetch"}
{"type": "web_fetch_20260209", "name": "web_fetch"}
{"type": "web_fetch_20260309", "name": "web_fetch"}
```

⚠️ **Both `type` and `name` are required, and `name` is a pydantic literal validated against Anthropic's schema** — you cannot rename them. Sending `{"name": "WebSearch"}`, `{"name": "web_search_sdk"}`, or omitting `name` produces a 422 before Conduit ever sees the request. Use the exact canonical strings.

The `type` prefix (`web_search` / `web_fetch`) is what triggers Conduit's hosted detection; the version date is forwarded to the SDK.

**Name collision with custom function tools.** Because hosted names are fixed, you cannot have a custom function tool also named `web_search` in the same request. Dedupe client-side: if the request already declares a tool with that name, skip injecting the hosted variant.

Custom-tool names are **bare** on both directions of the wire. The server's internal MCP prefix (`mcp__conduit__X`) is invisible to clients.

### 3.3 Non-streaming response shape

See §2.1. `content` is an ordered array of blocks — typically `[text]` for plain chat, `[text?, tool_use]` for a tool-using turn.

### 3.4 SSE event sequence

When `stream=true`, the response body is a sequence of SSE events in this order. **Every event has both `event:` and `data:` lines.** The `data:` value is a JSON object.

**Plain chat turn:**
```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant","model":"...","content":[],"stop_reason":null,"usage":{...}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}

# ... more text_delta events ...

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":15}}

event: message_stop
data: {"type":"message_stop"}
```

After `message_stop` the connection closes.

**Tool-use turn (pause):**
```
event: message_start
data: {...}

# Optional pre-tool text block:
event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Let me check."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

# THE tool_use block — at the next free index (here 1):
event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_01...","name":"get_weather","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"city\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\"NYC\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

# THE pause — stop_reason is tool_use, not end_turn:
event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":42}}

event: message_stop
data: {"type":"message_stop"}
```

Then the connection closes. The server keeps the session alive, waiting for your resume request.

### 3.5 Index renumbering

The Agent SDK emits thinking blocks at index 0 internally; Conduit **filters those out** and renumbers the remaining (text + tool_use) blocks so visible indices start at 0 and are contiguous. You don't need to know the original indices — just treat them as 0-based and dense.

### 3.6 Input JSON reassembly

The model streams `tool_use.input` as multiple `input_json_delta` events whose `partial_json` strings, concatenated in order, form one JSON document. **For non-streaming responses Conduit already does the reassembly** — `content[i].input` is a parsed object. For streaming you must accumulate the `partial_json` strings across deltas yourself, then `JSON.parse` once at `content_block_stop`.

---

## 4. Modes of operation

### 4.1 Stateless (Anthropic-style)

Omit `session_id`. Send full message history each call. Server creates an ephemeral session per request and tears it down.

```http
POST /v1/messages
{ "model": "...", "max_tokens": 256, "messages": [...] }
```

### 4.2 Stateful chat (no tools)

Three paths, pick whichever fits your client's model:

1. **Pattern A (stateless, Anthropic-drop-in).** Omit `session_id`, send the full message history in `messages[]` every turn. Server creates a fresh session per request and **replays the entire history** into the prompt so the model has context. Most compatible with code written against the upstream Anthropic API. The response still carries `x-conduit-session-id` if you want to switch to Pattern B later.

2. **Pattern B (stateful, Conduit-native).** Capture `x-conduit-session-id` from a response and reuse it on subsequent turns, sending **only the new user message** in `messages[]`. Server maintains conversation state via the Agent SDK; fewer tokens per request, faster after the first turn.

3. **Explicit creation.** `POST /v1/sessions` first (lets you set `system_prompt`/`model`/`effort`/`include_thinking` up front), then use the returned `session_id` like Pattern B.

You can also send `session_id` **and** full history — Conduit reads only the last user message in stateful mode, so the redundant history is ignored. Works (no harm) but wastes bandwidth.

**Pure-stateless WITHOUT history doesn't work.** Sending `[{role: "user", content: "follow up"}]` with no `session_id` and no prior turns means the server has no context. The model will respond as if it's the first message ever. This is the most common "multi-turn chat is broken" bug — fix by switching to Pattern A or B above.

### 4.2a Prompt caching (latency)

Conduit does **not** propagate Anthropic's `cache_control: {type: "ephemeral"}` field on message blocks — the text-extraction layer strips it. But it doesn't matter for the common case, because **the Agent SDK auto-caches the session's context aggressively** (uses Anthropic's 1-hour ephemeral cache tier by default — longer than the standard 5-min cache). Concretely:

- The first call on a session writes the system prompt + SDK-internal context to cache (`usage.cache_creation_input_tokens` reports how much).
- Every subsequent call on the **same session** reads from that cache (`usage.cache_read_input_tokens` reports how much). Big latency win on repeated requests with the same system prompt.

**Implication for choosing a pattern:**

| | Cache hit rate |
|---|---|
| Pattern A (no `session_id`, full history each request) | **0%** — new ephemeral session per request, cache cold every time. |
| Pattern B (reuse `session_id`) | **High** — system prompt + context cached on turn 1, hits on turns 2+. |

If you're sending the same ≥600-char system prompt on every paste/request and care about latency, **switch to Pattern B**. The `usage` block in every response now carries `cache_creation_input_tokens` and `cache_read_input_tokens` so you can observe cache effectiveness directly.

Example (probe numbers, Haiku, 600-char system prompt, same session reused):

```
Turn 1 (cold):  input=9  cache_create=744   cache_read=37918  output=373
Turn 2:         input=9  cache_create=384   cache_read=38662  output=232
Turn 3:         input=9  cache_create=245   cache_read=39046  output=245
```

(The 37K-token `cache_read` from turn 1 is the SDK's internal Claude Code context — it was warm from a prior unrelated session in the same hour. Your own system prompt is the 744 tokens cached on turn 1.)

**Model binding:** the model is fixed at session creation. The `model` field in subsequent requests through that session is **ignored at the SDK layer** but echoed in the response. If you need a different model, create a new session.

**Effort binding:** identical rule. `effort` is read once when the session is created and applies to every turn through that session. Sending a different `effort` on a follow-up request is silently ignored.

### 4.3 Tool-use

See §5.

### 4.4 Conduit-specific extensions

Three fields/headers Conduit adds on top of the Anthropic wire format. All optional; all designed so that a request without them behaves exactly like upstream Anthropic.

| Extension | Where | Type | Purpose |
|---|---|---|---|
| `session_id` | request body | string | Continue an existing server-side session. See §4.2, §5. |
| `effort` | request body | `"low" \| "medium" \| "high" \| "xhigh" \| "max"` | Set thinking-budget at session creation. Pydantic-validated; bad values → 422. |
| `include_thinking` | request body | bool | When true, response carries `thinking` content blocks. Bound at session creation. Pure forwarding switch — doesn't affect generation. |
| `x-conduit-session-id` | response header | string (uuid) | The session this response was served from. Auto-allocated for tool-use turns; capture it to resume. |

Defaults are sourced from the server's env (`CONDUIT_DEFAULT_MODEL`, `CONDUIT_DEFAULT_EFFORT`) if you omit a field. If the env var is also unset, the SDK's own defaults apply.

For the deep dive on `effort` — values, latency/cost expectations, code recipes for each language — see **`EFFORT_INTEGRATION.md`**.

---

## 5. The tool-use pause/resume protocol

This is the only really non-obvious part. Read it carefully.

### 5.1 Conceptual model

The Anthropic API is **pause-based**: the model emits a `tool_use` block, the response ends with `stop_reason: "tool_use"`, the client executes the tool, then sends a *new* request whose `messages` array ends in a `tool_result` block.

Conduit preserves this protocol identically. The only addition: because the Agent SDK under the hood is loop-based, Conduit keeps state on the server between the pause and the resume — addressed by `session_id`. That state lives in `x-conduit-session-id`, which is automatically issued on the first response.

### 5.2 Round trip (single tool, single call)

**Step 1 — Initial request.** Don't send `session_id`.

```json
POST /v1/messages
{
  "model": "claude-haiku-4-5-20251001",
  "max_tokens": 512,
  "tools": [{
    "name": "get_weather",
    "description": "Get current weather for a city.",
    "input_schema": {
      "type": "object",
      "properties": {"city": {"type": "string"}},
      "required": ["city"]
    }
  }],
  "messages": [
    {"role": "user", "content": "What's the weather in Paris?"}
  ]
}
```

**Step 2 — Server response.** Two things to capture:

- HTTP header: `x-conduit-session-id: <uuid>` ← **save this**
- Body: a Message with `stop_reason: "tool_use"` and a `tool_use` block in `content[]`:

```json
{
  "id": "msg_01...",
  "stop_reason": "tool_use",
  "content": [
    {"type": "text", "text": "Let me check that."},   // optional
    {"type": "tool_use",
     "id": "toolu_01...",                              // ← save this
     "name": "get_weather",                            // bare name
     "input": {"city": "Paris"}}
  ],
  "usage": {...}
}
```

**Step 3 — Execute the tool client-side.** Whatever your tool does, produce a result. Can be a string or list of content blocks.

**Step 4 — Resume request.** Send back **with `session_id`** and the trailing `tool_result`:

```json
POST /v1/messages
{
  "model": "claude-haiku-4-5-20251001",
  "max_tokens": 512,
  "session_id": "<the uuid from step 2 header>",
  "tools": [/* same tools array as step 1 */],
  "messages": [
    {"role": "user",      "content": "What's the weather in Paris?"},
    {"role": "assistant", "content": [/* the assistant's content from step 2, verbatim */]},
    {"role": "user",      "content": [
       {"type": "tool_result",
        "tool_use_id": "toolu_01...",   // matches step 2's tool_use.id
        "content": "It's 68°F and partly cloudy."}
    ]}
  ]
}
```

**Required on resume:**
- `session_id` (body field) — must match the header from step 2
- `tools` declared (same list) — required for schema validation; the server uses the session's bridge, not this list
- The last message must be `role: user` with a `content` list containing at least one `tool_result` block whose `tool_use_id` matches what the server is parked on

**Optional on resume:**
- Full message history. The server inspects only the trailing `tool_result` blocks; the rest is decorative for upstream-Anthropic parity. You can send placeholder text in earlier messages without affecting behavior.

**Step 5 — Server response.** Same shape as a normal Anthropic response: typically `stop_reason: "end_turn"` with the model's text answer that incorporates the tool result.

### 5.3 `tool_result.content` shapes

```json
// Plain text
{"type": "tool_result", "tool_use_id": "toolu_01...", "content": "72F sunny"}

// List of content blocks (multiple text, images, etc.)
{"type": "tool_result", "tool_use_id": "toolu_01...", "content": [
  {"type": "text", "text": "Result for Paris:"},
  {"type": "text", "text": "{\"temp_f\": 72, \"sky\": \"sunny\"}"}
]}

// Errors:
{"type": "tool_result", "tool_use_id": "toolu_01...", "is_error": true, "content": "City not found"}
```

### 5.4 Session lifecycle for tool-use

- Initial tool-use request **auto-allocates** the session (and surfaces the id via header).
- Session persists across pause → resume.
- After the final `stop_reason: "end_turn"` you may either:
  - Let it die by idle timeout (default 30 min), or
  - `DELETE /v1/sessions/{id}` to clean up immediately, or
  - **Reuse it for another user turn** (Pattern B) — works fine; the SDK retains conversation state.

### 5.5 Errors specific to resume

| Status | When |
|---|---|
| 400 | `tool_result` block missing `tool_use_id` |
| 400 | `tool_use_id` doesn't match a parked Future on this session |
| 400 | `tool_result` present but no `session_id` in body |
| 400 | `session_id` is for a non-tool session |
| 404 | `session_id` not found (likely timed out) — start over |

### 5.6 Multi-cycle tool use (N sequential tool calls in one turn)

The model is allowed to chain custom tool calls within a single user turn — e.g. `list_topics → tool_result → topic_coverage → tool_result → text → end_turn`. Each pause is its own HTTP round-trip but they all share the same `session_id`.

**The protocol is identical to §5.2 single-cycle — just repeat until `stop_reason: end_turn`.** No special "cycle number" header, no per-cycle session_id rotation. Every cycle:

1. Server response has `stop_reason: "tool_use"` and a `tool_use` block in `content[]`
2. Client executes the tool, builds the next request:
   - Append the assistant's last `content[]` (the `tool_use` block) to `messages[]`
   - Append a new user message with a `tool_result` block carrying the *current* cycle's `tool_use_id`
   - Send with the **same** `session_id` from the previous response's header
3. Loop until the server returns `stop_reason: "end_turn"`

Each cycle's `tool_use.id` is **unique** — don't reuse them across cycles, and don't reuse one for the wrong cycle (server returns 400 `unknown tool_use_id`).

**Cycle 2+ request body shape:**

```json
{
  "model": "...",
  "max_tokens": 600,
  "session_id": "<same uuid from cycle 1's header>",
  "tools": [/* same tools list */],
  "messages": [
    {"role": "user",      "content": "<original user question>"},
    {"role": "assistant", "content": [/* tool_use block from cycle 1, verbatim */]},
    {"role": "user",      "content": [
      {"type": "tool_result", "tool_use_id": "<cycle 1's id>", "content": "<cycle 1's result>"}
    ]},
    {"role": "assistant", "content": [/* tool_use block from cycle 2, verbatim */]},
    {"role": "user",      "content": [
      {"type": "tool_result", "tool_use_id": "<cycle 2's id>", "content": "<cycle 2's result>"}
    ]}
  ]
}
```

Server inspects only the trailing `tool_result` block in `messages[-1].content`. The earlier turns are decorative for upstream-Anthropic parity (and required by pydantic's schema validation), but the server doesn't need to re-read them — the session's SDK conversation state already remembers everything.

**Python recipe (Pattern A-style — works for both single and multi cycle):**

```python
import httpx, json

URL = "http://127.0.0.1:8765"
MODEL = "claude-haiku-4-5-20251001"
TOOLS = [...]

def execute_tool(name, args):
    ...  # your tool implementations
    return "<result string>"

history = [{"role": "user", "content": "<user prompt>"}]
sid = None

# Initial request
r = httpx.post(URL + "/v1/messages", json={
    "model": MODEL, "max_tokens": 600,
    "tools": TOOLS,
    "system": "...",
    "messages": history,
}, timeout=180)
sid = r.headers["x-conduit-session-id"]
msg = r.json()

# Loop while the model wants more tools
while msg["stop_reason"] == "tool_use":
    tu = next(b for b in msg["content"] if b["type"] == "tool_use")
    result = execute_tool(tu["name"], tu["input"])

    history.append({"role": "assistant", "content": msg["content"]})
    history.append({"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": tu["id"],
        "content": result,
    }]})

    r = httpx.post(URL + "/v1/messages", json={
        "model": MODEL, "max_tokens": 600,
        "tools": TOOLS,
        "session_id": sid,
        "messages": history,
    }, timeout=180)
    msg = r.json()

# stop_reason is now end_turn; msg["content"] has the final text
final_text = "".join(b["text"] for b in msg["content"] if b["type"] == "text")
httpx.delete(URL + f"/v1/sessions/{sid}", timeout=10)
```

**Streaming the resume cycles.** Same as §3.4 single-cycle, just repeated. Each cycle's SSE response starts with `message_start` and ends with `message_stop`. If that cycle ended with another `tool_use`, the `message_delta` carries `stop_reason: "tool_use"` and you start cycle N+1; if it carries `stop_reason: "end_turn"` you're done. The cycle 2+ stream looks identical to cycle 1's stream, with possibly different content (text-then-tool_use, or just-tool_use, or just-text depending on what the model chose).

**Limits in current implementation:**
- No hard cap on cycles within a turn (subject to the 30-min idle session timeout).
- Parallel tool calls in one cycle (model emitting two `tool_use` blocks in a single message) work in principle (per-name FIFO id queue is in place) but aren't exhaustively tested. Constrain via prompting if you want strict serialization.

See `scripts/multicycle_demo.py` for a runnable end-to-end example.

### 5.7 Hosted tools (WebSearch / WebFetch) — one request, no pause

For hosted server tools, the protocol simplifies dramatically. Declare them in `tools[]` and send **one** request:

```json
POST /v1/messages
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 512,
  "tools": [
    {"type": "web_search_20250305", "name": "web_search"},
    {"type": "web_fetch_20250910",  "name": "web_fetch"}
  ],
  "messages": [{"role": "user", "content": "What's the latest stable FastAPI?"}]
}
```

You get **one** response back, with `stop_reason: "end_turn"`. The Agent SDK ran the search/fetch internally; the response's `content[]` array carries:

- `server_tool_use` blocks — what the model called (`name`, `input`)
- `web_search_tool_result` / `web_fetch_tool_result` blocks — what the SDK fed back (correlated by `tool_use_id`)
- `text` blocks — the model's final synthesized answer (often with markdown citations)

The hidden SDK turn boundary (the internal `stop_reason: "tool_use"` and continuation `message_start`) is suppressed so it all looks like one continuous turn from the client's perspective.

| | Custom tools (`get_weather`) | Hosted tools (`WebSearch`/`WebFetch`) |
|---|---|---|
| Client requests per turn | 2+ (initial + `tool_result` resume) | **1** |
| Client implements the tool? | Yes | No — SDK executes |
| Tool-use blocks in `content[]` | `tool_use` (client must execute) | `server_tool_use` + `web_search_tool_result` / `web_fetch_tool_result` (already executed, visibility only) |
| `stop_reason` returned | `tool_use` → then `end_turn` | **`end_turn`** straight away |
| Where the loop lives | Your code | Inside the model |
| Session lifecycle | Persists across pause/resume | Auto-allocated and torn down after the response |

**Mixed-mode is supported.** You can declare hosted and custom tools in the same `tools[]`. The model picks; if it picks a hosted one you get a continuous turn, if it picks a custom one you get the standard `stop_reason: tool_use` pause and follow §5.2.

**The model decides when to invoke.** Declaring `web_search` doesn't force the model to use it — tools are advisory. Anchor with a system prompt if you want to discourage spurious searches:

```python
"system": "Only use web_search or web_fetch when the user is asking about "
          "recent events, current versions, or information you don't already "
          "know. For evergreen topics, answer from your training."
```

For the full hosted-tool reference (cost/latency, all accepted JSON shapes, debugging, Phase 2.5 limits like `allowed_domains` not yet wired through), see **`WEBSEARCH_INTEGRATION.md`**.

For the deep dive on the response shape — every block type, how to parse `*_tool_result.content`, how the SSE stream interleaves the new blocks, and what client-side code needs to change — see **`PASSTHROUGH_INTEGRATION.md`**.

---

## 6. Limitations (Phase 2)

The protocol is the Anthropic protocol — these are implementation limits, not wire incompatibilities. The client doesn't need workarounds, just awareness.

1. **Parallel tool calls in one cycle.** If the model emits two `tool_use` blocks in a single message (parallel tool calling), the per-name FIFO id queue handles it in principle but it's not exhaustively tested. Constrain via prompting if you need strict serialization.
2. ~~Single round per session.~~ **Supported.** N sequential `tool_use → tool_result` cycles within one user turn work — see §5.6 for the protocol. Multi-user-turn conversations on the same session (one tool sequence completes → user asks another question → another tool sequence) also work.
3. **`tool_choice` ignored.** You can send it; it doesn't constrain the model.
4. **Per-session model lock** — see §4.2.
5. **Session timeout** is 30 min default. If your tool execution takes longer than that, the resume request will 404. Move long-running work to async background and respond fast to Conduit.
6. **Thinking blocks filtered.** If you specifically need extended-thinking output, it's stripped from the stream.
7. **Hosted-tool `allowed_domains` / `blocked_domains` / `max_uses` not yet wired through.** Conduit accepts them on the request (schema validation passes) but doesn't propagate them to the SDK. The SDK uses its own defaults.
8. **Hosted-only sessions ARE reusable now** (was previously a limitation). The `x-conduit-session-id` header from a hosted-tool response can be reused on follow-up turns to maintain conversation context — same Pattern B behavior as plain chat.
9. **WebFetch doesn't render JavaScript.** Server-side fetch only — modern SPAs may return thin content.

---

## 7. Errors

Conduit's error envelope follows Anthropic's:

```json
{
  "detail": {
    "type": "invalid_request_error",
    "message": "session abc-123 not found"
  }
}
```

| Code | Meaning |
|---|---|
| 200 | Success (including SSE streams that complete normally). |
| 400 | Validation error — see `detail.message`. |
| 404 | `session_id` not found. |
| 422 | Pydantic validation failure on the request body (malformed JSON, missing required fields). `detail` is a list of pydantic errors. |
| 500 | Internal server error (SDK crash, network failure to Claude). |

Retry guidance:
- **404 on resume** → session expired, start the conversation over with a new initial request.
- **422** → fix the request body; don't retry.
- **500** → safe to retry once with backoff; if persistent, restart the server.

---

## 8. Code recipes

### 8.1 Python — official `anthropic` SDK (recommended for plain chat)

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

# Non-streaming
msg = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{"role": "user", "content": "Hello"}],
)
print(msg.content[0].text)

# Streaming
with client.messages.stream(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{"role": "user", "content": "Count to 5."}],
) as stream:
    for delta in stream.text_stream:
        print(delta, end="", flush=True)
```

The `api_key` must be **non-empty** (SDK validates), but the value is ignored — Conduit doesn't check it.

### 8.2 Python — httpx, tool-use round trip

```python
import httpx

URL = "http://127.0.0.1:8765"
MODEL = "claude-haiku-4-5-20251001"

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}

def fake_get_weather(city: str) -> str:
    return f"It's 72°F and sunny in {city}."

# --- Step 1: initial request ---
r1 = httpx.post(f"{URL}/v1/messages", json={
    "model": MODEL,
    "max_tokens": 512,
    "tools": [WEATHER_TOOL],
    "messages": [{"role": "user", "content": "What's the weather in Paris?"}],
}, timeout=60)
r1.raise_for_status()
session_id = r1.headers["x-conduit-session-id"]
msg1 = r1.json()
assert msg1["stop_reason"] == "tool_use"

tool_use = next(b for b in msg1["content"] if b["type"] == "tool_use")
tool_result_str = fake_get_weather(**tool_use["input"])

# --- Step 2: resume with tool_result ---
r2 = httpx.post(f"{URL}/v1/messages", json={
    "model": MODEL,
    "max_tokens": 512,
    "session_id": session_id,
    "tools": [WEATHER_TOOL],
    "messages": [
        {"role": "user",      "content": "What's the weather in Paris?"},
        {"role": "assistant", "content": msg1["content"]},
        {"role": "user",      "content": [{
            "type": "tool_result",
            "tool_use_id": tool_use["id"],
            "content": tool_result_str,
        }]},
    ],
}, timeout=60)
r2.raise_for_status()
msg2 = r2.json()
print(msg2["content"][0]["text"])

# Cleanup (optional — server idle-evicts after 30 min)
httpx.delete(f"{URL}/v1/sessions/{session_id}", timeout=10)
```

### 8.3 Python — httpx streaming with manual SSE parsing

```python
import httpx, json

def stream_chat(prompt: str):
    with httpx.stream("POST", "http://127.0.0.1:8765/v1/messages", json={
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }, timeout=60) as resp:
        resp.raise_for_status()
        event_name = None
        for line in resp.iter_lines():
            if not line:
                event_name = None
                continue
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("type") == "content_block_delta":
                    d = data["delta"]
                    if d["type"] == "text_delta":
                        print(d["text"], end="", flush=True)

stream_chat("Tell me a one-sentence joke.")
```

### 8.4 Rust — `reqwest` + `eventsource-stream` (chat streaming)

`Cargo.toml`:
```toml
reqwest = { version = "0.12", features = ["json", "stream"] }
eventsource-stream = "0.2"
serde_json = "1"
tokio = { version = "1", features = ["full"] }
futures = "0.3"
```

```rust
use eventsource_stream::Eventsource;
use futures::stream::StreamExt;
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let body = json!({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "stream": true,
        "messages": [{"role": "user", "content": "Hello!"}],
    });

    let mut stream = reqwest::Client::new()
        .post("http://127.0.0.1:8765/v1/messages")
        .json(&body)
        .send().await?
        .bytes_stream()
        .eventsource();

    while let Some(event) = stream.next().await {
        let ev = event?;
        if ev.event == "content_block_delta" {
            let data: serde_json::Value = serde_json::from_str(&ev.data)?;
            if data["delta"]["type"] == "text_delta" {
                print!("{}", data["delta"]["text"].as_str().unwrap_or(""));
            }
        } else if ev.event == "message_stop" {
            break;
        }
    }
    Ok(())
}
```

### 8.5 Rust — tool-use round trip sketch

```rust
use reqwest::Client;
use serde_json::{json, Value};

async fn tool_round_trip(client: &Client) -> Result<(), Box<dyn std::error::Error>> {
    let tools = json!([{
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    }]);

    // --- Step 1 ---
    let r1 = client.post("http://127.0.0.1:8765/v1/messages")
        .json(&json!({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "tools": tools,
            "messages": [{"role": "user", "content": "Weather in Paris?"}],
        }))
        .send().await?;

    let session_id = r1.headers()
        .get("x-conduit-session-id").ok_or("missing session id")?
        .to_str()?.to_string();
    let msg1: Value = r1.json().await?;
    assert_eq!(msg1["stop_reason"], "tool_use");

    let tool_use = msg1["content"].as_array().unwrap().iter()
        .find(|b| b["type"] == "tool_use").unwrap();
    let tool_use_id = tool_use["id"].as_str().unwrap();
    let city = tool_use["input"]["city"].as_str().unwrap();

    // --- Run your tool ---
    let tool_text = format!("It's 72F sunny in {}", city);

    // --- Step 2: resume ---
    let r2 = client.post("http://127.0.0.1:8765/v1/messages")
        .json(&json!({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 512,
            "session_id": session_id,
            "tools": tools,
            "messages": [
                {"role": "user", "content": "Weather in Paris?"},
                {"role": "assistant", "content": msg1["content"]},
                {"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": tool_text
                }]},
            ]
        }))
        .send().await?;
    let msg2: Value = r2.json().await?;
    println!("{}", msg2["content"][0]["text"]);
    Ok(())
}
```

### 8.6 Setting `effort` (Conduit extension)

Plain httpx — add `effort` to the request body alongside the standard fields:

```python
import httpx

r = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "effort": "high",                              # ← here
    "messages": [{"role": "user", "content": "Find primes of 851."}],
}, timeout=120)
```

Official `anthropic` SDK — `effort` is **not** in the upstream Anthropic API,
so the SDK strips it from top-level kwargs. Pass via `extra_body`:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    extra_body={"effort": "xhigh"},                # ← Conduit-only knob
    messages=[{"role": "user", "content": "..."}],
)
```

Setting effort once and reusing for many turns:

```python
sid = httpx.post("http://127.0.0.1:8765/v1/sessions", json={
    "effort": "high", "model": "claude-sonnet-4-6"
}).json()["session_id"]

# Every turn through `sid` runs at effort=high; the field is ignored if sent again.
httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6", "max_tokens": 512,
    "session_id": sid,
    "messages": [{"role": "user", "content": "..."}],
})
```

### 8.7 Hosted tools — WebSearch / WebFetch (Conduit's hosted-tool pattern)

One request, model orchestrates all search/fetch internally, response is plain text with citations:

```python
import httpx

r = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "tools": [
        {"type": "web_search_20250305", "name": "web_search"},
        {"type": "web_fetch_20250910",  "name": "web_fetch"},
    ],
    "messages": [{"role": "user",
                  "content": "What's the most recent stable FastAPI release? Cite the source."}],
}, timeout=120)
msg = r.json()
print(msg["content"][0]["text"])
# **0.136.1**
# Sources:
# - [Releases · fastapi/fastapi](https://github.com/...)
# - [fastapi · PyPI](https://pypi.org/...)
```

Official Anthropic SDK works too — hosted tools are part of the upstream wire format, so no `extra_body` needed for them:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")
msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    tools=[{"type": "web_search_20250305", "name": "web_search"}],
    messages=[{"role": "user", "content": "Latest stable Python release?"}],
)
print(msg.content[0].text)
```

Mixed hosted + custom — same code as §8.2 but add hosted entries to `tools[]`:

```python
tools = [
    {"type": "web_search_20250305", "name": "web_search"},   # hosted, invisible
    WEATHER_TOOL,                                              # custom, pauses
]
r1 = httpx.post(URL + "/v1/messages", json={..., "tools": tools, ...})
# If stop_reason == "tool_use", the model picked the CUSTOM tool — handle the
# normal pause/resume from §8.2. Hosted tools never produce a visible pause.
```

The Rust pattern is identical to §8.4/§8.5 — just include hosted entries in the `tools` JSON array. No new client-side code paths.

### 8.8 curl

```bash
# Non-streaming
curl -sS -X POST http://127.0.0.1:8765/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "ping"}]
  }'

# Streaming — -N disables curl's buffering so you see tokens as they arrive
curl -N -sS -X POST http://127.0.0.1:8765/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 256,
    "stream": true,
    "messages": [{"role": "user", "content": "Count to 5"}]
  }'

# Tool-use initial — capture session id from response header
curl -i -sS -X POST http://127.0.0.1:8765/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":256,
       "tools":[{"name":"get_weather","description":"...","input_schema":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}],
       "messages":[{"role":"user","content":"Weather in Tokyo?"}]}'
# Look at `x-conduit-session-id: ...` in the response headers, then post a resume.
```

---

## 9. Pitfalls (read before you debug)

1. **Don't forget the `tools` field on resume.** It's required for schema validation, even though the server uses the session's bridge. Send the same list.
2. **`tool_use_id` is case-sensitive and full-length.** Don't truncate `toolu_...` prefixes.
3. **The `assistant` message in the resume's `messages` array must be passed verbatim.** Send the *exact* `content` you received in the initial response (the `tool_use` block). Mutating it (e.g. re-serializing JSON in a way that changes field order or drops fields) is fine for the server because it doesn't inspect that message, but it's required for upstream-Anthropic protocol parity if you ever swap base URLs.
4. **`session_id` on `tool_result` requests is mandatory.** If you forget it, you'll get `400`. If you put the wrong one, you'll get `404`.
5. **`stream=true` on resume works** — the second SSE response contains a fresh `message_start … message_stop` sequence. The session_id header is again on the response.
6. **`api_key="not-used"`**: the official Anthropic SDK rejects empty/None api_keys. Use any non-empty placeholder.
7. **Sessions outlive the process that created them** (until restart or idle timeout). If your client crashes between turns, the session is still parked on the server holding the tool Future. Either resume normally on restart (if you stored the session_id) or DELETE it.
8. **Idle timeout starts at last access**, not at session creation. Each `/v1/messages` request resets it.
9. **Index numbering** in SSE events is per-message-start. Don't carry indices across turns.
10. **Content-Type matters.** Always set `Content-Type: application/json`. The server doesn't accept form-encoded bodies.
11. **`effort` via the official Anthropic SDK requires `extra_body`.** The SDK silently drops unknown top-level kwargs. `client.messages.create(..., effort="high")` will NOT send the field. Use `extra_body={"effort": "high"}`.
12. **`effort` values are case-sensitive lowercase.** `"High"` or `"HIGH"` → 422 validation error.
13. **Setting `effort` on a turn through an existing session does nothing.** Effort is locked at session creation, same rule as `model`. To change effort mid-conversation, create a new session.
14. **Hosted-tool `name` is a pydantic literal.** Must be exactly `"web_search"` (lowercase) for any `web_search_*` type, exactly `"web_fetch"` for any `web_fetch_*` type. Renaming (`"WebSearch"`, `"web_search_sdk"`, etc.) returns 422 — schema validation rejects it before Conduit's hosted-vs-custom splitter ever runs.
15. **Hosted tools emit `server_tool_use`, NOT `tool_use`.** Client-pause-style `tool_use` blocks are exclusive to custom function tools (where you must execute and send `tool_result`). Hosted blocks carry `type: "server_tool_use"` so you can distinguish them at the type level. The paired result block is `type: "web_search_tool_result"` or `"web_fetch_tool_result"`.
16. **`*_tool_result.content` is a string, not a list of blocks.** It includes embedded JSON `Links: [{title, url}]` plus a textual summary. The format is the SDK's; parse markdown citations from the trailing `text` block for stability. See `PASSTHROUGH_INTEGRATION.md` §4.
17. **`content[]` is no longer a single-block array for hosted-tool turns.** If your client asserted `len(content) == 1` or used `content[0]` to mean "the answer", it'll break now — iterate by `type` instead. See `PASSTHROUGH_INTEGRATION.md` §7 for migration notes.
18. **`include_thinking` doesn't change model behaviour, only forwarding.** The model thinks based on `effort` (and SDK defaults). Setting `include_thinking=true` on a simple-prompt low-effort request will yield no thinking blocks — the model just didn't think. See `THINKING_INTEGRATION.md` §1.
19. **Thinking blocks come with a `signature` field.** It's an opaque cryptographic blob the SDK uses internally. Don't parse, reformat, or truncate it. If round-tripping the assistant message elsewhere, pass it verbatim.
20. **Hosted-only requests keep their session.** The header is emitted; the session stays alive (idle-evicted after 30 min by default). Reuse the id for multi-turn hosted-tool chats.
21. **The model decides when to use hosted tools.** Declaring `web_search` is advisory. Sonnet/Haiku tend to over-use it (searching even for evergreen questions). Add a system prompt restriction if cost matters.
22. **Multi-cycle resume reuses the same `session_id`.** Every cycle (initial, resume 1, resume 2, ...) goes to the *same* server session. Don't capture a new id from each response — the server echoes the original one. Each cycle's `tool_use_id` is unique, but `session_id` stays constant. See §5.6.

---

## 10. Debugging recipes

### 10.1 Response hangs forever
- Likely cause: you sent a resume request without the trailing `tool_result` block. Server fell through to "new turn" code path and tried to call `query()` while the SDK was still parked → deadlock.
- Fix: double-check the **last** message has `role: user` and its `content` is a **list** containing a `tool_result` dict.

### 10.2 `{"detail": {"type":"invalid_request_error","message":"unknown tool_use_id: toolu_..."}}`
- Causes:
  - You're using a `tool_use_id` from a different session.
  - You've already delivered this id (each id is single-use). Calling resume with the same id twice is a no-op delivery; the second call falls through to a new turn.
  - The session expired and was recreated.

### 10.3 Stream emits `stop_reason: "end_turn"` when you expected `tool_use`
- The model chose not to use any of your tools. This is expected behavior — `tools` are advisory. Either reprompt to require it, or design your client to handle both outcomes per turn.

### 10.4 First response is fast, second times out
- If you used `httpx`'s default connection pooling and your `tools` declaration is large, check whether the second request actually arrived (use server logs). The wire is HTTP/1.1; no special handling needed.
- More likely: the resume body is malformed. Print what you're sending and validate against the Anthropic schema.

### 10.5 You want to see what the server sees
- Set `CONDUIT_TOOL_DEBUG=1` in the env before starting the server. It will print one line per pump event, plus when handlers are invoked and Futures are resolved. Verbose but precise. Strip in production.

### 10.6 You want to confirm wire-format parity
- Run the official `anthropic` Python SDK against `base_url=http://127.0.0.1:8765`. If it works there, your wire format is right. There is no way to be more wire-compatible than that.

### 10.7 `effort` doesn't seem to be taking effect
- Sending `effort` through the Anthropic SDK without `extra_body`: the SDK drops unknown kwargs silently. Check your code for the literal string `extra_body`.
- Sending `effort` on a request that has `session_id` set: bound at creation, can't change mid-session. Verify by `GET /v1/sessions` and checking which one is being reused, or by creating a fresh session.
- Confirming it's even being applied: run `scripts/effort_test.py` — it measures latency at each level so a meaningful difference proves the knob is wired.

### 10.8 422 Unprocessable Entity when declaring hosted tools
- Most common cause: non-canonical `name` field. Pydantic requires exactly `"web_search"` or `"web_fetch"` (lowercase, no suffix). Using `"WebSearch"` or a custom name like `"web_search_sdk"` fails the literal check.
- The 422 body is large — pydantic tries every member of `ToolUnionParam` and reports one error per failed match. Look for the `WebSearchTool*Param.name` line specifically; it will read `"Input should be 'web_search'"`.
- Fix: `{"type": "web_search_20250305", "name": "web_search"}` exactly.

### 10.9 Hosted tool appears as a `tool_use` block / pauses with `stop_reason: tool_use`
- The request was accepted as a *custom* tool, not hosted. Two common reasons:
  - You included `input_schema` — hosted tools have no input schema. Remove it.
  - Your `type` didn't match a `web_search_*` or `web_fetch_*` prefix (typo? wrong version?). The detector matches by prefix; if it doesn't match, the tool falls through to the custom path and Conduit builds a bridge nobody will satisfy.
- Run `scripts/websearch_test.py` to confirm canonical declarations work on your installation.

### 10.10 Response is slow when you declared web tools
- Each hosted-tool invocation adds real network latency (~3-6s for search, ~2-5s for fetch). Chains can run 10s+. This is the tool calls, not Conduit overhead.
- Use a tighter `system` prompt to discourage unnecessary searches.
- For latency-critical UX, don't declare hosted tools by default — opt in per-request.

### 10.11 Multi-cycle tool use: model keeps emitting `tool_use` and never reaches `end_turn`
- The model is choosing to chain more tool calls. This is intentional behavior — see §5.6. Loop your client until `stop_reason == "end_turn"`, capping `max_cycles` for safety.
- If the model is looping unproductively (calling the same tool repeatedly), tighten the system prompt to say what "done" looks like.
- If a tool returns a permanent error (e.g. "city not found"), encode it as `{"type": "tool_result", "tool_use_id": ..., "is_error": true, "content": "..."}` so the model knows to stop retrying.

### 10.12 `400 tool_result requires session_id` on resume
- Your resume request has a `tool_result` block in the last user message, but the request body has no `session_id`. Capture `x-conduit-session-id` from the initial response's headers and pass it on every resume.
- If you're in a multi-cycle scenario (§5.6), use the same `session_id` for every cycle — don't try to capture a new id from each response.

---

## 11. Versioning expectations

Conduit is pre-1.0 and tracks the Anthropic API loosely:

- New Anthropic content-block types: forwarded if they pass through the SDK's `StreamEvent.event` dict.
- Renamed/restructured Anthropic fields: server-side schema (re-exported from the `anthropic` Python package) updates with that package's version.
- Future work: exhaustive testing of parallel `tool_use` blocks in one message, `tool_choice` enforcement, `CONDUIT_TOOL_RESULT_TIMEOUT_S` enforcement, hosted-tool `allowed_domains`/`blocked_domains` propagation, persistent (Redis/SQLite) session store.

---

## 12. Minimum viable integration checklist

For an agent integrating this for the first time:

- [ ] Confirm `GET /health` returns 200.
- [ ] Confirm `POST /v1/messages` with `{"model":..., "max_tokens":16, "messages":[{"role":"user","content":"ping"}]}` returns a valid Message with `stop_reason: "end_turn"`.
- [ ] If using `effort`: pass it in the request body (or `extra_body={"effort": "..."}` via the Anthropic SDK); send a bad value (`"extreme"`) and confirm 422; consider running `scripts/effort_test.py`'s latency sweep to confirm it's wired through.
- [ ] If using custom tools: declare them, send initial request, capture `x-conduit-session-id` header and the `tool_use` block's `id`.
- [ ] Execute the tool client-side.
- [ ] Send resume request with `session_id`, `tools` (same list), and a trailing user message whose content is `[{"type":"tool_result","tool_use_id":<id>,"content":<your result>}]`.
- [ ] **For multi-cycle tools (§5.6):** loop while `stop_reason == "tool_use"`, reusing the same `session_id` and appending `assistant{tool_use}` + `user{tool_result}` pairs to `messages[]` each cycle. Cap `max_cycles` for safety.
- [ ] Receive the final answer with `stop_reason: "end_turn"`.
- [ ] Optionally `DELETE /v1/sessions/<id>`.
- [ ] If using hosted tools (`web_search` / `web_fetch`): declare them with the canonical `{"type": "web_search_20250305", "name": "web_search"}` shape (the `name` field is a pydantic literal — `"WebSearch"` etc. will 422); send a single request asking a current-events question; expect `stop_reason: "end_turn"`, `server_tool_use` + `web_search_tool_result` + `text` blocks in `content[]`, **no** client-pause-style `tool_use` blocks.
- [ ] Consider running `scripts/websearch_test.py` and `scripts/multicycle_demo.py` to verify both flows end-to-end on your installation.
