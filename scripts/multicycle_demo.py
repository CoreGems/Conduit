"""End-to-end demo of multi-cycle custom-tool use.

Reproduces the realistic agentic pattern: model calls a sequence of tools
within one user turn (list_topics → topic_coverage → final answer), with
each pause/resume going through Conduit using the same session_id.

Usage:
    conda run -n conduit --no-capture-output python scripts/multicycle_demo.py
    conda run -n conduit --no-capture-output python scripts/multicycle_demo.py --model claude-sonnet-4-6
    conda run -n conduit --no-capture-output python scripts/multicycle_demo.py --question "How am I doing on data loading?"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx


TOOLS = [
    {
        "name": "list_topics",
        "description": "List all study topics. No input.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "topic_coverage",
        "description": "Get coverage stats for a specific study topic.",
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "Topic name"}},
            "required": ["topic"],
        },
    },
    {
        "name": "list_recent_questions",
        "description": "List the last N questions the student answered for a topic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["topic"],
        },
    },
]

SYSTEM = (
    "You are a study assistant. Before answering any student question, "
    "always use the available tools to ground your answer: "
    "(1) call list_topics to see what's available, "
    "(2) call topic_coverage on the most relevant topic, "
    "(3) optionally call list_recent_questions for context, "
    "then write a short helpful answer based on the data. "
    "Do not answer before you have the coverage data."
)


def fake_tool(name: str, inp: dict) -> str:
    if name == "list_topics":
        return json.dumps(["snowflake-basics", "warehouses", "data-loading", "security"])
    if name == "topic_coverage":
        return json.dumps({
            "topic": inp.get("topic"),
            "questions_answered": 14,
            "questions_correct": 11,
            "accuracy": 0.79,
            "last_studied": "2026-05-20",
        })
    if name == "list_recent_questions":
        topic = inp.get("topic", "unknown")
        return json.dumps([
            {"q": f"What is a {topic} cluster?", "correct": True},
            {"q": f"How does {topic} scale auto?", "correct": False},
            {"q": f"What's the cost model for {topic}?", "correct": True},
        ])
    return "{}"


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


def text_of(content: list) -> str:
    return "".join(b.get("text", "") for b in content if b["type"] == "text")


def print_cycle(label: str, msg: dict, sid: str | None, dt: float) -> None:
    print(f"\n--- {label}  ({dt:.2f}s) ---")
    print(f"  sid:         {sid}")
    print(f"  stop_reason: {msg['stop_reason']!r}")
    print(f"  blocks:      {[b['type'] for b in msg['content']]}")
    for b in msg["content"]:
        if b["type"] == "tool_use":
            print(f"    → tool_use  name={b['name']!r}  id={b['id']!r}")
            print(f"               input={json.dumps(b['input'])}")
        elif b["type"] == "text":
            t = b["text"]
            print(f"    → text ({len(t)} chars):")
            for line in t.splitlines():
                print(f"        {line}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--model", default=os.environ.get("CONDUIT_MODEL", "claude-haiku-4-5-20251001"),
                   help="Model to use. Defaults to $CONDUIT_MODEL or Haiku (cheapest).")
    p.add_argument("--question", default="How am I doing on warehouses? Be specific.")
    p.add_argument("--max-cycles", type=int, default=6)
    args = p.parse_args()

    check_health(args.url)

    banner(f"Multi-cycle tool-use demo (model={args.model})")
    print(f"user question: {args.question!r}")

    history = [{"role": "user", "content": args.question}]
    sid: str | None = None
    cycle = 0
    total_dt = 0.0

    # Initial request
    t0 = time.perf_counter()
    r = httpx.post(f"{args.url}/v1/messages", json={
        "model": args.model,
        "max_tokens": 600,
        "system": SYSTEM,
        "tools": TOOLS,
        "messages": history,
    }, timeout=180)
    dt = time.perf_counter() - t0
    total_dt += dt
    r.raise_for_status()
    sid = r.headers["x-conduit-session-id"]
    msg = r.json()
    print_cycle(f"Cycle {cycle} (initial)", msg, sid, dt)

    while msg["stop_reason"] == "tool_use":
        cycle += 1
        if cycle > args.max_cycles:
            print(f"\n[abort] max_cycles ({args.max_cycles}) reached")
            break

        tu = next(b for b in msg["content"] if b["type"] == "tool_use")
        result = fake_tool(tu["name"], tu["input"])
        print(f"    [client] executed {tu['name']} → {result[:100]!r}")

        history.append({"role": "assistant", "content": msg["content"]})
        history.append({"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": tu["id"],
            "content": result,
        }]})

        t0 = time.perf_counter()
        r = httpx.post(f"{args.url}/v1/messages", json={
            "model": args.model,
            "max_tokens": 600,
            "tools": TOOLS,
            "session_id": sid,
            "messages": history,
        }, timeout=180)
        dt = time.perf_counter() - t0
        total_dt += dt
        r.raise_for_status()
        msg = r.json()
        print_cycle(f"Cycle {cycle} (resume)", msg, r.headers.get("x-conduit-session-id"), dt)

    banner(f"Done — {cycle + 1} cycles, total {total_dt:.2f}s, final stop={msg['stop_reason']!r}")

    # Cleanup
    httpx.delete(f"{args.url}/v1/sessions/{sid}", timeout=10)
    print(f"[demo] deleted session {sid}")


if __name__ == "__main__":
    main()
