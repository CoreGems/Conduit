"""End-to-end inference demo against a running Conduit server.

Runs three demos:
  1. Non-streaming one-shot (Anthropic-style stateless)
  2. Streaming — tokens print as they arrive
  3. Stateful multi-turn — remember a fact, then recall it via session_id

Uses the OFFICIAL `anthropic` Python SDK pointed at Conduit, which is the
strongest possible compatibility test: if this script works, any Anthropic
SDK client works.

Usage:
    conda run -n conduit --no-capture-output python scripts/inference_demo.py
    conda run -n conduit --no-capture-output python scripts/inference_demo.py --url http://127.0.0.1:9000
    conda run -n conduit --no-capture-output python scripts/inference_demo.py --model claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx
from anthropic import Anthropic


def banner(s: str) -> None:
    print()
    print("=" * 70)
    print(s)
    print("=" * 70)


def check_health(url: str) -> None:
    try:
        r = httpx.get(f"{url}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:
        sys.exit(f"[demo] server at {url} is not responding ({e}). Start it with .\\run.ps1")
    print(f"[demo] server OK at {url}: {r.json()}")


def demo_non_streaming(client: Anthropic, model: str) -> None:
    banner("Demo 1 — non-streaming one-shot")
    t0 = time.perf_counter()
    msg = client.messages.create(
        model=model,
        max_tokens=128,
        messages=[{"role": "user", "content": "Say hello and tell me one tiny weird fact about octopuses, in 2 sentences."}],
    )
    dt = time.perf_counter() - t0
    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    print(f"id={msg.id}")
    print(f"model={msg.model}")
    print(f"stop_reason={msg.stop_reason}")
    print(f"usage={msg.usage}")
    print(f"latency={dt:.2f}s")
    print()
    print(text)


def demo_streaming(client: Anthropic, model: str) -> None:
    banner("Demo 2 — streaming (watch tokens arrive)")
    t0 = time.perf_counter()
    first_token_at: float | None = None
    chunks: list[str] = []
    with client.messages.stream(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": "Count from 1 to 10, one number per line, no other text."}],
    ) as stream:
        for text in stream.text_stream:
            if first_token_at is None:
                first_token_at = time.perf_counter()
            chunks.append(text)
            print(text, end="", flush=True)
        final = stream.get_final_message()
    dt = time.perf_counter() - t0
    ttft = (first_token_at - t0) if first_token_at else float("nan")
    print()
    print()
    print(f"chars={sum(len(c) for c in chunks)} chunks={len(chunks)} ttft={ttft:.2f}s total={dt:.2f}s")
    print(f"stop_reason={final.stop_reason} usage={final.usage}")


def demo_stateful_session(url: str, client: Anthropic, model: str) -> None:
    banner("Demo 3 — stateful session (multi-turn memory)")
    # Create session
    r = httpx.post(f"{url}/v1/sessions", json={"model": model}, timeout=30)
    r.raise_for_status()
    sid = r.json()["session_id"]
    print(f"created session_id={sid}")

    try:
        # Turn 1
        print("\n--- Turn 1 ---")
        m1 = client.messages.create(
            model=model,
            max_tokens=64,
            extra_body={"session_id": sid},
            messages=[{"role": "user", "content": "Please remember the secret word 'porcupine'. Just acknowledge."}],
        )
        print("user> Please remember the secret word 'porcupine'. Just acknowledge.")
        print(f"asst> {''.join(b.text for b in m1.content if hasattr(b, 'text'))}")

        # Turn 2
        print("\n--- Turn 2 ---")
        m2 = client.messages.create(
            model=model,
            max_tokens=64,
            extra_body={"session_id": sid},
            messages=[{"role": "user", "content": "What was the secret word I just told you?"}],
        )
        text2 = "".join(b.text for b in m2.content if hasattr(b, "text"))
        print("user> What was the secret word I just told you?")
        print(f"asst> {text2}")
        memory_ok = "porcupine" in text2.lower()
        print(f"\nmemory check: {'PASS' if memory_ok else 'FAIL'} (looked for 'porcupine' in reply)")

        # Show session metadata
        print("\n--- Session metadata ---")
        r = httpx.get(f"{url}/v1/sessions", timeout=10)
        for s in r.json()["sessions"]:
            if s["session_id"] == sid:
                print(s)
    finally:
        # Cleanup
        r = httpx.delete(f"{url}/v1/sessions/{sid}", timeout=10)
        print(f"\ndeleted session: {r.json()}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765", help="Conduit base URL")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="Model to use (Haiku is cheapest for testing)")
    p.add_argument("--skip", choices=["none", "stream", "session"], default="none",
                   help="Skip a section if you only want to run some demos")
    args = p.parse_args()

    check_health(args.url)
    client = Anthropic(base_url=args.url, api_key="not-used")

    demo_non_streaming(client, args.model)
    if args.skip != "stream":
        demo_streaming(client, args.model)
    if args.skip != "session":
        demo_stateful_session(args.url, client, args.model)

    banner("All demos finished")


if __name__ == "__main__":
    main()
