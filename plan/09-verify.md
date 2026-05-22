# Step 9 — Verify & Run

## Goal
End-to-end checklist proving the server is production-ready for local use.

## Pre-flight

```powershell
# 1. Make sure Claude Code is logged in
claude --version

# 2. CRITICAL: ANTHROPIC_API_KEY must NOT be set in this shell.
$env:ANTHROPIC_API_KEY
# Should print blank. If not:
Remove-Item env:ANTHROPIC_API_KEY
```

If `ANTHROPIC_API_KEY` is set, the Agent SDK uses metered API billing
**instead** of your Max subscription — silently. Always check.

## Smoke sequence

```powershell
# Start server
uv run conduit

# In another shell, run each check:

# Health
curl http://127.0.0.1:8787/health
# Expect: {"status":"ok"}

# Swagger UI
start http://127.0.0.1:8787/docs

# Stateless single-shot (real Anthropic-style)
curl -X POST http://127.0.0.1:8787/v1/messages `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"claude-sonnet-4-6\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}'

# Streaming
curl -N -X POST http://127.0.0.1:8787/v1/messages `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"claude-sonnet-4-6\",\"max_tokens\":128,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"count to 3\"}]}'

# Stateful — memory across turns (see step 7 PowerShell snippet)

# Compatibility test using the official Anthropic SDK
uv run pytest -m integration
```

## Done when
- All smoke commands above succeed.
- `pytest -m integration` is green.
- A Rust `reqwest` client at `http://127.0.0.1:8787/v1/messages` with the same
  request shape as the Anthropic API streams events identically.

## Operational notes
- **Single user, local only.** No auth, bound to `127.0.0.1`. Do not expose
  to a network as-is — there is no rate limiting, no auth, and Agent SDK
  errors are surfaced verbatim.
- **One concurrent turn per session.** The per-session `asyncio.Lock`
  serializes requests against the same `session_id`. Parallel turns require
  separate sessions.
- **Idle eviction.** Sessions are dropped after `CONDUIT_SESSION_IDLE_TIMEOUT_S`
  seconds of inactivity (default 1800).
- **Budget after 2026-06-15.** Agent SDK usage will draw from the separate
  monthly Pro/Max credit pool, not the main Claude Code subscription. Plan
  load accordingly.

## Next steps (out of scope for this plan)
- Persistent session storage (Redis / SQLite) for cross-restart durability
- Auth header check (single shared secret) if ever exposed beyond localhost
- Tool-use passthrough so clients can declare `tools` in requests
- OpenAI-format adapter (`POST /v1/chat/completions`) for non-Anthropic clients

## Depends on
Steps 1–8.
