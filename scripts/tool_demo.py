"""End-to-end tool-use demo against a running Conduit server.

Three demos, all using a single fake `get_weather` tool implemented in this
script:

  1. Declares the tool but asks something irrelevant — model returns plain
     text with stop_reason=end_turn (proves tools are advisory).
  2. Full round trip, non-streaming — model picks the tool, we deliver a
     result, model produces a final answer that quotes the result.
  3. Full round trip, streaming — same as 2 but with manual SSE parsing so
     you can see the wire format live (tool_use block construction, pause,
     resume, final text).

Uses httpx directly (not the anthropic SDK) because tool-use requires
session_id and x-conduit-session-id header plumbing that the SDK doesn't
know about.

Usage:
    conda run -n conduit --no-capture-output python scripts/tool_demo.py
    conda run -n conduit --no-capture-output python scripts/tool_demo.py --url http://127.0.0.1:9000
    conda run -n conduit --no-capture-output python scripts/tool_demo.py --model claude-sonnet-4-6
    conda run -n conduit --no-capture-output python scripts/tool_demo.py --skip stream
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx


# ---------------------------------------------------------------------------
# fake tool — what the agent gets when it calls get_weather(...)
# ---------------------------------------------------------------------------

WEATHER_DATA = {
    "paris": "It is 68°F and partly cloudy.",
    "tokyo": "It is 55°F with light rain.",
    "nyc":   "It is 72°F and sunny.",
    "new york": "It is 72°F and sunny.",
    "london": "It is 50°F and foggy.",
}

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get current weather for a city. Returns a short sentence.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name"},
        },
        "required": ["city"],
    },
}


def fake_get_weather(city: str) -> str:
    key = (city or "").strip().lower()
    return WEATHER_DATA.get(key, f"No data available for {city!r}.")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

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
    print(f"[demo] server OK at {url}: {r.json()}")


def extract_text(content: list) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def find_tool_use(content: list) -> dict | None:
    return next((b for b in content if b.get("type") == "tool_use"), None)


# ---------------------------------------------------------------------------
# Demo 1 — tool declared but not needed
# ---------------------------------------------------------------------------

def demo_no_tool_use(url: str, model: str) -> None:
    banner("Demo 1 — tool declared but model doesn't use it")
    payload = {
        "model": model,
        "max_tokens": 96,
        "tools": [WEATHER_TOOL],
        "messages": [{"role": "user",
                      "content": "Say only the literal word PING. Do not use any tool."}],
    }
    t0 = time.perf_counter()
    r = httpx.post(f"{url}/v1/messages", json=payload, timeout=60)
    dt = time.perf_counter() - t0
    r.raise_for_status()
    msg = r.json()

    print(f"latency:      {dt:.2f}s")
    print(f"stop_reason:  {msg['stop_reason']}")
    print(f"sid header:   {r.headers.get('x-conduit-session-id')}")
    print(f"content[]:    {[b['type'] for b in msg['content']]}")
    print(f"text:         {extract_text(msg['content'])!r}")

    if msg["stop_reason"] != "end_turn":
        print("[demo] WARN: expected end_turn — model used the tool anyway?")
    if find_tool_use(msg["content"]):
        print("[demo] WARN: unexpected tool_use block in content")


# ---------------------------------------------------------------------------
# Demo 2 — full round trip, non-streaming
# ---------------------------------------------------------------------------

def demo_round_trip_nonstream(url: str, model: str) -> None:
    banner("Demo 2 — full round trip (non-streaming)")
    user_question = "What's the weather in Paris? Use the get_weather tool exactly once."

    # --- Initial request ---
    t0 = time.perf_counter()
    r1 = httpx.post(f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 512,
        "tools": [WEATHER_TOOL],
        "messages": [{"role": "user", "content": user_question}],
    }, timeout=60)
    dt1 = time.perf_counter() - t0
    r1.raise_for_status()
    msg1 = r1.json()
    sid = r1.headers["x-conduit-session-id"]

    print(f"--- Turn 1 (initial) ---")
    print(f"latency:        {dt1:.2f}s")
    print(f"session id:     {sid}")
    print(f"stop_reason:    {msg1['stop_reason']}")

    tu = find_tool_use(msg1["content"])
    assert tu is not None, "expected a tool_use block"
    print(f"tool_use:       name={tu['name']!r} id={tu['id']!r} input={tu['input']!r}")

    pre_text = extract_text(msg1["content"])
    if pre_text:
        print(f"pre-tool text:  {pre_text!r}")

    # --- Execute tool ---
    tool_result = fake_get_weather(**tu["input"])
    print(f"tool result:    {tool_result!r}")

    # --- Resume ---
    t0 = time.perf_counter()
    r2 = httpx.post(f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 512,
        "tools": [WEATHER_TOOL],
        "session_id": sid,
        "messages": [
            {"role": "user", "content": user_question},
            {"role": "assistant", "content": msg1["content"]},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": tool_result,
            }]},
        ],
    }, timeout=60)
    dt2 = time.perf_counter() - t0
    r2.raise_for_status()
    msg2 = r2.json()

    print(f"\n--- Turn 2 (resume) ---")
    print(f"latency:        {dt2:.2f}s")
    print(f"stop_reason:    {msg2['stop_reason']}")
    print(f"final text:     {extract_text(msg2['content'])!r}")

    # Cleanup
    httpx.delete(f"{url}/v1/sessions/{sid}", timeout=10)
    print(f"\n--- Total: {dt1 + dt2:.2f}s, session deleted ---")


# ---------------------------------------------------------------------------
# Demo 3 — full round trip, streaming, manual SSE parsing
# ---------------------------------------------------------------------------

def _iter_sse(resp: httpx.Response):
    """Yield (event_name, data_dict) tuples from an SSE response."""
    event_name: str | None = None
    for line in resp.iter_lines():
        if not line:
            event_name = None
            continue
        if line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: ") and event_name:
            yield event_name, json.loads(line[6:])


def _drain_until_stop(resp: httpx.Response) -> tuple[list[dict], str | None]:
    """Reconstruct the assistant's content[] and final stop_reason from SSE."""
    blocks: dict[int, dict] = {}
    stop_reason: str | None = None
    input_buffers: dict[int, str] = {}

    for event_name, data in _iter_sse(resp):
        t = data.get("type")
        if t == "content_block_start":
            idx = data["index"]
            blocks[idx] = dict(data["content_block"])
            if blocks[idx].get("type") == "tool_use":
                input_buffers[idx] = ""
        elif t == "content_block_delta":
            idx = data["index"]
            d = data["delta"]
            if d["type"] == "text_delta":
                b = blocks[idx]
                b["text"] = b.get("text", "") + d["text"]
                # Live print of streamed text
                print(d["text"], end="", flush=True)
            elif d["type"] == "input_json_delta":
                input_buffers[idx] += d["partial_json"]
        elif t == "content_block_stop":
            idx = data["index"]
            if idx in input_buffers:
                try:
                    blocks[idx]["input"] = json.loads(input_buffers.pop(idx) or "{}")
                except json.JSONDecodeError:
                    blocks[idx]["input"] = {}
        elif t == "message_delta":
            stop_reason = (data.get("delta") or {}).get("stop_reason", stop_reason)
        elif t == "message_stop":
            break

    return [blocks[i] for i in sorted(blocks)], stop_reason


def demo_round_trip_streaming(url: str, model: str) -> None:
    banner("Demo 3 — full round trip (streaming, manual SSE parsing)")
    user_question = "What's the weather in Tokyo right now? Use get_weather."

    # --- Initial streaming request ---
    print("--- Turn 1 (initial, streaming) ---")
    print("> live tokens: ", end="", flush=True)
    t0 = time.perf_counter()
    with httpx.stream("POST", f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 512,
        "tools": [WEATHER_TOOL],
        "stream": True,
        "messages": [{"role": "user", "content": user_question}],
    }, timeout=60) as resp:
        resp.raise_for_status()
        sid = resp.headers["x-conduit-session-id"]
        content1, stop1 = _drain_until_stop(resp)
    dt1 = time.perf_counter() - t0
    print()  # newline after live tokens

    print(f"latency:        {dt1:.2f}s")
    print(f"session id:     {sid}")
    print(f"stop_reason:    {stop1}")
    print(f"content[]:      {[b['type'] for b in content1]}")

    tu = find_tool_use(content1)
    assert tu is not None, "expected a tool_use block"
    print(f"tool_use:       name={tu['name']!r} id={tu['id']!r} input={tu['input']!r}")

    # --- Execute tool ---
    tool_result = fake_get_weather(**tu["input"])
    print(f"tool result:    {tool_result!r}")

    # --- Resume, also streaming ---
    print(f"\n--- Turn 2 (resume, streaming) ---")
    print("> live tokens: ", end="", flush=True)
    t0 = time.perf_counter()
    with httpx.stream("POST", f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 512,
        "tools": [WEATHER_TOOL],
        "session_id": sid,
        "stream": True,
        "messages": [
            {"role": "user", "content": user_question},
            {"role": "assistant", "content": content1},
            {"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": tool_result,
            }]},
        ],
    }, timeout=60) as resp:
        resp.raise_for_status()
        content2, stop2 = _drain_until_stop(resp)
    dt2 = time.perf_counter() - t0
    print()

    print(f"latency:        {dt2:.2f}s")
    print(f"stop_reason:    {stop2}")

    httpx.delete(f"{url}/v1/sessions/{sid}", timeout=10)
    print(f"\n--- Total: {dt1 + dt2:.2f}s, session deleted ---")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765", help="Conduit base URL")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="Model to use (Haiku is the cheapest)")
    p.add_argument("--skip", choices=["none", "no-tool", "nonstream", "stream"],
                   default="none", help="Skip a section")
    args = p.parse_args()

    check_health(args.url)

    if args.skip != "no-tool":
        demo_no_tool_use(args.url, args.model)
    if args.skip != "nonstream":
        demo_round_trip_nonstream(args.url, args.model)
    if args.skip != "stream":
        demo_round_trip_streaming(args.url, args.model)

    banner("All demos finished")


if __name__ == "__main__":
    main()
