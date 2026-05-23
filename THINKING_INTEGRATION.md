# THINKING_INTEGRATION

Client-side integration guide for Conduit's `include_thinking` field — exposing the model's extended-thinking content blocks in the response.

Self-contained.

---

## 0. TL;DR

```json
POST /v1/messages
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 1024,
  "include_thinking": true,          ← here
  "messages": [{"role": "user", "content": "Hard math question"}]
}
```

Response gains `thinking` blocks before the final `text`:

```json
{"content": [
   {"type": "thinking",
    "thinking": "Let me work through this. First...",
    "signature": "Ev4CCm0IDR..."},
   {"type": "text", "text": "The answer is 42."}
 ],
 "stop_reason": "end_turn"}
```

| Fact | Value |
|---|---|
| Field name | `include_thinking` (request body) |
| Type | `bool` |
| Default | `false` (thinking stripped, current behaviour) |
| Wire format when true | Anthropic-canonical `thinking` content blocks |
| Bound at | Session creation (same rule as `model`, `effort`) |
| Affects model behaviour? | **No.** Only controls forwarding. Use `effort` to control how much the model thinks. |

---

## 1. What it does (and doesn't do)

**Does:** When set to `true`, Conduit stops filtering `thinking` content blocks from the response stream. They appear in `content[]` (non-streaming) or as SSE events (streaming).

**Does not:**
- Cause the model to think more. The model's thinking budget is governed by `effort` and the SDK's `thinking` config — those are separate knobs. `include_thinking` only controls what Conduit forwards.
- Change `stop_reason`. Thinking blocks come *before* the final text/tool_use blocks in `content[]`; the turn still ends with the same `stop_reason`.
- Add cost. Whatever thinking the model did, it did regardless of this flag. Setting `include_thinking=true` is free — you just get to see what was already happening.

**Key consequence:** even with `include_thinking=false`, the model is still thinking and that thinking is still consuming subscription tokens. The only difference is whether Conduit *shows* them to you.

---

## 2. Response shape

### 2.1 The `thinking` block

```json
{
  "type": "thinking",
  "thinking": "<the model's chain-of-thought, possibly long>",
  "signature": "<base64-ish cryptographic signature, ~500-1000 chars>"
}
```

- `thinking` — the reasoning text. Can be multi-paragraph. Format is free-form natural language.
- `signature` — a signature Anthropic uses for tool-use validation. **Treat as opaque.** Don't parse, don't truncate. If you round-trip the assistant message back to a server (for multi-turn), pass it verbatim.

### 2.2 Ordering in `content[]`

`thinking` always appears **before** `text` in the same message. For tool-use turns, thinking precedes the `tool_use` / `server_tool_use` block too:

```
[0] thinking          ← reasoning
[1] tool_use OR server_tool_use OR text
[2] (more, depending on the turn)
```

Multiple thinking blocks in one message are possible (the model may pause to think mid-answer). Iterate by `type`, not by index.

### 2.3 Mixed with hosted tools

When `include_thinking=true` AND web tools fire, you get the full chain:

```json
"content": [
  {"type": "thinking", "thinking": "I should search...", "signature": "..."},
  {"type": "server_tool_use", "name": "WebSearch", "input": {"query": "..."}},
  {"type": "web_search_tool_result", "tool_use_id": "...", "content": "..."},
  {"type": "thinking", "thinking": "Now I have the results, the answer is...",
   "signature": "..."},
  {"type": "text", "text": "The answer is 42 (source: ...)."}
]
```

Multiple thinking blocks bracket the tool round-trip — one before the call, one after the result. This is the Anthropic-canonical pattern.

---

## 3. Streaming

The SSE event sequence when `include_thinking=true` and the model thinks before a text answer:

```
event: message_start
event: content_block_start    (type=thinking, thinking="", signature="")
event: content_block_delta    (thinking_delta, "Let me work")
event: content_block_delta    (thinking_delta, " through this")
event: content_block_delta    (thinking_delta, ".")
event: content_block_delta    (signature_delta, "Ev4CCm0IDR...")
event: content_block_stop
event: content_block_start    (type=text, text="")
event: content_block_delta    (text_delta, "The answer is")
event: content_block_delta    (text_delta, " 42.")
event: content_block_stop
event: message_delta          (stop_reason=end_turn)
event: message_stop
```

Two delta types you'll see only in thinking blocks:

- **`thinking_delta`** — `delta.thinking` is a chunk of the thinking text. Accumulate across deltas to get the full thinking string.
- **`signature_delta`** — `delta.signature` is the signature value. **It arrives once at the end of the thinking block** in current SDK versions. To be safe, accumulate (append) like text — works whether the SDK chunks it or sends it whole.

`content_block_stop` closes the thinking block. The next `content_block_start` begins the next block (often `text` or `tool_use`).

---

## 4. Code recipes

### 4.1 Python — httpx, non-streaming

```python
import httpx

r = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "include_thinking": True,
    "messages": [{"role": "user", "content": "Find the prime factorization of 851."}],
}, timeout=120)
msg = r.json()

for b in msg["content"]:
    if b["type"] == "thinking":
        print(f"💭 {b['thinking'][:200]}...")
    elif b["type"] == "text":
        print(f"💬 {b['text']}")
```

### 4.2 Python — official Anthropic SDK

`include_thinking` is a Conduit extension (not in Anthropic's upstream API), so pass it via `extra_body`:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")
msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    extra_body={"include_thinking": True},
    messages=[{"role": "user", "content": "..."}],
)

for b in msg.content:
    # Anthropic SDK exposes thinking blocks as ThinkingBlock dataclass
    if b.type == "thinking":
        print(b.thinking)
    elif b.type == "text":
        print(b.text)
```

### 4.3 Python — streaming with thinking reassembly

```python
import httpx, json

thinking_buf = []
signature_buf = []
text_buf = []
current_block_type = None

with httpx.stream("POST", "http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "stream": True,
    "include_thinking": True,
    "messages": [{"role": "user", "content": "Hard reasoning task..."}],
}, timeout=120) as resp:
    event_name = None
    for line in resp.iter_lines():
        if not line:
            event_name = None; continue
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: "):
            d = json.loads(line[6:])
            t = d.get("type")
            if t == "content_block_start":
                current_block_type = d["content_block"]["type"]
            elif t == "content_block_delta":
                delta = d["delta"]
                if delta["type"] == "thinking_delta":
                    thinking_buf.append(delta.get("thinking", ""))
                elif delta["type"] == "signature_delta":
                    signature_buf.append(delta.get("signature", ""))
                elif delta["type"] == "text_delta":
                    text_buf.append(delta["text"])

print("Thinking:", "".join(thinking_buf))
print("Signature:", "".join(signature_buf))
print("Answer:", "".join(text_buf))
```

### 4.4 Stateful: lock thinking on for the session

```python
import httpx

sid = httpx.post("http://127.0.0.1:8765/v1/sessions", json={
    "model": "claude-sonnet-4-6",
    "effort": "high",
    "include_thinking": True,
}).json()["session_id"]

# Every turn through `sid` will return thinking blocks; per-turn
# include_thinking on subsequent /v1/messages is ignored.
```

### 4.5 Rust — `reqwest`

```rust
let body = serde_json::json!({
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "include_thinking": true,
    "messages": [{"role": "user", "content": "Hard problem"}]
});

let msg: serde_json::Value = reqwest::Client::new()
    .post("http://127.0.0.1:8765/v1/messages")
    .json(&body)
    .send().await?
    .json().await?;

for b in msg["content"].as_array().unwrap() {
    match b["type"].as_str().unwrap_or("") {
        "thinking" => println!("💭 {}", b["thinking"].as_str().unwrap_or("")),
        "text"     => println!("💬 {}", b["text"].as_str().unwrap_or("")),
        _          => {}
    }
}
```

---

## 5. Combining with effort

Two related knobs, different purposes:

| Knob | Type | What it does |
|---|---|---|
| `effort` | enum | Controls **how much** the model thinks (token budget) |
| `include_thinking` | bool | Controls whether you **see** that thinking |

To get the most reasoning visible, combine both:

```json
{
  "effort": "high",
  "include_thinking": true,
  ...
}
```

To get heavy reasoning but keep responses small (e.g. for UI that just shows the answer):

```json
{
  "effort": "high",
  "include_thinking": false,
  ...
}
```

Both are bound at session creation. Match them at the session level.

---

## 6. Pitfalls

1. **`include_thinking` doesn't enable thinking — `effort` does.** If you set `include_thinking=true` but the model doesn't think (low effort, simple prompt), you'll get zero thinking blocks. The flag is purely a forwarding switch.

2. **Anthropic SDK requires `extra_body`.** It's a Conduit extension; the SDK strips unknown top-level kwargs. Use `extra_body={"include_thinking": True}`.

3. **Bound at session creation.** Sending `include_thinking=true` on a turn through an existing session that was created with `include_thinking=false` does nothing. Create a new session with the right setting.

4. **Thinking content can be long.** Multi-paragraph reasoning is common. If you're rendering this in a UI, plan for it (collapsible "Reasoning" section, truncation, scroll). Token-wise: thinking is part of `usage.output_tokens` — you'll see higher numbers when thinking is on, even though the flag doesn't change generation.

5. **Don't trust thinking text as a public-facing artifact.** It's the model's draft work; format and style vary. The `text` blocks are the model's polished answer. For UX: show thinking in a separate area or behind a disclosure widget, not inline.

6. **`signature` is opaque.** It's a base64-ish cryptographic blob the SDK uses. Don't parse it, don't reformat, don't truncate. If you ever round-trip the assistant message to another Anthropic-compatible server, the signature should be passed unchanged.

7. **Streaming thinking can pause mid-block.** The SDK may emit a long string of `thinking_delta` events without any other event types between them. Your SSE parser shouldn't time out during long thinking generations — use generous read timeouts (60s+).

---

## 7. Debugging

### 7.1 I set `include_thinking=true` but get no thinking blocks
- Most likely: the model didn't think on this prompt. Try a harder question (math, multi-step reasoning) or raise `effort` to `high`/`xhigh`.
- Check: `GET /v1/sessions` to see if you're reusing a session created with `include_thinking=false` — bound at session creation.

### 7.2 `include_thinking` not making it through the Anthropic SDK
- Use `extra_body={"include_thinking": True}` — top-level kwargs unknown to Anthropic's API get dropped by the SDK.

### 7.3 Thinking text looks malformed / truncated
- Check streaming reassembly: `thinking_delta.thinking` is accumulated across many events (chunk-by-chunk). If you only take the last delta, you miss most of it. Use `+= delta.get("thinking","")`.

### 7.4 422 on `include_thinking`
- Wrong type. It must be a bool (`true` / `false`). String `"true"` will 422.

---

## 8. Phase / version notes

- Phase 2.5 ships `include_thinking` as a Conduit extension.
- Future: Anthropic's actual `thinking: {type: "enabled", budget_tokens: N}` config could be accepted instead, mapping `budget_tokens` to the SDK's `max_thinking_tokens`. Not yet implemented.
- `signature` field is forwarded verbatim from the SDK; Conduit doesn't regenerate or validate.

---

## 9. Minimum viable integration checklist

- [ ] Confirm a request without `include_thinking` returns only `text` blocks (no `thinking`)
- [ ] Confirm `"include_thinking": true` returns at least one `thinking` block with non-empty `thinking` + `signature` fields
- [ ] If using the Anthropic SDK: confirm you used `extra_body={"include_thinking": True}` (not a top-level kwarg)
- [ ] If streaming: confirm you accumulate `thinking_delta.thinking` across deltas and treat `signature_delta` as accumulate-or-replace safely
- [ ] If displaying to users: show thinking in a separate UI affordance from the final text (disclosure / "Reasoning" panel)

---

## 10. References

- `AGENTIC_INTEGRATION.md` §4.4 — general Conduit extensions overview
- `EFFORT_INTEGRATION.md` — companion knob that controls *how much* thinking happens
- `PASSTHROUGH_INTEGRATION.md` — same pattern applied to hosted-tool blocks
- `tests/test_thinking.py` — runnable behavioural tests
