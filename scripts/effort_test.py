"""Exercise Conduit's `effort` field end-to-end against a running server.

Four demos:

  1. Latency sweep — same prompt at each effort level, prints latency + token
     counts so you can see the budget effect.
  2. Validation — sending a bogus value returns 422.
  3. Binding — creating a stateful session at `xhigh`, then sending a turn
     with `effort: "low"` in the body, proves the session ignores it.
  4. Official Anthropic SDK — passing `effort` via `extra_body`.

A "reasoning-heavy" prompt is used so higher effort levels produce visibly
different latency. (Don't expect dramatic correctness differences on a toy
prompt — the budget is about reasoning headroom, not answer quality on
trivial problems.)

Usage:
    conda run -n conduit --no-capture-output python scripts/effort_test.py
    conda run -n conduit --no-capture-output python scripts/effort_test.py --url http://127.0.0.1:9000
    conda run -n conduit --no-capture-output python scripts/effort_test.py --model claude-sonnet-4-6
    conda run -n conduit --no-capture-output python scripts/effort_test.py --skip sweep
    conda run -n conduit --no-capture-output python scripts/effort_test.py --levels low,high   # subset
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence

import httpx


ALL_LEVELS = ("low", "medium", "high", "xhigh", "max")

# A prompt that benefits from thinking budget. The model has to reason about
# divisibility — higher effort tends to spend more thinking tokens.
REASONING_PROMPT = (
    "Without calculating directly, decide whether 1,234,567 is prime. "
    "Walk through your reasoning briefly, then state YES or NO on the final line."
)


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
        sys.exit(f"[demo] server at {url} is not responding ({e}). Start it with .\\run.ps1")
    print(f"[demo] server OK at {url}")


def _post_message(url: str, payload: dict, timeout: float = 180) -> tuple[dict, float]:
    t0 = time.perf_counter()
    r = httpx.post(f"{url}/v1/messages", json=payload, timeout=timeout)
    dt = time.perf_counter() - t0
    r.raise_for_status()
    return r.json(), dt


def _text(msg: dict) -> str:
    return "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")


# ---------------------------------------------------------------------------
# Demo 1 — latency sweep
# ---------------------------------------------------------------------------

def demo_latency_sweep(url: str, model: str, levels: Sequence[str]) -> None:
    banner(f"Demo 1 — latency sweep across effort levels ({', '.join(levels)})")
    print(f"prompt: {REASONING_PROMPT}\n")
    print(f"{'level':<8} {'latency':<10} {'in':<6} {'out':<6} preview")
    print("-" * 72)

    for level in levels:
        try:
            msg, dt = _post_message(url, {
                "model": model,
                "max_tokens": 384,
                "effort": level,
                "messages": [{"role": "user", "content": REASONING_PROMPT}],
            })
        except httpx.HTTPStatusError as e:
            print(f"{level:<8} ERROR {e.response.status_code}: {e.response.text[:80]}")
            continue
        u = msg.get("usage") or {}
        preview = _text(msg).replace("\n", " ")[:60]
        print(f"{level:<8} {dt:7.2f}s   {u.get('input_tokens',0):<6} {u.get('output_tokens',0):<6} {preview!r}")


# ---------------------------------------------------------------------------
# Demo 2 — validation rejection
# ---------------------------------------------------------------------------

def demo_validation(url: str, model: str) -> None:
    banner("Demo 2 — invalid effort value rejected with 422")
    bad_values = ["extreme", "HIGH", "", "0", "ultra"]
    for v in bad_values:
        r = httpx.post(f"{url}/v1/messages", json={
            "model": model,
            "max_tokens": 16,
            "effort": v,
            "messages": [{"role": "user", "content": "ping"}],
        }, timeout=10)
        ok = r.status_code == 422
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] effort={v!r:<10} -> {r.status_code}")


# ---------------------------------------------------------------------------
# Demo 3 — binding: stateful session locks effort at creation
# ---------------------------------------------------------------------------

def demo_binding(url: str, model: str) -> None:
    banner("Demo 3 — stateful session binds effort at creation")
    # Create one session at xhigh, one at low. Send the same prompt through each.
    levels = ("xhigh", "low")
    timings: dict[str, float] = {}

    for level in levels:
        sid = httpx.post(f"{url}/v1/sessions", json={
            "model": model,
            "effort": level,
        }, timeout=10).json()["session_id"]
        print(f"  created session at effort={level!r}: {sid}")

        # Send a turn with effort='medium' in the body — it should be ignored.
        try:
            _, dt = _post_message(url, {
                "model": model,
                "max_tokens": 384,
                "effort": "medium",       # ← intentionally different; should be ignored
                "session_id": sid,
                "messages": [{"role": "user", "content": REASONING_PROMPT}],
            })
            timings[level] = dt
            print(f"    turn ran in {dt:.2f}s (sent effort=medium, session locked at {level})")
        finally:
            httpx.delete(f"{url}/v1/sessions/{sid}", timeout=10)

    if "xhigh" in timings and "low" in timings:
        diff = timings["xhigh"] - timings["low"]
        verdict = "PASS — xhigh slower as expected" if diff > 0.5 else "INCONCLUSIVE — too close to call"
        print(f"\n  {verdict}: xhigh={timings['xhigh']:.2f}s  low={timings['low']:.2f}s  Δ={diff:+.2f}s")
        print("  (If xhigh were ignored in favor of body's effort=medium, both would be similar.)")


# ---------------------------------------------------------------------------
# Demo 4 — anthropic SDK via extra_body
# ---------------------------------------------------------------------------

def demo_anthropic_sdk(url: str, model: str) -> None:
    banner("Demo 4 — official Anthropic SDK via extra_body")
    try:
        from anthropic import Anthropic
    except ImportError:
        print("  anthropic package not installed — skipping")
        return

    client = Anthropic(base_url=url, api_key="not-used")
    t0 = time.perf_counter()
    msg = client.messages.create(
        model=model,
        max_tokens=128,
        extra_body={"effort": "high"},
        messages=[{"role": "user", "content": "Reply with: SDK OK"}],
    )
    dt = time.perf_counter() - t0
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    print(f"  latency:     {dt:.2f}s")
    print(f"  stop_reason: {msg.stop_reason}")
    print(f"  text:        {text!r}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765", help="Conduit base URL")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="Model to use (Haiku is the cheapest for testing)")
    p.add_argument("--levels", default=",".join(ALL_LEVELS),
                   help="Comma-separated effort levels for the sweep (default: all)")
    p.add_argument("--skip", action="append", default=[],
                   choices=["sweep", "validation", "binding", "sdk"],
                   help="Skip a demo (repeatable)")
    args = p.parse_args()

    levels = [x.strip() for x in args.levels.split(",") if x.strip()]
    bad = [x for x in levels if x not in ALL_LEVELS]
    if bad:
        sys.exit(f"unknown effort levels: {bad}. Valid: {ALL_LEVELS}")

    check_health(args.url)

    if "sweep" not in args.skip:
        demo_latency_sweep(args.url, args.model, levels)
    if "validation" not in args.skip:
        demo_validation(args.url, args.model)
    if "binding" not in args.skip:
        demo_binding(args.url, args.model)
    if "sdk" not in args.skip:
        demo_anthropic_sdk(args.url, args.model)

    banner("All demos finished")


if __name__ == "__main__":
    main()
