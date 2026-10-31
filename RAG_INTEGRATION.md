# RAG_INTEGRATION

Client-side guide for the three upstream Anthropic features a retrieval
pipeline leans on, as served by Conduit: **`document` content blocks**,
**citations**, and **`output_config`** (canonical `effort` + structured-output
`format`). None of these is a Conduit extension; the request and response
shapes are the standard Anthropic ones. Self-contained.

---

## 0. TL;DR card

| Question | Answer |
|---|---|
| Can I send retrieved passages as `document` blocks? | Yes. Text, base64 PDF and `content`-source documents on the **newest** user turn are forwarded to the model verbatim, alongside `text` and `image` blocks. |
| Do citations come back? | Yes. Put `"citations": {"enabled": true}` on each document. The answer arrives as several `text` blocks; cited ones carry a `citations` array (non-streaming) / `citations_delta` events (streaming). |
| Does `output_config.effort` work? | Yes. It is the preferred spelling; Conduit's top-level `effort` remains as an alias. With the official SDKs it is a plain keyword argument. |
| Does structured output work? | Yes. `output_config.format = {"type": "json_schema", "schema": {...}}` returns exactly one `text` block whose text is the JSON document, `stop_reason: "end_turn"`. |
| Binding | `output_config` (both members) binds at **session creation**, like `model` / `effort`. Documents are per turn. |
| Official SDK | `client.messages.create(..., output_config={...})` and `client.messages.parse(..., output_format=Model)` work unchanged against Conduit's `base_url`. |

---

## 1. Document blocks

### 1.1 Shapes

All three upstream source types pass through:

```json
{"type": "document",
 "source": {"type": "text", "media_type": "text/plain", "data": "…passage…"},
 "title": "Board memo 2026-03",                 // optional
 "context": "Retrieved for: runway question",   // optional, not shown as citable text
 "citations": {"enabled": true}}                // optional, see §2
```

```json
{"type": "document",
 "source": {"type": "content", "content": [
    {"type": "text", "text": "chunk 1 …"},
    {"type": "text", "text": "chunk 2 …"}]},
 "title": "Retrieved windows",
 "citations": {"enabled": true}}
```

```json
{"type": "document",
 "source": {"type": "base64", "media_type": "application/pdf", "data": "<base64>"},
 "title": "Q3 deck"}
```

Put the documents **before** the question text in the content list, upstream's
recommended order:

```json
{"role": "user", "content": [
  {"type": "document", ...},
  {"type": "document", ...},
  {"type": "text", "text": "What was Q3 revenue? Cite the memo."}
]}
```

### 1.2 What reaches the model

| Mode | Behaviour |
|---|---|
| Stateless (`session_id` omitted) | The **newest** user turn is forwarded as a content-block list (documents, images and text, in your order). Earlier turns are flattened to text for the history prefix; `document` and `image` blocks in those earlier turns are dropped. |
| Stateful (`session_id`) | The new user turn is forwarded as content blocks. Documents sent on earlier turns of the same session remain in the server-side history and the model still sees them. |
| Tool-use turns | Same as above; documents and tools combine freely. |

Conduit forwards blocks byte-for-byte; `cache_control` on a document block
goes through untouched.

---

## 2. Citations

### 2.1 Enabling

Set `"citations": {"enabled": true}` on every document (upstream requires all or
none). Nothing else changes in the request.

### 2.2 Non-streaming response

The answer is split into multiple `text` blocks. Blocks that cite carry a
`citations` array; plain blocks have none:

```json
"content": [
  {"type": "text", "text": "The launch code is ZEBRA-4471.",
   "citations": [{"type": "char_location",
                  "cited_text": "The launch code is ZEBRA-4471. ",
                  "document_index": 0, "document_title": "Memo Q3",
                  "start_char_index": 15, "end_char_index": 46}]},
  {"type": "text", "text": " "},
  {"type": "text", "text": "The warehouse is in Tallinn.",
   "citations": [{"type": "content_block_location",
                  "cited_text": "Window 1: The warehouse moved to Tallinn in March.",
                  "document_index": 1, "document_title": "Retrieved windows",
                  "start_block_index": 0, "end_block_index": 1}]}
]
```

| Document source | Citation `type` | Location fields |
|---|---|---|
| `text` | `char_location` | `start_char_index`, `end_char_index` (0-based, end exclusive) |
| `content` | `content_block_location` | `start_block_index`, `end_block_index` (into the `source.content` list) |
| `base64` PDF | `page_location` | `start_page_number`, `end_page_number` (1-based) |

`document_index` counts **document blocks only**, in the order you sent them,
regardless of any `text` or `image` blocks between them. Build your numbered
footnotes from `(document_index, location)`.

### 2.3 Streaming response

A cited block announces itself with an empty `citations` list on
`content_block_start`, then interleaves `citations_delta` with `text_delta`:

```
event: content_block_start
data: {"type":"content_block_start","index":1,
       "content_block":{"type":"text","text":"","citations":[]}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,
       "delta":{"type":"citations_delta","citation":{"type":"char_location", ...}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,
       "delta":{"type":"text_delta","text":"The launch code is ZEBRA-4471."}}

event: content_block_stop
```

Accumulate `citations_delta.citation` per block index exactly as you
accumulate `text_delta.text`. The official SDKs' stream helpers already do
this: the final message's `TextBlock.citations` is populated.

### 2.4 Invariants you can test against

- A request with citations enabled never yields `citations` on a block
  without a matching `citations_delta` / array entry, so "every citation has a
  `document_index` within `[0, N_documents)`" holds.
- Blocks with no citations have no `citations` key (non-streaming) or no
  `citations` field on `content_block_start` (streaming).
- The model can still answer from general knowledge if the question isn't
  covered; such sentences are simply uncited. Enforce grounding in the system
  prompt if you need it.

---

## 3. `output_config`

```json
"output_config": {
  "effort": "high",
  "format": {"type": "json_schema", "schema": {...}}
}
```

| Member | Effect | Validation |
|---|---|---|
| `effort` | Same five values as the top-level `effort` alias (`low` … `max`). When both are sent, `output_config.effort` wins. Resolution: `output_config.effort` → top-level `effort` → `CONDUIT_DEFAULT_EFFORT` → SDK default. | 422 on an unknown value. |
| `format` | Structured output. Forwarded to the Agent SDK's `output_format`; see §4 for the response shape. | `type` must be `"json_schema"` and `schema` must be an object, else 422. |
| anything else (`task_budget`, …) | Accepted and ignored. | none |

Both members are **bound at session creation**. On a stateful session
(`session_id`) the per-request `output_config` is ignored; create a new
session (or `POST /v1/sessions` with an `output_config` body) to change them.

---

## 4. Structured output response shape

Upstream parity: one `text` block, the JSON document as its text, `end_turn`.

```json
{"content": [{"type": "text", "text": "{\"name\": \"Marcus Chen\", \"age\": 34}"}],
 "stop_reason": "end_turn", ...}
```

Streaming:

```
message_start
content_block_start   {"type":"text","text":""}
content_block_delta   text_delta '{"name": "Marcus'
content_block_delta   text_delta ' Chen", "age": 34}'
content_block_stop
message_delta         stop_reason=end_turn
message_stop
```

How it works underneath (you don't need this, but it explains the edges): the
Agent SDK implements structured output with a CLI-internal end-turn tool. The
model fills the tool input with the JSON; Conduit rewrites that call into the
`text` block above and the trailing `tool_use` stop into `end_turn`. Because
of that:

- **Prose is suppressed.** Any ordinary `text` block the model emits in a
  structured-output session is dropped, so `content[0].text` is always the
  JSON. `thinking` blocks still appear when `include_thinking` is on.
- **Custom tools still pause normally.** Declaring `tools` together with
  `output_config.format` works: real tool calls return `stop_reason: tool_use`
  for you to resolve; the final JSON turn ends with `end_turn`.
- **No citations with structured output.** Upstream returns a 400 for the
  combination; Conduit doesn't enforce it and the behaviour is undefined.
- The JSON is validated against the schema by the Agent SDK, not by Conduit.
  `json.loads` + your own schema check is still a good idea on the client.

---

## 5. Code recipes

### 5.1 httpx: ask over retrieved windows with citations

```python
import httpx

URL = "http://127.0.0.1:8765"

def ask(question: str, windows: list[dict]) -> tuple[str, list[dict]]:
    """windows: [{"title": str, "text": str}, ...] → (answer, citations)"""
    docs = [{
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": w["text"]},
        "title": w["title"],
        "citations": {"enabled": True},
    } for w in windows]

    r = httpx.post(f"{URL}/v1/messages", json={
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "output_config": {"effort": "medium"},
        "system": "Answer only from the provided documents.",
        "messages": [{"role": "user", "content": docs + [{"type": "text", "text": question}]}],
    }, timeout=180)
    r.raise_for_status()
    msg = r.json()

    answer, cites = [], []
    for block in msg["content"]:
        if block["type"] != "text":
            continue
        for c in block.get("citations") or []:
            cites.append(c)
            answer.append(f"{block['text']}[{len(cites)}]")
            break
        else:
            answer.append(block["text"])
    return "".join(answer), cites

text, cites = ask("Where is the warehouse?", [
    {"title": "ops-2026-03", "text": "The warehouse moved to Tallinn in March."},
    {"title": "hr-2026-09",  "text": "Headcount is 37 as of September."},
])
print(text)
for i, c in enumerate(cites, 1):
    print(f"[{i}] doc {c['document_index']} ({c['document_title']}): {c['cited_text']!r}")
```

### 5.2 Official `anthropic` SDK: structured output

```python
from anthropic import Anthropic
from pydantic import BaseModel

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

# Raw schema
msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    output_config={
        "effort": "low",
        "format": {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {"headline": {"type": "string"},
                           "risk_score": {"type": "integer"}},
            "required": ["headline", "risk_score"],
            "additionalProperties": False}},
    },
    messages=[{"role": "user", "content": "Summarise: runway 9 months, burn rising."}],
)
import json
brief = json.loads(msg.content[0].text)

# Or let the SDK build the schema and parse the result
class Brief(BaseModel):
    headline: str
    risk_score: int

parsed = client.messages.parse(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    output_format=Brief,
    messages=[{"role": "user", "content": "Summarise: runway 9 months, burn rising."}],
)
print(parsed.parsed_output)   # Brief(headline=..., risk_score=...)
```

### 5.3 Official `anthropic` SDK: streaming citations

```python
with client.messages.stream(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": [
        {"type": "document",
         "source": {"type": "text", "media_type": "text/plain", "data": memo_text},
         "title": "Memo", "citations": {"enabled": True}},
        {"type": "text", "text": "Who is the CFO?"},
    ]}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
    final = stream.get_final_message()

for block in final.content:
    if block.type == "text" and block.citations:
        for c in block.citations:
            print("\n  cites doc", c.document_index, repr(c.cited_text))
```

### 5.4 Rust (`reqwest`): structured output

```rust
let body = serde_json::json!({
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "output_config": {"format": {"type": "json_schema", "schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]}}},
    "messages": [{"role": "user", "content": "Name one European capital."}],
});
let msg: serde_json::Value = reqwest::Client::new()
    .post("http://127.0.0.1:8765/v1/messages")
    .json(&body).send().await?.json().await?;
let obj: serde_json::Value =
    serde_json::from_str(msg["content"][0]["text"].as_str().unwrap_or("{}"))?;
println!("{}", obj["city"]);
```

---

## 6. Pitfalls

1. **Documents in earlier turns vanish on stateless replay.** Resend the
   documents on the turn that asks the question, or use a stateful session.
2. **`document_index` counts documents, not content blocks.** Interleaved
   `text`/`image` blocks don't shift it.
3. **An uncited answer isn't an error.** If the documents don't cover the
   question the model may answer from general knowledge without citations.
   Constrain it in `system`.
4. **Structured output drops prose.** If you want an explanation *and* data,
   put the explanation in the schema.
5. **`output_config` is session-bound.** Sending a different `format` or
   `effort` on an existing `session_id` does nothing.
6. **Don't mix citations and `output_config.format`.** Upstream 400s; Conduit
   doesn't guard it.
7. **Keep `effort` in one place.** If you send both `effort` and
   `output_config.effort`, the latter wins silently.
8. **Large documents cost input tokens every turn in stateless mode.** Use a
   stateful session when the same corpus backs several questions, so the
   SDK's prompt cache does the work (`usage.cache_read_input_tokens`).

---

## 7. Minimum-viable integration checklist

- [ ] A question + one `document` (citations enabled) returns text that uses
      the document's content and at least one `citations` entry.
- [ ] `citations[*].document_index` is within range of the documents you sent.
- [ ] Streaming: you accumulate `citations_delta` per block index.
- [ ] `output_config: {"format": {...}}` returns one `text` block that
      `json.loads` accepts and `stop_reason == "end_turn"`.
- [ ] `output_config: {"effort": "low"}` is accepted (200) and, through the
      official SDK, needs no `extra_body`.
- [ ] A bad `format` (`{"type": "text"}`) is a 422, proving validation is on.

---

## 8. References

- `AGENTIC_INTEGRATION.md` §2.1 (fields), §3.1 (content blocks), §6 (limits)
- `EFFORT_INTEGRATION.md` for the effort levels themselves
- `tests/test_document_input.py`, `tests/test_citations.py`,
  `tests/test_structured_output.py`, `tests/test_output_config.py`
- `scripts/rag_live_check.py`: end-to-end check against a running server
