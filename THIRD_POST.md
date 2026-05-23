# [Update] Conduit now exposes the Claude `effort` enum — control thinking budget per request

Small but useful addition to Conduit (the local Anthropic-compatible server that routes through your Claude Max subscription via the Agent SDK). You can now control thinking budget with the same `effort` enum Claude Code uses:

```json
POST /v1/messages
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 1024,
  "effort": "high",
  "messages": [{"role": "user", "content": "Find primes of 851"}]
}
```

`effort` takes one of `"low"` / `"medium"` / `"high"` / `"xhigh"` / `"max"`. It maps directly to `ClaudeAgentOptions.effort` in the SDK, which is the same knob the Claude Code CLI exposes.

## Why it matters

The Anthropic API's `thinking: {type: "enabled", budget_tokens: N}` field gives precise control but you have to pick a token number. The `effort` enum gives the friendlier mental model — five named levels with predictable cost/latency tradeoffs:

| effort | typical latency vs `low` | When to reach for it |
|---|---|---|
| `low` | baseline | Format conversion, one-line rewrites |
| `medium` | ~1.5× | Most chat, drafting (default if unset) |
| `high` | ~3× | Cross-file debugging, design discussion |
| `xhigh` | ~6× | Hard math, multi-step planning, security review |
| `max` | up to ~15× | Research-grade reasoning, long causal chains |

Each step is meaningfully larger than the last — not linear. `max` is genuinely slow and expensive.

## A few gotchas worth flagging upfront

1. **Bound at session creation**, same rule as `model`. If you send `effort` on a follow-up turn through a stateful session, it's silently ignored. Make a new session if you want a new level.

2. **Conduit-only field** — not part of upstream Anthropic. Using the official `anthropic` SDK? Pass it through `extra_body`, otherwise the SDK strips it:
   ```python
   client.messages.create(
       model="claude-sonnet-4-6",
       max_tokens=1024,
       extra_body={"effort": "high"},   # ← not a top-level kwarg
       messages=[...],
   )
   ```

3. **Lowercase exact values only.** `"High"`, `"HIGH"`, `"extreme"` → 422 with a clear validation error.

4. **Don't reach for `max` reflexively** — especially after 2026-06-15 when Agent SDK usage starts drawing from a separate monthly credit pool (Pro $20 / Max $100 / Max20 $200). Burns through faster than you might guess at `xhigh`/`max`.

## Quick proof it's wired

`scripts/effort_test.py` runs a latency sweep across all five levels with the same reasoning-heavy prompt, plus a validation pass and a binding-rule check (creates a session at `xhigh`, sends a turn with `effort: "low"` in the body, proves the body is ignored):

```
level    latency    in     out    preview
------------------------------------------------------------
low         2.41s   12     128    'NO. 1234567 = 127 × 9721 ...'
medium      3.62s   12     156    'NO. To check, I look for ...'
high        7.18s   12     287    'NO. I need to check divis...'
xhigh      18.04s   12     412    'NO. Systematically: 1234567...'
max        41.22s   12     691    'NO. Let me reason about this...'
```

(Numbers are illustrative — your mileage varies with prompt and model.)

## Docs

- `EFFORT_INTEGRATION.md` — full client integration guide (Python httpx, anthropic SDK, Rust, curl)
- `AGENTIC_INTEGRATION.md` — has the field documented alongside the rest of Conduit's wire surface
- `scripts/effort_test.py` — runnable sweep

## Server config

Set a global default with `CONDUIT_DEFAULT_EFFORT=high` in your env. Defaults to unset (SDK default).

Repo: `<link>`

Same disclaimer as the other Conduit posts: local single-user, bound to `127.0.0.1`, no auth.
