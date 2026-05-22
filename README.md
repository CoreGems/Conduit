# Conduit

Anthropic-compatible local Messages API server, powered by
[`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python).

Conduit exposes `POST /v1/messages` with the **exact same wire format as
`https://api.anthropic.com/v1/messages`**, but routes every request through
your Claude Max subscription via the Agent SDK's OAuth session instead of a
metered API key. Any client that can talk to the Anthropic API — including
the official `anthropic` Python and TypeScript SDKs — works against Conduit
with just a `base_url` change.

## Why

You're on Claude Max already. The Agent SDK lets you reuse that subscription
from your own programs. Conduit puts an Anthropic-shaped HTTP surface in
front of it so:

- Tauri/Rust apps (or anything else that speaks HTTP+SSE) get a stable, local
  endpoint that doesn't change when the SDK does.
- Existing Anthropic-SDK code drops in with one constructor change.
- Multi-turn chat state lives server-side, addressed by `session_id`.

## Endpoints

| Method | Path                 | Purpose                                          |
| ------ | -------------------- | ------------------------------------------------ |
| POST   | `/v1/messages`       | Anthropic-compatible. Optional `session_id`.     |
| GET    | `/v1/sessions`       | List active sessions                             |
| POST   | `/v1/sessions`       | Create empty session → `{session_id}`            |
| DELETE | `/v1/sessions/{id}`  | Tear down session                                |
| GET    | `/health`            | Liveness                                         |
| GET    | `/docs`              | Swagger UI — free testing console                |

`session_id` is the only Conduit extension to the Anthropic schema:

- **Omitted** → behaves like Anthropic: stateless, an ephemeral session is
  created for the request and torn down afterward.
- **Provided** → server keeps history. Send only the new user turn; the
  Agent SDK threads conversation state.

## Quick start

### Prerequisites

- Miniconda / conda (or any Python 3.11 environment manager)
- `claude` CLI installed and logged in once (`claude` interactively) — Agent
  SDK reuses that OAuth session
- **`ANTHROPIC_API_KEY` UNSET** in the env, otherwise the SDK falls back to
  metered API billing silently

### Install

```powershell
conda create -n conduit python=3.11 -y
conda run -n conduit pip install -e ".[dev]"
```

### Run

```powershell
conda run -n conduit --no-capture-output python -m uvicorn conduit.app:app --host 127.0.0.1 --port 8765
```

Browse to <http://127.0.0.1:8765/docs> for the interactive Swagger UI.

## Use it

### From the official Anthropic Python SDK

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8765", api_key="not-used")

msg = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Say hi in three words."}],
)
print(msg.content[0].text)
```

Streaming works identically:

```python
with client.messages.stream(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Count to five."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

### From Rust (`reqwest` + `eventsource-stream`)

```rust
use reqwest::Client;
use eventsource_stream::Eventsource;
use futures::stream::StreamExt;

let body = serde_json::json!({
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 128,
    "stream": true,
    "messages": [{"role": "user", "content": "Hello"}],
});

let mut stream = Client::new()
    .post("http://127.0.0.1:8765/v1/messages")
    .json(&body)
    .send().await?
    .bytes_stream()
    .eventsource();

while let Some(event) = stream.next().await {
    let event = event?;
    println!("{}: {}", event.event, event.data);
}
```

### Stateful multi-turn chat

```powershell
# Create a session
$sid = (Invoke-RestMethod -Method Post http://127.0.0.1:8765/v1/sessions -ContentType 'application/json' -Body '{}').session_id

# Turn 1
$body = @{ model="claude-haiku-4-5-20251001"; max_tokens=128; session_id=$sid;
           messages=@(@{role="user"; content="Remember 42."}) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post http://127.0.0.1:8765/v1/messages -ContentType 'application/json' -Body $body

# Turn 2 — same session_id, server remembers
$body = @{ model="claude-haiku-4-5-20251001"; max_tokens=128; session_id=$sid;
           messages=@(@{role="user"; content="What number did I just tell you?"}) } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post http://127.0.0.1:8765/v1/messages -ContentType 'application/json' -Body $body

# Cleanup
Invoke-RestMethod -Method Delete "http://127.0.0.1:8765/v1/sessions/$sid"
```

## Configuration

All settings via env (`CONDUIT_*` prefix) or `.env` file:

| Variable                          | Default                       | Meaning                              |
| --------------------------------- | ----------------------------- | ------------------------------------ |
| `CONDUIT_HOST`                    | `127.0.0.1`                   | Bind address                         |
| `CONDUIT_PORT`                    | `8765`                        | Port                                 |
| `CONDUIT_DEFAULT_MODEL`           | `claude-sonnet-4-6`           | Used when client doesn't specify     |
| `CONDUIT_DEFAULT_SYSTEM_PROMPT`   | *(none)*                      | Server-side default system prompt    |
| `CONDUIT_ALLOWED_TOOLS`           | `[]`                          | Tools the agent may use (pure chat by default) |
| `CONDUIT_SESSION_IDLE_TIMEOUT_S`  | `1800`                        | Sessions evicted after N idle seconds |
| `CONDUIT_MAX_SESSIONS`            | `100`                         | Hard cap on concurrent sessions      |

## Tests

```powershell
# Unit (offline, fast)
conda run -n conduit python -m pytest tests/test_schema.py tests/test_routes_unit.py

# Integration — requires server running + Claude Max OAuth login
conda run -n conduit python -m pytest -m integration
```

## Limitations / Notes

- **Single-host, single-user.** No auth, bound to `127.0.0.1`. Don't expose to
  a network without adding auth and rate limiting first.
- **One concurrent turn per session.** A per-session `asyncio.Lock` serializes
  requests against the same `session_id`. Parallel work requires multiple sessions.
- **Model is bound at session creation.** When a stateful session is created
  with model X, all subsequent turns through that session use X regardless of
  the `model` field the client sends. The response echoes the session's
  actual model.
- **`thinking` blocks are filtered.** The Agent SDK may emit extended-thinking
  content; Conduit strips those events from the SSE stream so callers see a
  pure chat stream. Text block indices are renumbered to stay contiguous.
- **Budget after 2026-06-15.** Agent SDK usage will draw from a separate
  monthly Pro/Max credit pool, not the main Claude Code subscription.

## Architecture

```
your client (rust/python/ts)
        │  HTTP + SSE  (Anthropic wire format)
        ▼
FastAPI  ──►  SessionManager  ──►  ClaudeSDKClient (one per session_id)
                                          │
                                          ▼
                                  claude-agent-sdk
                                          │
                                          ▼
                              Claude Max subscription (OAuth)
```

See [`plan/`](./plan/) for the step-by-step implementation guide that built this.
