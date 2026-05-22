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
| Conduit-specific extension | `session_id` field in request body + `x-conduit-session-id` response header |
| Drop-in OK | Yes — pointing the official `anthropic` SDK at the base URL works for chat *and* streaming |
| Tool support | **Phase 2**: single-tool, single-call per turn. Parallel/sequential calls not yet supported. |

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
| `tools` | `ToolParam[]` | ❌ | Triggers tool-use mode (see §5). |
| `tool_choice` | object | ❌ | Accepted but **currently ignored** in Phase 2. |
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
{"system_prompt": "be brief", "model": "claude-haiku-4-5-20251001"}
```
Both fields optional. Empty body `{}` is valid.

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

### 3.2 `ToolParam` (entries in `tools`)

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

Names are **bare** on both directions of the wire. The server's internal MCP prefix (`mcp__conduit__X`) is invisible to clients.

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

Two paths:
1. Auto-allocated: omit `session_id`, response header carries the new id. Server keeps it.
2. Explicit: `POST /v1/sessions` first, then send `session_id` on every `/v1/messages`.

When `session_id` is present, the server keeps history. You **only** need to send the new user turn in `messages`, not the whole history. (You can still send the whole history — the server only reads the most recent user message in stateful mode.)

**Model binding:** the model is fixed at session creation. The `model` field in subsequent requests through that session is **ignored at the SDK layer** but echoed in the response. If you need a different model, create a new session.

### 4.3 Tool-use

See §5.

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
  - **Reuse it for another turn** — see §6.4 limitation note before relying on this.

### 5.5 Errors specific to resume

| Status | When |
|---|---|
| 400 | `tool_result` block missing `tool_use_id` |
| 400 | `tool_use_id` doesn't match a parked Future on this session |
| 400 | `tool_result` present but no `session_id` in body |
| 400 | `session_id` is for a non-tool session |
| 404 | `session_id` not found (likely timed out) — start over |

---

## 6. Limitations (Phase 2)

The protocol is the Anthropic protocol — these are implementation limits, not wire incompatibilities. The client doesn't need workarounds, just awareness.

1. **Single tool call per turn.** If the model tries to emit two `tool_use` blocks in one turn (parallel tool calling), behavior is undefined in Phase 2. Constrain your tool list / prompting so the model picks one at a time.
2. **Single round per session.** Phase 2 is tested with one pause/resume cycle then `end_turn`. Multi-turn conversations on the same tool session (user → tool_use → result → text → user → ... again) are not yet exercised.
3. **`tool_choice` ignored.** You can send it; it doesn't constrain the model.
4. **Per-session model lock** — see §4.2.
5. **Session timeout** is 30 min default. If your tool execution takes longer than that, the resume request will 404. Move long-running work to async background and respond fast to Conduit.
6. **Thinking blocks filtered.** If you specifically need extended-thinking output, it's stripped from the stream.

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

### 8.6 curl

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

---

## 11. Versioning expectations

Conduit is pre-1.0 and tracks the Anthropic API loosely:

- New Anthropic content-block types: forwarded if they pass through the SDK's `StreamEvent.event` dict.
- Renamed/restructured Anthropic fields: server-side schema (re-exported from the `anthropic` Python package) updates with that package's version.
- Phase 3+ will add: parallel tool calls, multi-turn tool sessions, `tool_choice` enforcement, `CONDUIT_TOOL_RESULT_TIMEOUT_S` enforcement, persistent (Redis/SQLite) session store.

---

## 12. Minimum viable integration checklist

For an agent integrating this for the first time:

- [ ] Confirm `GET /health` returns 200.
- [ ] Confirm `POST /v1/messages` with `{"model":..., "max_tokens":16, "messages":[{"role":"user","content":"ping"}]}` returns a valid Message with `stop_reason: "end_turn"`.
- [ ] If using tools: declare them, send initial request, capture `x-conduit-session-id` header and the `tool_use` block's `id`.
- [ ] Execute the tool client-side.
- [ ] Send resume request with `session_id`, `tools` (same list), and a trailing user message whose content is `[{"type":"tool_result","tool_use_id":<id>,"content":<your result>}]`.
- [ ] Receive the final answer with `stop_reason: "end_turn"`.
- [ ] Optionally `DELETE /v1/sessions/<id>`.
