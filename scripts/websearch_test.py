"""Exercise Conduit's hosted SDK tools (WebSearch / WebFetch) end-to-end.

Four demos, all using the Anthropic-style hosted-tool pattern — client declares
the tool in `tools[]`, server runs it via the Agent SDK, response is plain
text with citations inline. No client-side execution, no pause/resume, no
session_id plumbing required.

  1. WebSearch — ask a current-events question that requires fresh info.
  2. WebFetch  — fetch a specific URL and summarise it.
  3. Both hosted tools enabled — model picks which to use.
  4. Mixed hosted + custom tool — proves the pause logic still works for
     custom tools while hidden hosted execution stays invisible.

Usage:
    conda run -n conduit --no-capture-output python scripts/websearch_test.py
    conda run -n conduit --no-capture-output python scripts/websearch_test.py --url http://127.0.0.1:9000
    conda run -n conduit --no-capture-output python scripts/websearch_test.py --model claude-sonnet-4-6
    conda run -n conduit --no-capture-output python scripts/websearch_test.py --skip mixed
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx


WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search"}
WEB_FETCH  = {"type": "web_fetch_20250910",  "name": "web_fetch"}

# A custom client-defined tool for the "mixed" demo.
WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


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
    print(f"[demo] server OK at {url}")


def extract_text(content: list) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def block_types(content: list) -> list[str]:
    return [b.get("type", "?") for b in content]


def print_result(label: str, dt: float, msg: dict, *, sid: str | None) -> None:
    print(f"{label}")
    print(f"  latency:      {dt:.2f}s")
    print(f"  stop_reason:  {msg.get('stop_reason')!r}")
    print(f"  content[]:    {block_types(msg.get('content', []))}")
    if sid is not None:
        print(f"  x-conduit-session-id: {sid}")
    text = extract_text(msg.get("content", []))
    if text:
        preview = text if len(text) <= 400 else text[:400] + " ..."
        print(f"  text:\n    " + preview.replace("\n", "\n    "))


def post(url: str, payload: dict, timeout: float = 180):
    t0 = time.perf_counter()
    r = httpx.post(f"{url}/v1/messages", json=payload, timeout=timeout)
    dt = time.perf_counter() - t0
    r.raise_for_status()
    return r, r.json(), dt


# ---------------------------------------------------------------------------
# Demo 1 — WebSearch
# ---------------------------------------------------------------------------

def demo_web_search(url: str, model: str) -> None:
    banner("Demo 1 — WebSearch (current-events question)")
    r, msg, dt = post(url, {
        "model": model,
        "max_tokens": 400,
        "tools": [WEB_SEARCH],
        "messages": [{"role": "user",
                      "content": "What is the most recent stable release of FastAPI? "
                                 "Use web_search, then state just the version on its own line "
                                 "and cite the source URL."}],
    })
    print_result("response:", dt, msg, sid=r.headers.get("x-conduit-session-id"))

    # Sanity: no client-visible tool_use blocks should leak through.
    if "tool_use" in block_types(msg.get("content", [])):
        print("  [WARN] hosted tool_use leaked to client — filter bug")
    if msg.get("stop_reason") != "end_turn":
        print(f"  [WARN] expected end_turn, got {msg.get('stop_reason')!r}")


# ---------------------------------------------------------------------------
# Demo 2 — WebFetch
# ---------------------------------------------------------------------------

def demo_web_fetch(url: str, model: str) -> None:
    banner("Demo 2 — WebFetch (specific URL)")
    target = "https://docs.anthropic.com/en/api/overview"
    r, msg, dt = post(url, {
        "model": model,
        "max_tokens": 400,
        "tools": [WEB_FETCH],
        "messages": [{"role": "user",
                      "content": f"Fetch {target} and tell me in one sentence "
                                 "what kind of page it is."}],
    })
    print_result("response:", dt, msg, sid=r.headers.get("x-conduit-session-id"))


# ---------------------------------------------------------------------------
# Demo 3 — both hosted tools
# ---------------------------------------------------------------------------

def demo_both(url: str, model: str) -> None:
    banner("Demo 3 — both hosted tools, model picks")
    r, msg, dt = post(url, {
        "model": model,
        "max_tokens": 400,
        "tools": [WEB_SEARCH, WEB_FETCH],
        "messages": [{"role": "user",
                      "content": "When did Python 3.13 release? Use whichever tool fits best."}],
    })
    print_result("response:", dt, msg, sid=r.headers.get("x-conduit-session-id"))


# ---------------------------------------------------------------------------
# Demo 4 — mixed hosted + custom
# ---------------------------------------------------------------------------

def demo_mixed(url: str, model: str) -> None:
    banner("Demo 4 — hosted + custom tools (pause only for custom)")
    print("declaring: WebSearch (hosted) AND get_weather (custom)")
    print("prompting model to use the custom weather tool, NOT search the web.\n")

    r1, msg1, dt1 = post(url, {
        "model": model,
        "max_tokens": 400,
        "tools": [WEB_SEARCH, WEATHER_TOOL],
        "messages": [{"role": "user",
                      "content": "What's the weather in Paris? "
                                 "Use the get_weather tool, do not search the web."}],
    })
    sid = r1.headers.get("x-conduit-session-id")
    print_result("turn 1:", dt1, msg1, sid=sid)

    stop = msg1.get("stop_reason")
    if stop == "tool_use":
        # Custom tool fired — handle the pause.
        tu = next((b for b in msg1["content"] if b["type"] == "tool_use"), None)
        if tu and tu.get("name") == "get_weather":
            print(f"\n  → pausing for custom tool {tu['name']!r} (id={tu['id']})")
            r2, msg2, dt2 = post(url, {
                "model": model,
                "max_tokens": 400,
                "tools": [WEB_SEARCH, WEATHER_TOOL],
                "session_id": sid,
                "messages": [
                    {"role": "user", "content": "..."},
                    {"role": "assistant", "content": msg1["content"]},
                    {"role": "user", "content": [{
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": "It's 68°F and partly cloudy in Paris.",
                    }]},
                ],
            })
            print()
            print_result("turn 2 (after tool_result):", dt2, msg2, sid=r2.headers.get("x-conduit-session-id"))
        else:
            print(f"  [WARN] stop_reason=tool_use but tool wasn't get_weather: {tu}")
            httpx.delete(f"{url}/v1/sessions/{sid}", timeout=10)
    else:
        # Model answered without the tool — fine.
        print("  (model didn't use the tool — try a different prompt for the mixed pause demo)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765", help="Conduit base URL")
    p.add_argument("--model", default="claude-haiku-4-5-20251001",
                   help="Model to use (Haiku is the cheapest)")
    p.add_argument("--skip", action="append", default=[],
                   choices=["search", "fetch", "both", "mixed"],
                   help="Skip a demo (repeatable)")
    args = p.parse_args()

    check_health(args.url)

    if "search" not in args.skip:
        demo_web_search(args.url, args.model)
    if "fetch" not in args.skip:
        demo_web_fetch(args.url, args.model)
    if "both" not in args.skip:
        demo_both(args.url, args.model)
    if "mixed" not in args.skip:
        demo_mixed(args.url, args.model)

    banner("All demos finished")


if __name__ == "__main__":
    main()
