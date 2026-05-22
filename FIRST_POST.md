# [Release] Conduit — drop-in Anthropic API server backed by your Claude Max subscription

```
TL;DR — point the official `anthropic` SDK at http://127.0.0.1:8765 and every
call routes through your Max subscription via the Agent SDK's OAuth instead
of a metered API key. Streaming, multi-turn sessions, ~250 lines of Python.
```

I have a Max subscription and got tired of either burning API credits for tinkering or wiring `claude-agent-sdk` into every prototype directly. Conduit is a small FastAPI server that exposes `POST /v1/messages` with the **exact same wire format as `api.anthropic.com`**, but routes requests through `claude-agent-sdk` under the hood. The official Anthropic Python and TypeScript SDKs work against it with one constructor change:

```python
from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

# Identical API, no Anthropic billing — uses your Max OAuth session
msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": "hi"}],
)
```

**What works**

- `POST /v1/messages` — non-streaming and SSE streaming, byte-identical event format
- `session_id` extension — server keeps multi-turn history; omit it for stateless Anthropic-style behavior
- `GET/POST/DELETE /v1/sessions` — lifecycle endpoints
- Swagger UI at `/docs` for free, no UI to build
- Verified by pointing the official `anthropic` SDK at it — both `messages.create` and `messages.stream` pass

**What it's NOT**

- Not a hosted service. Local single-user. Bound to `127.0.0.1`, no auth, no rate limiting.
- Not a tool-use platform (yet) — pure chat. Thinking blocks are filtered out of the SSE stream.
- Not magic. After **2026-06-15**, Anthropic splits Agent SDK usage into a separate monthly Pro/Max credit pool ($20/$100/$200), so this won't draw from the main interactive Claude Code subscription anymore.

**Install**

```powershell
git clone <repo>
conda create -n conduit python=3.11 -y
conda run -n conduit pip install -e ".[dev]"
.\run.ps1
```

Then visit `http://127.0.0.1:8765/docs`. Or run `python scripts/inference_demo.py` for an end-to-end demo (non-streaming, streaming with TTFT, and multi-turn memory check).

**Stack**: Python 3.11 · FastAPI · sse-starlette · `claude-agent-sdk` 0.2.84 · `anthropic` types for guaranteed schema parity.

Repo: `<link>`

Built it primarily for a Tauri/Rust app I'm working on, but it works just as well from any language that speaks HTTP+SSE. Feedback and bug reports welcome — especially edge cases in the streaming translator (it strips thinking blocks and renumbers indices, which is the only non-trivial bit).
