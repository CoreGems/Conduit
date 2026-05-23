# WEBSEARCH_INTEGRATION

Client-side integration guide for Conduit's hosted SDK tools — **WebSearch**
and **WebFetch**. Self-contained.

---

## 0. TL;DR

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

Response is plain text with citations inline. **No `tool_use` blocks**, **no `stop_reason: tool_use`**, **no client-side execution**. The Agent SDK runs both tools internally.

| Fact | Value |
|---|---|
| Tools | `WebSearch`, `WebFetch` |
| Pattern | Anthropic-style **hosted tool** — declare in `tools[]`, server runs it |
| Client work | Just declare them. No bridge handlers, no `tool_result` returns. |
| `stop_reason` you'll see | `end_turn` (the hidden pause is fully suppressed) |
| Citations | Inline in the model's text as markdown links |
| Session behavior | Auto-allocated and torn down after the response (no resume needed) |
| Restriction | Bound at session creation, same rule as `model` and `effort` |

---

## 1. The hosted-tool pattern

Anthropic's API distinguishes two kinds of tools:

- **Custom function tools** — client implements them. Model emits `tool_use`, response ends, client executes, client sends `tool_result` in a new request. This is what `tool_demo.py` exercises.
- **Hosted server tools** — server (Anthropic, or in our case Conduit via the Agent SDK) implements them. Client just declares them and gets back text. There is no pause from the client's perspective.

Conduit's WebSearch/WebFetch follow the second pattern exactly. The Agent SDK pauses internally to fetch, then continues, all in one HTTP response. Conduit strips the internal pause markers so the client sees a single continuous turn.

What gets filtered before reaching the client:

- The `tool_use` content block with `name: "WebSearch"` or `"WebFetch"`
- Its `input_json_delta` events and its `content_block_stop`
- The `message_delta` with `stop_reason: "tool_use"` that ended the SDK's hidden turn 1
- The paired `message_stop`
- The `message_start` of the SDK's hidden turn 2

What you see: a normal Anthropic message with `content: [{type: "text", text: "..."}]` and `stop_reason: "end_turn"`. Citations are inlined as markdown links the model produces while writing its answer.

---

## 2. Declarations

### 2.1 WebSearch — accepted shape

```json
{"type": "web_search_20250305", "name": "web_search"}
{"type": "web_search_20260209", "name": "web_search"}
```

**Both fields are required and the `name` must be the literal string `"web_search"`** (lowercase, exact). Pydantic's `ToolUnionParam` validates this strictly — any other value (including `"WebSearch"` or a custom name like `"web_search_sdk"`) produces a 422 before Conduit's own detection runs.

### 2.2 WebFetch — accepted shape

```json
{"type": "web_fetch_20250910", "name": "web_fetch"}
{"type": "web_fetch_20260209", "name": "web_fetch"}
{"type": "web_fetch_20260309", "name": "web_fetch"}
```

Same rule: `name` must be the literal `"web_fetch"`.

### 2.3 Name collision with a custom function tool

Because pydantic pins the hosted-tool names to `"web_search"` / `"web_fetch"`, you cannot have a custom function tool with one of those exact names in the same request — they'll collide on declaration. Two practical patterns if your client also has a function named `web_search`:

1. **Dedupe at the client.** Before injecting the hosted declaration, check if the request already declares a tool with that name; skip the hosted injection if so (one or the other wins for that turn, not both).

   ```rust
   // Skip hosted web_search if the request already has a tool with that name
   let existing: HashSet<String> = final_tools.iter()
       .filter_map(|t| t.get("name").and_then(|v| v.as_str()).map(String::from))
       .collect();
   if !existing.contains("web_search") {
       final_tools.push(json!({"type": "web_search_20250305", "name": "web_search"}));
   }
   ```

2. **Rename your custom tool.** Use `do_web_search` or `search_internal` for your function tool, freeing up `web_search` for the hosted version.

Both work. The dedupe approach is the only choice if you can't rename the function tool because external code depends on it.

### 2.3 Mixing with custom tools

```json
"tools": [
  {"type": "web_search_20250305", "name": "web_search"},
  {"name": "get_weather", "input_schema": {...}, "description": "..."}
]
```

Conduit splits the array internally: hosted entries go straight to the SDK; custom entries get a per-session bridge. If the model invokes a hosted tool, it's invisible. If it invokes the custom one, you get the normal `stop_reason: tool_use` pause and follow the resume protocol from `AGENTIC_INTEGRATION.md` §5.

---

## 3. What you get back

Hosted-tool responses are Anthropic-canonical: each tool invocation appears as a `server_tool_use` block with the call inputs, paired with a `*_tool_result` block carrying what came back, followed by the model's final text. All in one response with `stop_reason: "end_turn"` — no client pause.

### 3.1 Non-streaming

```json
{
  "id": "msg_01...",
  "type": "message",
  "role": "assistant",
  "model": "claude-haiku-4-5-20251001",
  "content": [
    {"type": "server_tool_use",
     "id": "toolu_01Y6...",
     "name": "WebSearch",
     "input": {"query": "latest stable Python release 2026"}},

    {"type": "web_search_tool_result",
     "tool_use_id": "toolu_01Y6...",
     "content": "Web search results for query: \"latest stable Python release 2026\"\n\nLinks: [{\"title\":\"Status of Python versions\",\"url\":\"https://devguide.python.org/versions/\"}, ...]\n\nBased on the web search results, the latest stable version is Python 3.14.5..."},

    {"type": "text",
     "text": "Python 3.14.5 is the most recent stable release, having been released on May 10, 2026 ([source](https://www.python.org/downloads/))."}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {"input_tokens": 12, "output_tokens": 87}
}
```

The block ordering is: **`server_tool_use` → corresponding `*_tool_result` → next call or final text**. Multiple search→fetch chains produce multiple alternating pairs.

### 3.2 Block-type reference

| `type` | Fields | Purpose |
|---|---|---|
| `server_tool_use` | `id`, `name` (`"WebSearch"` or `"WebFetch"`), `input` (dict — `{query}` or `{url}`) | Records what the model asked the SDK to run |
| `web_search_tool_result` | `tool_use_id`, `content` (str) | The search-result string the SDK fed back to the model. Includes embedded JSON `Links: [{title,url},...]` followed by a summary |
| `web_fetch_tool_result` | `tool_use_id`, `content` (str) | The fetched page content the SDK fed back |
| `text` | `text` (str) | The model's synthesized answer. Often contains markdown citation links |

`tool_use_id` correlates a call to its result. The SDK reuses the same id (`toolu_...`) on both blocks; clients can pair them by exact-string match.

### 3.3 Extracting structured citations

The `web_search_tool_result.content` is a string that starts with `Web search results for query: "..."` followed by `Links: <JSON-array of {title,url}>` and then a textual summary. To extract URLs/titles cleanly:

```python
import re, json

def parse_search_result(content: str) -> list[dict]:
    m = re.search(r"Links: (\[.*?\])\n", content, re.DOTALL)
    return json.loads(m.group(1)) if m else []
```

This format is the SDK's, not Conduit's — it may change with SDK versions. If you need stability, also look at the inline markdown citations the model writes in the trailing `text` block; those tend to be format-stable across SDK versions.

### 3.4 Streaming

The SSE sequence:

```
message_start
  content_block_start (type=server_tool_use, with id/name/empty input)
    content_block_delta (input_json_delta, partial query)
    content_block_delta (input_json_delta, more partial query)
  content_block_stop
  content_block_start (type=web_search_tool_result, content already populated)
  content_block_stop
  content_block_start (type=text, text="")
    content_block_delta (text_delta) × N
  content_block_stop
message_delta (stop_reason=end_turn)
message_stop
```

The SDK's hidden inter-turn boundary is suppressed (no internal `message_delta(stop_reason: tool_use)` reaches the client). Multiple search→fetch rounds produce multiple `server_tool_use`+`*_tool_result` pairs interleaved before the final text. Index numbering stays continuous across them.

**Important:** `web_search_tool_result` and `web_fetch_tool_result` blocks arrive with `content` already populated at `content_block_start` time — no `content_block_delta` reassembly needed. (`server_tool_use.input` does stream via `input_json_delta` like a regular tool_use, so reassemble it normally.)

### 3.5 Response headers

`x-conduit-session-id` is still surfaced on hosted-only responses, but you don't need it for anything — the session is torn down after the response. Ignore it unless you also have custom tools that pause.

---

## 4. WebSearch vs WebFetch

| | WebSearch | WebFetch |
|---|---|---|
| Input | A search query string the model writes | A specific URL the model knows |
| Output to model | A list of result snippets + URLs (SERP-like) | The full page text at that URL |
| When the model picks it | Vague factual lookups, recent events, "find me X" | Reading a specific page the model already has the URL for |
| Typical chain | First WebSearch → results → WebFetch on the best URL | Direct, when you provide the URL in the prompt |
| JS rendering | n/a | **No** — server-side fetch only, SPAs may come back thin |
| Paywalls / login walls | n/a | Returns whatever the unauthenticated GET sees |

Declare both if you don't want to constrain the model's strategy.

---

## 5. Code recipes

### 5.1 Python — plain `httpx`

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
                  "content": "What's the most recent stable Python release?"}],
}, timeout=120)
msg = r.json()
print(msg["content"][0]["text"])
```

### 5.2 Python — official `anthropic` SDK

`tools` is a top-level kwarg in the SDK, so you can pass hosted declarations
directly — no `extra_body` needed for this one. The SDK accepts the full
`ToolUnionParam` shape.

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    tools=[{"type": "web_search_20250305", "name": "web_search"}],
    messages=[{"role": "user", "content": "What's the latest stable FastAPI?"}],
)
print(msg.content[0].text)
```

Streaming works identically — `client.messages.stream(..., tools=[...])`.

### 5.3 Mixed hosted + custom (Python)

```python
import httpx

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search"}
WEATHER    = {"name": "get_weather", "description": "...",
              "input_schema": {"type": "object",
                               "properties": {"city": {"type": "string"}},
                               "required": ["city"]}}

r1 = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "tools": [WEB_SEARCH, WEATHER],
    "messages": [{"role": "user", "content": "Weather in Paris?"}],
}, timeout=60)
msg1 = r1.json()

if msg1["stop_reason"] == "tool_use":
    # Custom tool fired — the standard pause/resume from AGENTIC_INTEGRATION §5
    sid = r1.headers["x-conduit-session-id"]
    tu = next(b for b in msg1["content"] if b["type"] == "tool_use")
    # assert tu["name"] == "get_weather"  (hosted ones never reach here)
    ...
else:
    # Model answered directly, possibly using WebSearch silently
    print(msg1["content"][0]["text"])
```

### 5.4 Rust — `reqwest`

```rust
let body = serde_json::json!({
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    "messages": [{"role": "user", "content": "Latest stable Rust release?"}],
});

let msg: serde_json::Value = reqwest::Client::new()
    .post("http://127.0.0.1:8765/v1/messages")
    .json(&body)
    .send().await?
    .json().await?;

println!("{}", msg["content"][0]["text"].as_str().unwrap_or(""));
```

For streaming (`bytes_stream().eventsource()`) you don't need any
hosted-tool-specific handling — the events look like a normal chat stream.
See `AGENTIC_INTEGRATION.md` §8.4.

### 5.5 curl

```bash
curl -sS -X POST http://127.0.0.1:8765/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 400,
    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    "messages": [{"role": "user", "content": "Latest FastAPI release?"}]
  }'
```

---

## 6. Pitfalls

1. **You'll never see a *client-pause-style* `tool_use` block for WebSearch/WebFetch.** Those are reserved for custom function tools that the client executes. Hosted calls show up as `server_tool_use` (note the prefix). If you see plain `tool_use` with `name: "WebSearch"`, file a bug.
2. **You'll never see `stop_reason: "tool_use"` from a hosted-only request.** The hidden pause is suppressed. The whole hosted dance arrives in one response ending in `stop_reason: "end_turn"`.
3. **WebFetch can't render JS.** If you fetch a SPA, the model gets thin content. For modern docs sites that pre-render or use SSR, you're fine.
4. **The model decides when to call them.** Declaring `web_search` doesn't force it. If the model is confident in its pre-training answer, it may skip. Prompt explicitly ("use web_search to look up X") if you need it.
5. **Multiple invocations per turn are common** for `web_search → web_fetch` chains. Conduit handles them all transparently; you still get a single text response.
6. **Latency is higher.** Expect 2-10× the latency of a tool-less request — each search/fetch round-trip adds seconds.
7. **No way to constrain `allowed_domains` / `blocked_domains` from the request yet.** Anthropic's hosted `web_search` supports those fields; Conduit currently passes the tool through to the SDK as-is, which doesn't expose those filters. Future enhancement.
8. **Session binding rule still applies.** If you reuse a session_id from a hosted-only request, you'll get a 400 because hosted-only sessions are torn down after the response. (Custom-tool sessions stay alive across the pause.)
9. **Citations are model-generated text.** They're not a separate structured field in the response. Parse the markdown if you need URLs as data — the model's format is consistent within a single response but varies across model versions / prompts.

---

## 7. Cost / latency expectations

| Operation | Typical added latency | Notes |
|---|---|---|
| WebSearch (single) | +3-6s | One round trip to the search backend |
| WebFetch (single page) | +2-5s | Faster for cached / small pages |
| Chain (search → fetch) | +5-12s | Common pattern for "find and read X" |
| With `effort: "high"` | additional 2-3× | The thinking budget multiplies through |

These costs come out of your Claude Max subscription's Agent SDK quota (and out of the separate Pro/Max credit pool after 2026-06-15). Hosted tools are *not free* — the SDK uses subscription tokens for the search/fetch operations even though they look "external."

---

## 8. Phase 2.5 limitations

1. **`allowed_domains` / `blocked_domains` not propagated.** If you put them in the `web_search` tool spec, Conduit accepts the field (passes pydantic) but doesn't pass them to the SDK. Future work.
2. **`max_uses` not enforced.** Same — accepted in the spec but the SDK runs as many as the model wants.
3. ~~No `server_tool_use` / `web_search_tool_result` blocks in responses.~~ **Implemented.** Conduit now emits both, Anthropic-canonical. See §3.1.
4. **`user_location` / `user_timezone` not propagated** to the SDK's WebSearch — the SDK uses its own defaults.
5. **Hosted-only sessions can't be reused.** Because we tear them down after the response (no pause expected).

---

## 9. Debugging recipes

### 9.1 422 Unprocessable Entity on the request
- **Most common cause:** you used a non-canonical `name`. Pydantic's `ToolUnionParam` requires *exact* `name: "web_search"` for any `web_search_*` `type`, and `"web_fetch"` for any `web_fetch_*` `type`. Anything else (e.g. `"WebSearch"`, `"web_search_sdk"`) fails the literal check.
- The 422 body is huge — pydantic tries every member of the union and reports one error per failed match. Look for the `WebSearchTool*Param.name` error specifically; it will say `"Input should be 'web_search'"`.
- Fix: use the canonical form `{"type": "web_search_20250305", "name": "web_search"}`.

### 9.2 `tool_use` block appears in the response when I declared web_search
- Either you declared a *custom* function tool that the model picked (different protocol — that's `stop_reason: tool_use` pause/resume from `AGENTIC_INTEGRATION.md` §5).
- Or your declaration was accepted as `ToolParam` (custom) because it included `input_schema` — for hosted tools, omit `input_schema`. The presence of `input_schema` is how pydantic's union discriminates.

### 9.3 `stop_reason: "tool_use"` returned for a hosted-only request
- Same root cause as 9.2 — request was accepted as a *custom* tool declaration (because `input_schema` was present, or `type` didn't match a hosted variant), and Conduit set up a bridge that nobody will satisfy.
- Verify by listing sessions: `GET /v1/sessions` — if a session with your `x-conduit-session-id` is hanging around with `message_count: 0`, it's the bridge case.
- Fix: remove `input_schema` from your hosted-tool declaration. Hosted tools have no input schema (the SDK runs them; the model writes the query/url itself).

### 9.4 Model answers without using web_search even though I declared it
- Tools are advisory. The model decides whether to call them.
- Try a more explicit prompt: "Use web_search to look up X" or "Search the web for the answer".
- Newer-information questions ("what happened today", "current version of") are more likely to trigger it than evergreen ones.

### 9.5 Response is very slow / appears to stall
- Search and fetch each add real network latency. A single search is typically 3-6s; chains can run 10s+.
- For streaming clients: nothing visible streams during the SDK's hidden search/fetch. After the search completes you'll see text deltas resume.
- If it genuinely hangs (>30s with no output), check the server with `CONDUIT_TOOL_DEBUG=1` to see if pump events stopped flowing.

### 9.6 Verify the wiring is right
- Run `scripts/websearch_test.py` — its three demos exercise WebSearch alone, WebFetch alone, and both together. If they pass, your declarations are correct.

---

## 10. Minimum viable integration checklist

- [ ] Confirm `GET /health` returns 200.
- [ ] Send a single request with `tools: [{"type": "web_search_20250305", "name": "web_search"}]` and a fresh-info question. Expect `stop_reason: "end_turn"`, plain text response, no `tool_use` blocks.
- [ ] Send with `tools: [{"type": "web_fetch_20250910", "name": "web_fetch"}]` and a URL in the prompt. Same expectations.
- [ ] If using both: declare both, model picks which to use.
- [ ] If mixing with custom tools: standard pause/resume protocol applies for the custom ones; hosted ones stay invisible regardless.
- [ ] (Optional) Confirm `GET /v1/sessions` shows the auto-allocated session was reaped after each hosted-only request.

---

## 11. References

- `AGENTIC_INTEGRATION.md` — general client guide
- `EFFORT_INTEGRATION.md` — companion guide for the `effort` field
- `TOOLS_HOWTO.md` — server-side implementation design for tool support
- `scripts/websearch_test.py` — runnable demos covering all four scenarios
- Anthropic's hosted web_search documentation (for the underlying tool semantics — Conduit aims for behavioral parity)
