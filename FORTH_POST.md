# [Update] Conduit now does hosted web tools — WebSearch + WebFetch, one HTTP request, no client loop

Last week I added client-defined tool calling to Conduit (the local Anthropic-compatible server that routes through Claude Max via the Agent SDK). This week: **hosted tools**. Specifically `WebSearch` and `WebFetch`, exposed in the same shape Anthropic uses for their hosted server tools.

The interesting part is the protocol difference between *custom* tools and *hosted* tools, and why hosted is great when it fits.

## Custom vs hosted, side by side

| | Custom tools (`get_weather`) | Hosted tools (`WebSearch`/`WebFetch`) |
|---|---|---|
| Client requests per turn | 2+ (initial + 1 per `tool_result`) | **1** |
| Client implements the tool? | Yes — your code runs the work | No — SDK runs it |
| Client sees `tool_use` blocks? | Yes (must parse + execute + send `tool_result`) | **No** (filtered) |
| `stop_reason` you get back | `tool_use` (pause) → then `end_turn` | **`end_turn`** straight away |
| Where the orchestration loop lives | Your code | Inside the model |

Hosted tools are the right answer when:
- You don't want to implement the tool yourself (web search, file fetch, code execution, etc.)
- You don't need granular control over which URLs get fetched in what order
- One round-trip latency is the goal

## What you write

```python
import httpx

r = httpx.post("http://127.0.0.1:8765/v1/messages", json={
    "model": "claude-sonnet-4-6",
    "max_tokens": 512,
    "tools": [
        {"type": "web_search_20250305", "name": "web_search"},
        {"type": "web_fetch_20250910",  "name": "web_fetch"}
    ],
    "messages": [{"role": "user",
                  "content": "What's the latest stable FastAPI? Cite the source."}]
})
msg = r.json()
print(msg["content"][0]["text"])
# **0.136.1**
#
# Sources:
# - [Releases · fastapi/fastapi](https://github.com/...)
# - [fastapi · PyPI](https://pypi.org/...)
```

One request. The model on the server side decided to search, picked the best result URL, possibly fetched the page, wrote a synthesized answer with citations woven into the markdown. You wrote no loop.

## What's happening behind the scenes

The Agent SDK is loop-based: it runs the model, when the model emits `tool_use` for WebSearch the SDK executes the search itself, feeds the snippets back, the model picks a URL, the SDK runs WebFetch, feeds the page back, etc. — all in one `receive_response()` iteration.

But that loop's intermediate boundaries leak as Anthropic SSE events: `message_delta` with `stop_reason: "tool_use"`, `message_stop`, then a fresh `message_start` for the continuation. From the client's perspective that looks like a pause that needs a `tool_result` to resume — but for hosted tools the client *can't* satisfy it (the SDK already did).

So Conduit's stream translator now:
- **Filters** the `tool_use` content blocks whose name is `WebSearch`/`WebFetch`
- **Tracks** "did we forward any visible (custom) tool_use this message?"
- When `stop_reason: "tool_use"` arrives with zero visible tool_use → **suppresses** that `message_delta`, the paired `message_stop`, **and** the next `message_start` (continuation marker)

Net effect: client sees one continuous turn ending in `stop_reason: "end_turn"`. The hidden internal pauses are erased.

## Mixed bag works too

You can declare hosted and custom tools in the same `tools[]`. Hosted ones run silently; custom ones still trigger the normal pause/resume protocol. If the model picks `get_weather`, you get a `stop_reason: "tool_use"` pause; if it picks `web_search`, you don't.

```json
"tools": [
  {"type": "web_search_20250305", "name": "web_search"},
  {"name": "get_weather", "input_schema": {...}}
]
```

## Honest costs

- **Latency**: each search adds ~3-6s, each fetch ~2-5s. Chains can run 10s+. Not the path for ultra-low-latency UX.
- **Subscription quota**: the SDK's tool calls do consume your Max plan's Agent SDK tokens. Not free.
- **Over-eagerness**: Sonnet/Haiku tend to search when it "might" help, even for evergreen questions. Anchor with a system prompt if you mind: "Only use web tools when info would be out-of-date or you don't already know it."

## Phase 2.5 limits

- `allowed_domains` / `blocked_domains` / `max_uses` not yet wired through to the SDK
- WebFetch doesn't render JavaScript (server-side fetch only)
- Hosted-only sessions are torn down after the response (no resume cycle — there's nothing to resume)

## Docs + demos

- `WEBSEARCH_INTEGRATION.md` — full client integration reference
- `AGENTIC_INTEGRATION.md` — general client guide  
- `scripts/websearch_test.py` — runnable demos: WebSearch alone, WebFetch alone, both together, mixed hosted+custom

Same Conduit caveats as always: local-only on `127.0.0.1`, no auth, single-user. From 2026-06-15 Agent SDK draws from a separate monthly credit pool from the main Max plan.

Repo: `<link>`
