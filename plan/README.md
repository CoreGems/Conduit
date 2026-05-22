# Conduit — Implementation Plan

Anthropic-compatible local Messages API server, powered by `claude-agent-sdk`.
Drop-in replacement for `https://api.anthropic.com/v1/messages` that routes
through your Claude Max subscription (Agent SDK + OAuth) instead of a metered
API key.

## Architecture

```
Tauri / Rust client
        │  HTTP + SSE  (Anthropic wire format)
        ▼
FastAPI app  ──►  SessionManager  ──►  ClaudeSDKClient (one per session_id)
                                            │
                                            ▼
                                    claude-agent-sdk
                                            │
                                            ▼
                                Claude Max subscription (OAuth)
```

## Endpoints

| Method | Path                     | Purpose                                       |
| ------ | ------------------------ | --------------------------------------------- |
| POST   | `/v1/messages`           | Anthropic-compatible. Optional `session_id`.  |
| GET    | `/v1/sessions`           | List active sessions                          |
| POST   | `/v1/sessions`           | Create empty session → `{session_id}`         |
| DELETE | `/v1/sessions/{id}`      | Tear down session                             |
| GET    | `/health`                | Liveness                                      |
| GET    | `/docs`                  | Swagger UI — your free testing console        |

## Sequence

1. [Bootstrap](./01-bootstrap.md)            — project scaffold, deps
2. [Config](./02-config.md)                  — env-driven settings module
3. [Schema](./03-schema.md)                  — pydantic models matching Anthropic
4. [Sessions](./04-sessions.md)              — SessionManager
5. [Streaming](./05-streaming.md)            — Agent SDK → Anthropic SSE translator
6. [Messages endpoint](./06-messages-endpoint.md) — POST /v1/messages
7. [Session endpoints](./07-session-endpoints.md) — auxiliary lifecycle endpoints
8. [Tests](./08-tests.md)                    — pytest + httpx + anthropic-SDK compat
9. [Verify & run](./09-verify.md)            — dev workflow, smoke checks

Each step is independently buildable and has its own acceptance criteria.
Do not start step N+1 until step N's Verify section passes.

## Prerequisites

- Python 3.11+
- `uv` installed (`winget install astral-sh.uv` or `pip install uv`)
- Claude Code CLI logged in once (`claude` interactively) — Agent SDK reuses that OAuth session
- `ANTHROPIC_API_KEY` **unset** in the shell that runs the server, otherwise the
  SDK falls back to metered API billing instead of your subscription

## Design constraints

- **Bind `127.0.0.1` only** — local server, not LAN-exposed
- **Wire-format parity with Anthropic** — any client that talks to
  `api.anthropic.com/v1/messages` must work against this server with only a
  `base_url` change
- **`session_id` is a Conduit extension** — when omitted, behave statelessly
  like real Anthropic; when present, server keeps history
- **No UI** — Swagger UI at `/docs` is the human-facing surface
