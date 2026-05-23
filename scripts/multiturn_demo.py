"""Multi-turn chat patterns.

Walks through four scenarios so you can see exactly what works and what
doesn't:

  1. BROKEN — client sends only the new user message and no session_id.
              Each request is a fresh session that sees only that turn;
              no history → no memory.
  2. Pattern A (stateless, RECOMMENDED for Anthropic drop-in) — client
              appends each assistant reply to its local history and sends
              the full messages[] every turn, no session_id. Conduit
              replays the conversation into the prompt server-side.
  3. Pattern B (stateful, RECOMMENDED for Conduit-native) — client reuses
              the session_id from turn 1's x-conduit-session-id header
              on every subsequent turn, sending ONLY the new user message.
              Server maintains the conversation state; fewer tokens per
              request, faster.
  4. REDUNDANT but harmless — uses session_id AND also resends full
              history. Server reads only the last user message from
              each request, so the redundant history is ignored but
              consistent (the SDK's own conversation state matches what
              the client got back, so no contradiction).

Each scenario runs the same 3-turn conversation:
  Turn 1: pick a random European capital
  Turn 2: what country is that in?
  Turn 3: what's its population, roughly?

If a scenario is working, turns 2 and 3 reference the city/country from
turn 1. If broken, the model invents new ones each turn.

Usage:
    conda run -n conduit --no-capture-output python scripts/multiturn_demo.py
    conda run -n conduit --no-capture-output python scripts/multiturn_demo.py --skip broken
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx


TURNS = [
    "Pick a single random European capital. Just the name on one line, nothing else.",
    "Good. What country is that city in? One short sentence.",
    "What's its approximate population? One short sentence.",
]


def banner(s: str) -> None:
    print()
    print("=" * 72)
    print(s)
    print("=" * 72)


def check_health(url: str) -> None:
    try:
        r = httpx.get(f"{url}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:
        sys.exit(f"[demo] server at {url} not responding ({e}). Start it with .\\run.ps1")
    print(f"[demo] server OK at {url}")


def extract_text(content: list) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def post(url: str, payload: dict, timeout: float = 60):
    t0 = time.perf_counter()
    r = httpx.post(f"{url}/v1/messages", json=payload, timeout=timeout)
    dt = time.perf_counter() - t0
    r.raise_for_status()
    return r, r.json(), dt


def run_turn(label: str, user_text: str, answer: str, dt: float) -> None:
    print(f"\n  Turn — {label}")
    print(f"  user: {user_text}")
    print(f"  asst ({dt:.2f}s): {answer.strip()}")


# ---------------------------------------------------------------------------
# Scenario 1 — broken: no session_id, only the new user message each turn
# ---------------------------------------------------------------------------

def demo_broken(url: str, model: str) -> None:
    banner("Scenario 1 — BROKEN: no session_id, only new user message")
    print("Each request is an ephemeral session. Server has no memory of prior turns.")
    print("Expect: turns 2-3 produce nonsense — model can't reference turn 1.")

    for i, q in enumerate(TURNS, 1):
        _, msg, dt = post(url, {
            "model": model,
            "max_tokens": 80,
            "messages": [{"role": "user", "content": q}],     # only new turn, no history
            # NO session_id
        })
        run_turn(f"{i}/3", q, extract_text(msg["content"]), dt)


# ---------------------------------------------------------------------------
# Scenario 2 — Pattern A: stateless, client owns history
# ---------------------------------------------------------------------------

def demo_stateless(url: str, model: str) -> None:
    banner("Scenario 2 — Pattern A (stateless): client sends full history each turn")
    print("No session_id. Client appends assistant replies to history before next turn.")
    print("Expect: turns 2-3 correctly reference the city from turn 1.")

    history: list[dict] = []
    for i, q in enumerate(TURNS, 1):
        history.append({"role": "user", "content": q})
        _, msg, dt = post(url, {
            "model": model,
            "max_tokens": 80,
            "messages": history,                              # full history
            # NO session_id
        })
        answer = extract_text(msg["content"])
        run_turn(f"{i}/3", q, answer, dt)
        history.append({"role": "assistant", "content": answer})


# ---------------------------------------------------------------------------
# Scenario 3 — Pattern B: stateful, server owns history
# ---------------------------------------------------------------------------

def demo_stateful(url: str, model: str) -> None:
    banner("Scenario 3 — Pattern B (stateful): reuse session_id, send only new turn")
    print("Server remembers history. Client sends just the new user message each turn.")
    print("Expect: turns 2-3 correctly reference the city from turn 1.")

    sid: str | None = None
    for i, q in enumerate(TURNS, 1):
        body = {
            "model": model,
            "max_tokens": 80,
            "messages": [{"role": "user", "content": q}],     # only new turn
        }
        if sid is not None:
            body["session_id"] = sid                          # reuse from turn 1
        r, msg, dt = post(url, body)
        if sid is None:
            sid = r.headers.get("x-conduit-session-id")
            print(f"  captured session_id from turn 1 header: {sid}")
        run_turn(f"{i}/3", q, extract_text(msg["content"]), dt)

    if sid:
        httpx.delete(f"{url}/v1/sessions/{sid}", timeout=10)


# ---------------------------------------------------------------------------
# Scenario 4 — Anti-pattern: session_id + duplicate full history
# ---------------------------------------------------------------------------

def demo_antipattern(url: str, model: str) -> None:
    banner("Scenario 4 — REDUNDANT but harmless: session_id AND full history")
    print("In stateful mode Conduit reads only the most-recent user message,")
    print("so the redundant history is ignored. Works (since SDK has its own")
    print("session state matching what you got back) but wastes bandwidth.")

    sid: str | None = None
    history: list[dict] = []
    for i, q in enumerate(TURNS, 1):
        history.append({"role": "user", "content": q})
        body = {
            "model": model,
            "max_tokens": 80,
            "messages": history,                              # full history (WRONG with sid)
        }
        if sid is not None:
            body["session_id"] = sid
        r, msg, dt = post(url, body)
        if sid is None:
            sid = r.headers.get("x-conduit-session-id")
        answer = extract_text(msg["content"])
        run_turn(f"{i}/3", q, answer, dt)
        history.append({"role": "assistant", "content": answer})

    if sid:
        httpx.delete(f"{url}/v1/sessions/{sid}", timeout=10)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765", help="Conduit base URL")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="Model (Haiku is the cheapest)")
    p.add_argument("--skip", action="append", default=[],
                   choices=["broken", "stateless", "stateful", "antipattern"],
                   help="Skip a scenario (repeatable)")
    args = p.parse_args()

    check_health(args.url)

    if "broken" not in args.skip:
        demo_broken(args.url, args.model)
    if "stateless" not in args.skip:
        demo_stateless(args.url, args.model)
    if "stateful" not in args.skip:
        demo_stateful(args.url, args.model)
    if "antipattern" not in args.skip:
        demo_antipattern(args.url, args.model)

    banner("Done")
    print("Working scenarios: Patterns A (stateless), B (stateful), and the")
    print("                    redundant variant (4) — all maintain context.")
    print("Broken scenario:   only Scenario 1 (no session_id, no history).")


if __name__ == "__main__":
    main()
