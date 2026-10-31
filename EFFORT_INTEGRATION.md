# EFFORT_INTEGRATION

Client-side integration guide for the `effort` field — Conduit's exposure of
Claude's thinking-budget enum. Self-contained.

---

## 0. TL;DR

```json
POST /v1/messages
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 1024,
  "effort": "high",                  ← here
  "messages": [{"role": "user", "content": "..."}]
}
```

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `effort` | `"low" \| "medium" \| "high" \| "xhigh" \| "max"` | ❌ | server's `CONDUIT_DEFAULT_EFFORT`, or `null` if unset (SDK default) | **Conduit extension** — not in the upstream Anthropic API. **Bound at session creation**, same as `model`. |

> **Canonical spelling also accepted:** `output_config: {"effort": "high"}`, the upstream Anthropic location. When both are present, `output_config.effort` wins. With the official SDKs this is a first-class keyword argument (`output_config={"effort": "high"}`), so `extra_body` is no longer required. Everything below applies to both spellings.

---

## 1. What it controls

`effort` maps directly to the Claude Agent SDK's `effort` parameter, which is the same knob exposed by the Claude API as effort levels. It governs how many tokens Claude can spend on *internal reasoning* before producing its visible response.

- **`low`** — fast, cheap, suitable for routine generation/transformation.
- **`medium`** — balanced default for most chat work.
- **`high`** — meaningful reasoning headroom; good for analysis, debugging, multi-step planning.
- **`xhigh`** — heavy reasoning; complex math, long causal chains, careful code review.
- **`max`** — maximum budget Claude can use; long-running deep work. Slow and expensive.

These are **not** linear — each step is meaningfully larger than the last. Don't reach for `xhigh`/`max` reflexively.

You don't see the thinking output. Conduit strips thinking blocks from the SSE stream (chat-style). What you observe is just the final assistant text/tool_use blocks.

---

## 2. Binding rules

`effort` is bound when the **session is created**, alongside `model`. Subsequent turns through that session inherit it.

| Mode | Where `effort` is sourced |
|---|---|
| Stateless (`session_id` omitted, no `tools`) | Request body's `effort` → ephemeral session created at that effort → torn down after the request. |
| Auto-allocated tool-use (`tools` present, no `session_id`) | Request body's `effort` → session created at that effort. Subsequent resume requests through the returned `x-conduit-session-id` reuse the same effort. |
| Stateful chat (`POST /v1/sessions` then `session_id` on requests) | `POST /v1/sessions` body's `effort` field at creation time. Per-request `effort` is **silently ignored** afterwards. |

If you want a different effort, you need a different session. There is no way to change effort mid-conversation.

---

## 3. Default resolution

1. Request body's `effort` field, if present.
2. Otherwise, the server's `CONDUIT_DEFAULT_EFFORT` env var.
3. Otherwise, `null` — the Agent SDK applies its own default (currently `medium`, but treat as unspecified).

Server admins set the env var; clients override per-request.

---

## 4. Validation

Sending an unrecognised string returns **422 Unprocessable Entity** with a pydantic validation error:

```json
{
  "detail": [{
    "type": "literal_error",
    "loc": ["body", "effort"],
    "msg": "Input should be 'low', 'medium', 'high', 'xhigh' or 'max'",
    "input": "extreme"
  }]
}
```

Don't pass casing variations (`HIGH`, `Low`) — values are case-sensitive.

---

## 5. Interaction with other fields

| Field | Interaction |
|---|---|
| `model` | Independent. Both bound at session creation. You can mix any effort with any model. |
| `max_tokens` | Independent. `max_tokens` caps the *visible output*; `effort` caps the *invisible reasoning*. They are separate budgets. |
| `temperature`, `stop_sequences` | Independent. |
| `tools` | Independent. Tool-use auto-allocates a session whose effort comes from the initial request's `effort` field. |
| `tool_choice` | Currently ignored (Phase 2). |
| `stream` | Independent. Streaming responses include the same thinking budget; you just don't see thinking tokens. |
| `session_id` | If present, `effort` from the request body is **ignored**. The session keeps its original effort. |

---

## 6. Cost / latency expectations

Order-of-magnitude only — actual numbers depend on prompt length and the model.

| effort | typical extra latency vs `low` | typical extra cost vs `low` |
|---|---|---|
| `low` | baseline | baseline |
| `medium` | ~1.5× | ~1.5× |
| `high` | ~3× | ~3× |
| `xhigh` | ~6× | ~6× |
| `max` | up to ~15× | up to ~15× |

This matters for Conduit because it consumes your Claude Max subscription quota faster at higher levels. After 2026-06-15 the Agent SDK has its own credit pool — be aware of where your budget goes.

---

## 7. Code recipes

### 7.1 Plain `httpx` (Python)

```python
import httpx

r = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "effort": "high",
    "messages": [{"role": "user", "content": "Find the prime factorization of 851."}],
}, timeout=60)
r.raise_for_status()
print(r.json()["content"][0]["text"])
```

### 7.2 Official `anthropic` Python SDK

Two spellings. The **canonical** one is `output_config`, which the SDK exposes as a keyword argument:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    output_config={"effort": "high"},   # ← standard Anthropic field
    messages=[{"role": "user", "content": "Find the prime factorization of 851."}],
)
print(msg.content[0].text)
```

The Conduit top-level `effort` alias is unknown to the SDK, so if you use that spelling pass it through `extra_body`:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    extra_body={"effort": "high"},      # ← Conduit extension
    messages=[{"role": "user", "content": "Find the prime factorization of 851."}],
)
print(msg.content[0].text)
```

Same pattern for `client.messages.stream(...)`.

### 7.3 Explicit session at a fixed effort (Python)

When you want a stateful chat that holds a specific effort across many turns:

```python
import httpx

URL = "http://127.0.0.1:8765"

sid = httpx.post(URL + "/v1/sessions", json={
    "effort": "xhigh",
    "model": "claude-sonnet-4-6",
}, timeout=10).json()["session_id"]

# Every turn through this session uses xhigh; per-request `effort` is ignored.
def turn(text: str) -> str:
    r = httpx.post(URL + "/v1/messages", json={
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "session_id": sid,
        "messages": [{"role": "user", "content": text}],
    }, timeout=120)
    return r.json()["content"][0]["text"]

print(turn("Step 1: outline the proof."))
print(turn("Step 2: fill in the algebra."))

httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)
```

### 7.4 With tools (Python)

`effort` is read on the **initial** tool-use request. The auto-allocated session keeps that effort through pause/resume.

```python
r1 = httpx.post(URL + "/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "effort": "high",                # ← bound on initial request
    "tools": [WEATHER_TOOL],
    "messages": [{"role": "user", "content": "..."}],
}, timeout=60)
sid = r1.headers["x-conduit-session-id"]

# Resume — `effort` here is ignored; session already runs at "high".
r2 = httpx.post(URL + "/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "session_id": sid,
    "tools": [WEATHER_TOOL],
    "messages": [...with tool_result...],
}, timeout=60)
```

### 7.5 Rust (`reqwest`)

```rust
use serde_json::json;

let body = json!({
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "effort": "high",
    "messages": [{"role": "user", "content": "Find primes of 851"}],
});

let resp: serde_json::Value = reqwest::Client::new()
    .post("http://127.0.0.1:8765/v1/messages")
    .json(&body)
    .send().await?
    .json().await?;

println!("{}", resp["content"][0]["text"].as_str().unwrap_or(""));
```

### 7.6 curl

```bash
curl -sS -X POST http://127.0.0.1:8765/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "effort": "high",
    "messages": [{"role": "user", "content": "Outline a proof of the four-colour theorem."}]
  }'
```

---

## 8. Pitfalls

1. **Sending `effort` on a resume / stateful turn does nothing.** Effort is locked at session creation. If you need a different level, create a new session.
2. **Don't reach for `max` reflexively.** Latency can be 10× and quota burn is real, especially after 2026-06-15 when Agent SDK usage draws from its own credit pool.
3. **Casing matters.** Only lowercase exact values work. `"High"` fails validation.
4. **Anthropic SDK clients: prefer `output_config={"effort": ...}`.** It is a real keyword argument. The top-level `effort` alias is unknown to the SDK and must go in `extra_body={"effort": "..."}`.
5. **No effort comes back in the response.** You can record what you sent but the response shape is identical to a no-`effort` response.
6. **`effort` is not part of the official Anthropic API.** Any code you write depending on it is Conduit-specific. If you ever switch `base_url` to `api.anthropic.com`, drop the `effort` field (and use Anthropic's native `thinking: {type: "enabled", budget_tokens: N}` if you need similar control).

---

## 9. When to use which level — heuristics

| Task | Recommended |
|---|---|
| One-line rewrites, format conversion, simple Q&A | `low` |
| Most chat, code generation, drafting | `medium` (or unset) |
| Debugging a bug across multiple files, design discussion | `high` |
| Hard math, multi-step planning, security review | `xhigh` |
| Research-level reasoning, long causal chains, the "I want it perfect" case | `max` |

If unsure: start at `medium`, only step up when the answer is visibly under-baked.

---

## 10. Minimum-viable integration checklist

- [ ] Confirm `POST /v1/messages` accepts `effort: "high"` and the response looks normal (text in `content[0].text`, `stop_reason: "end_turn"`).
- [ ] Try `effort: "extreme"` and confirm you get a 422 with a clear error message — proves validation is in effect.
- [ ] If using `extra_body` via the Anthropic SDK, confirm a response comes back (no client-side error for the unknown field).
- [ ] If using a stateful session: verify that creating the session with `effort: "high"` then sending turns without `effort` still produces high-effort behavior (slower latency vs a `low` session as a sanity check).
