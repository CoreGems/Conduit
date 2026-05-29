"""Test that thinking pass-through works end-to-end.

Asks the model a small reasoning question that reliably produces thinking,
prints the thinking trace alongside the final answer so you can verify the
`include_thinking` flag is wired correctly through your client → Conduit
→ Agent SDK → model.

Usage:
    conda run -n conduit --no-capture-output python scripts/thinking_demo.py
    conda run -n conduit --no-capture-output python scripts/thinking_demo.py --model claude-opus-4-8 --effort xhigh
    conda run -n conduit --no-capture-output python scripts/thinking_demo.py --prompt "your custom question"

Model defaults to $CONDUIT_MODEL if set, else Haiku. A more capable model with
--effort high/xhigh produces richer thinking traces.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx


DEFAULT_PROMPT = "What's the prime factorization of 2024? Show no work, just the answer."


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


def ask(url: str, model: str, effort: str | None, prompt: str, include_thinking: bool) -> dict:
    body = {
        "model": model,
        "max_tokens": 800,
        "include_thinking": include_thinking,
        "messages": [{"role": "user", "content": prompt}],
    }
    if effort:
        body["effort"] = effort
    t0 = time.perf_counter()
    r = httpx.post(f"{url}/v1/messages", json=body, timeout=180)
    dt = time.perf_counter() - t0
    r.raise_for_status()
    return r.json(), dt


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765", help="Conduit base URL")
    p.add_argument("--model", default=os.environ.get("CONDUIT_MODEL", "claude-haiku-4-5-20251001"),
                   help="Model to use. Defaults to $CONDUIT_MODEL or Haiku. Try a more "
                        "capable model with --effort xhigh for richer thinking.")
    p.add_argument("--effort", default=None,
                   choices=[None, "low", "medium", "high", "xhigh", "max"],
                   help="Thinking budget level (controls how much the model thinks)")
    p.add_argument("--prompt", default=DEFAULT_PROMPT,
                   help="The user prompt (default is a small factorization puzzle)")
    p.add_argument("--no-baseline", action="store_true",
                   help="Skip the include_thinking=false baseline call")
    args = p.parse_args()

    check_health(args.url)

    print(f"\nprompt:  {args.prompt!r}")
    print(f"model:   {args.model}")
    print(f"effort:  {args.effort or '(default)'}")

    # --- Baseline (no thinking) ---
    if not args.no_baseline:
        banner("Baseline: include_thinking = false")
        msg, dt = ask(args.url, args.model, args.effort, args.prompt, include_thinking=False)
        print(f"latency:     {dt:.2f}s")
        print(f"stop_reason: {msg['stop_reason']}")
        print(f"blocks:      {[b['type'] for b in msg['content']]}")
        print(f"answer:      {next((b['text'] for b in msg['content'] if b['type'] == 'text'), '')!r}")

    # --- With thinking ---
    banner("With thinking: include_thinking = true")
    msg, dt = ask(args.url, args.model, args.effort, args.prompt, include_thinking=True)
    print(f"latency:     {dt:.2f}s")
    print(f"stop_reason: {msg['stop_reason']}")
    print(f"blocks:      {[b['type'] for b in msg['content']]}")
    print(f"usage:       {msg.get('usage', {})}")

    thinking_blocks = [b for b in msg["content"] if b["type"] == "thinking"]
    text_blocks     = [b for b in msg["content"] if b["type"] == "text"]

    if not thinking_blocks:
        print("\n  [WARN] no thinking blocks in response.")
        print("  Possible causes:")
        print("    - server isn't running today's code (.\\run.ps1 again to be sure)")
        print("    - model didn't think for this prompt (try --effort high/xhigh)")
        print("    - the model genuinely had nothing to think about")
        return

    print()
    for i, tb in enumerate(thinking_blocks, 1):
        thinking_text = tb.get("thinking", "")
        signature = tb.get("signature", "")
        print(f"--- 💭 Thinking block #{i} ({len(thinking_text)} chars, sig={len(signature)} chars) ---")
        print(thinking_text)
        print()

    print("--- 💬 Final answer ---")
    for tb in text_blocks:
        print(tb.get("text", ""))


if __name__ == "__main__":
    main()
