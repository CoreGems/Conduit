# PASSTHROUGH_INTEGRATION

Client-side integration guide for Conduit's hosted-tool **visibility pass-through** — what the response now carries when `WebSearch` / `WebFetch` are invoked, and how to consume it.

Self-contained. If you've already read `WEBSEARCH_INTEGRATION.md`, this is the deep dive on response-shape changes.

---

## 0. TL;DR

Before pass-through, a hosted-tool response gave you just text:

```json
{"content": [{"type": "text", "text": "FastAPI 0.136.1 (source: github.com/...)"}],
 "stop_reason": "end_turn"}
```

Now you get the full Anthropic-canonical chain:

```json
{"content": [
   {"type": "server_tool_use",
    "id": "toolu_01...",
    "name": "WebSearch",
    "input": {"query": "FastAPI latest stable release"}},

   {"type": "web_search_tool_result",
    "tool_use_id": "toolu_01...",
    "content": "Web search results...\n\nLinks: [{...}]\n\n<summary>"},

   {"type": "text", "text": "FastAPI 0.136.1"}
 ],
 "stop_reason": "end_turn"}
```

Same single HTTP request, same `stop_reason: "end_turn"`, no pause. **You can now see what was called and what came back.**

---

## 1. What changed and why

Previous behaviour (Phase 2.5 initial): Conduit's stream filter dropped the model's hosted-tool calls and the SDK's tool-result UserMessage. Clients only saw the model's final text.

Current behaviour (Phase 2.5 visibility): Conduit re-emits the call/result pair in Anthropic-canonical wire format. Client UX, audit logs, and observability tooling can now show "what the model actually did" instead of just the synthesised answer.

The hidden SDK turn boundary (the internal `message_delta(stop_reason: tool_use)` + paired `message_stop` + the continuation `message_start`) is **still suppressed** so it remains one logical turn from the client's perspective.

---

## 2. Response block types

These three are the only ones you'll see for hosted tools:

### 2.1 `server_tool_use`

The model's call to a hosted tool.

```json
{
  "type": "server_tool_use",
  "id":   "toolu_01ABCDEF...",
  "name": "WebSearch",       // or "WebFetch"
  "input": {"query": "..."}  // for WebSearch
  // or:  "input": {"url": "..."}   for WebFetch
}
```

- `id` — the original SDK tool_use id. Used to correlate with the result block.
- `name` — `"WebSearch"` or `"WebFetch"` (PascalCase — these are SDK-internal names, not the lowercase declaration names).
- `input` — the model's chosen arguments. Streamed as `input_json_delta` chunks; complete and parsed by the time `content_block_stop` arrives in streaming mode.

### 2.2 `web_search_tool_result`

The SDK's response to a WebSearch call.

```json
{
  "type": "web_search_tool_result",
  "tool_use_id": "toolu_01ABCDEF...",
  "content": "Web search results for query: \"...\"\n\nLinks: [{\"title\":\"...\",\"url\":\"...\"}, ...]\n\n<short summary the SDK adds>"
}
```

- `tool_use_id` — matches the `id` of the corresponding `server_tool_use` block. Exact-string equality.
- `content` — a **string** (not a list). Format: a header line, then `Links: <JSON array>`, then a textual summary. Format details in §4.

### 2.3 `web_fetch_tool_result`

The SDK's response to a WebFetch call.

```json
{
  "type": "web_fetch_tool_result",
  "tool_use_id": "toolu_01ABCDEF...",
  "content": "<the fetched page's textual content>"
}
```

- `tool_use_id` — same correlation rule.
- `content` — the fetched page's textual content, server-side rendered (no JS execution). For docs/blogs/news this is the main body text.

---

## 3. Block ordering

Within a single response, hosted tool blocks always come in **pairs** in this order:

```
server_tool_use(WebSearch, input={query})
web_search_tool_result(content=<results>)
[optional more pairs if the model chains]
text(text="<final synthesised answer>")
```

A search → fetch chain produces two pairs:

```
server_tool_use(WebSearch, {query})
web_search_tool_result(<list of result urls>)
server_tool_use(WebFetch, {url})
web_fetch_tool_result(<page content>)
text("Based on what I found at <url>, ...")
```

Always assume `content[]` may contain multiple `server_tool_use`/`*_tool_result` pairs before the final text. Don't index hardcoded positions; iterate by type.

---

## 4. Parsing the result string

### 4.1 WebSearch results

The `content` string is shaped like:

```
Web search results for query: "<original query>"

Links: [{"title":"...","url":"..."},{...},...]

<short summary the SDK appends>
```

To extract URLs cleanly:

```python
import re, json

def extract_search_links(content: str) -> list[dict]:
    m = re.search(r"Links: (\[.*?\])\n", content, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

# → [{"title": "Releases · ...", "url": "https://github.com/..."}, ...]
```

⚠️ The format is the **SDK's**, not part of Anthropic's wire spec. It may change with future SDK versions. If your client needs format-stable citations, parse the markdown links the model writes in the trailing `text` block — those are more reliable across SDK upgrades.

### 4.2 WebFetch results

The `content` string is the fetched page's textual representation (HTML stripped, links and structure preserved). No structured `Links:` header for WebFetch — it's just the page content.

Treat it as raw text; don't expect a schema.

---

## 5. Streaming behaviour

The SSE event sequence for a hosted-tool response (one search, then text):

```
event: message_start                            ← session/usage metadata
event: content_block_start    (server_tool_use) ← tool call begins
event: content_block_delta    (input_json_delta) × N  ← query streams in chunks
event: content_block_stop                       ← tool call complete
event: content_block_start    (web_search_tool_result)  ← result block (content already populated)
event: content_block_stop                       ← result block done
event: content_block_start    (text)            ← model's answer begins
event: content_block_delta    (text_delta) × N  ← answer streams
event: content_block_stop
event: message_delta          (stop_reason: end_turn)
event: message_stop
```

**Two important asymmetries:**

1. **`server_tool_use.input` streams in chunks** (just like a custom `tool_use.input`). You must accumulate `input_json_delta.partial_json` strings between `content_block_start` and `content_block_stop`, then `JSON.parse` the concatenation.

2. **`web_search_tool_result` / `web_fetch_tool_result` arrive complete** at `content_block_start` time. The full string is in `content_block.content` immediately — no `content_block_delta` events for these blocks. The `content_block_stop` is just a close marker.

If you were reusing your custom-tool-input-reassembly code for `server_tool_use`, that works identically. The result blocks don't need any reassembly — read `content` directly from `content_block_start`.

Indices stay continuous across the hidden internal turn boundary. If the first `server_tool_use` was at index 0 and the result at index 1, the final text might be at index 2 (single chain), or index 4 (search → fetch → text), and so on.

---

## 6. Code recipes

### 6.1 Python — extract everything that happened

```python
import httpx, json, re

r = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    "messages": [{"role": "user",
                  "content": "What is the latest stable Python release?"}],
}, timeout=120).json()

# Build a list of {kind, ...} events in order
for b in r["content"]:
    t = b["type"]
    if t == "server_tool_use":
        print(f"CALL {b['name']}({b['input']!r})  id={b['id']}")
    elif t == "web_search_tool_result":
        # Extract URLs from the result string
        m = re.search(r"Links: (\[.*?\])\n", b["content"], re.DOTALL)
        urls = [x["url"] for x in json.loads(m.group(1))] if m else []
        print(f"  → {len(urls)} results, first: {urls[0] if urls else '(none)'}")
    elif t == "web_fetch_tool_result":
        print(f"  → fetched page, {len(b['content'])} chars")
    elif t == "text":
        print(f"ANSWER: {b['text']}")
```

### 6.2 Python — official `anthropic` SDK

Hosted tools are in the upstream wire format, so `tools=` accepts them directly. The SDK returns these blocks as typed objects:

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")
msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    tools=[{"type": "web_search_20250305", "name": "web_search"}],
    messages=[{"role": "user", "content": "Latest stable FastAPI?"}],
)

for b in msg.content:
    # SDK exposes the typed dataclasses; use isinstance or check .type
    if b.type == "server_tool_use":
        print(f"call: {b.name}({b.input})")
    elif b.type == "web_search_tool_result":
        print(f"result content[:100]: {b.content[:100]}")
    elif b.type == "text":
        print(f"answer: {b.text}")
```

### 6.3 Python — streaming with manual SSE parsing

```python
import httpx, json

call_inputs: dict[str, str] = {}   # tool_use_id -> accumulating input_json string
finished_calls: list[dict] = []
results: list[dict] = []
text_buf: list[str] = []

with httpx.stream("POST", "http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "stream": True,
    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    "messages": [{"role": "user", "content": "Latest stable Python?"}],
}, timeout=120) as resp:
    event_name = None
    block_meta: dict[int, dict] = {}
    for line in resp.iter_lines():
        if not line:
            event_name = None
            continue
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: "):
            d = json.loads(line[6:])
            t = d.get("type")
            if t == "content_block_start":
                idx = d["index"]
                cb = d["content_block"]
                block_meta[idx] = dict(cb)
                if cb["type"] == "server_tool_use":
                    call_inputs[idx] = ""   # start accumulating
                elif cb["type"] in ("web_search_tool_result", "web_fetch_tool_result"):
                    # content already populated at start time
                    results.append({"tool_use_id": cb["tool_use_id"],
                                    "content": cb["content"]})
            elif t == "content_block_delta":
                idx = d["index"]
                delta = d["delta"]
                if delta["type"] == "input_json_delta" and idx in call_inputs:
                    call_inputs[idx] += delta["partial_json"]
                elif delta["type"] == "text_delta":
                    text_buf.append(delta["text"])
            elif t == "content_block_stop":
                idx = d["index"]
                meta = block_meta.get(idx, {})
                if meta.get("type") == "server_tool_use" and idx in call_inputs:
                    try:
                        meta["input"] = json.loads(call_inputs.pop(idx) or "{}")
                    except json.JSONDecodeError:
                        meta["input"] = {}
                    finished_calls.append(meta)

print("calls:", finished_calls)
print("results:", [(r['tool_use_id'], r['content'][:80] + '...') for r in results])
print("text:", "".join(text_buf))
```

### 6.4 Rust — pattern-match on block type

```rust
#[derive(serde::Deserialize)]
#[serde(tag = "type")]
enum ContentBlock {
    #[serde(rename = "server_tool_use")]
    ServerToolUse { id: String, name: String, input: serde_json::Value },
    #[serde(rename = "web_search_tool_result")]
    WebSearchResult { tool_use_id: String, content: String },
    #[serde(rename = "web_fetch_tool_result")]
    WebFetchResult  { tool_use_id: String, content: String },
    #[serde(rename = "text")]
    Text { text: String },
    // ... other variants ...
}

let body: serde_json::Value = client.post(url).json(&request).send().await?.json().await?;
let blocks: Vec<ContentBlock> = serde_json::from_value(body["content"].clone())?;

for b in blocks {
    match b {
        ContentBlock::ServerToolUse { name, input, .. } => {
            println!("call {} with {:?}", name, input);
        }
        ContentBlock::WebSearchResult { content, .. } => {
            // extract Links: [...] from `content`
        }
        ContentBlock::Text { text } => print!("{}", text),
        _ => {}
    }
}
```

---

## 7. Migration — does this break existing code?

If your client was already parsing hosted-tool responses **as plain text**, the change is **additive**:

- The trailing `text` block is still there with the same content
- The new `server_tool_use` + `*_tool_result` blocks appear *before* it

So this idiom keeps working:

```python
# Still works — concatenate all text blocks, ignore others
text = "".join(b["text"] for b in msg["content"] if b["type"] == "text")
```

You'll **break** if your code:

- Asserted `len(content) == 1` (now there are 3+ blocks per turn that used the tool)
- Used `content[0]` to mean "the answer" (now `content[0]` is a `server_tool_use`)
- Asserted `"tool_use" not in content_types` (now `"server_tool_use"` is present — note the prefix; client-pause `tool_use` is still absent for hosted)

The Anthropic SDK and most well-written generic clients iterate by type and handle unknown types gracefully — those need no changes.

---

## 8. Common questions

### Q. Can I get the structured search results as a JSON list instead of a string?

Not natively yet. Parse the `Links: [...]` section out of `web_search_tool_result.content` (see §4.1). Future work may add a structured `web_search_result_part` shape matching Anthropic's hosted-tool output more precisely; until then, the SDK only exposes the string form to us.

### Q. Will `tool_use_id` collide between hosted and custom tool calls in mixed-mode requests?

No. Custom tool ids start `toolu_` and so do hosted (the SDK uses the same prefix). They're unique per turn — the model picks fresh ids for each call. Match by exact string equality between `server_tool_use.id` and `web_search_tool_result.tool_use_id`.

### Q. Can I suppress the new blocks if my client wants the old text-only behaviour?

Not via a flag. The cheapest filter is client-side:

```python
visible = [b for b in msg["content"] if b["type"] in ("text",)]
```

That gives you the same shape as the pre-passthrough response.

### Q. Why is `name` lowercase in the declaration but PascalCase in `server_tool_use`?

Anthropic-API quirk: the declared `name` field on hosted tool params is a pydantic literal pinned to `"web_search"` / `"web_fetch"` (lowercase). The SDK's *internal* tool name (what the model invokes) is `WebSearch` / `WebFetch` (PascalCase). Conduit forwards the SDK's PascalCase form on the `server_tool_use.name` field so you can tell which tool was used. Match on PascalCase when inspecting responses.

### Q. Does pass-through work for streaming too?

Yes — see §5 for the exact SSE event sequence. Both modes emit the same blocks in the same order.

### Q. What about `usage` numbers — do search/fetch tokens count?

The SDK reports total tokens including the internal tool round-trips in the `usage.output_tokens` of the response. There's no separate breakdown by tool. If you need that, parse `web_search_tool_result.content` and estimate — or wait for Phase 3 server-side telemetry.

---

## 9. Minimum viable integration checklist

- [ ] Confirm a hosted-tool response now has `content[]` length > 1 (was 1 in the pre-passthrough world)
- [ ] Iterate by `type`, not by index, when consuming `content[]`
- [ ] Handle `server_tool_use`, `web_search_tool_result`, `web_fetch_tool_result` types (skip / parse / display as fits your UX)
- [ ] (Optional) Add a URL extractor for `web_search_tool_result.content` if you display sources separately from the model's text
- [ ] In streaming: handle `server_tool_use.input` as `input_json_delta` reassembly; handle result blocks as content-already-populated-at-start
- [ ] Run `scripts/passthrough_test.py` to verify end-to-end on your installation

---

## 10. References

- `WEBSEARCH_INTEGRATION.md` — companion guide for declaring and using hosted tools
- `AGENTIC_INTEGRATION.md` §5.6 — general client guide section on hosted tools
- `TOOLS_HOWTO.md` — server-side design (custom tool pause/resume, for context)
- `scripts/passthrough_test.py` — runnable end-to-end demo
- `scripts/websearch_test.py` — broader hosted-tool demo (declaration variants, mixed-mode)
