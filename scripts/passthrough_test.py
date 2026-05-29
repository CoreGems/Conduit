"""End-to-end demo of Conduit's hosted-tool visibility pass-through.

Verifies and shows the content[] blocks now exposed for WebSearch / WebFetch:

  1. Single web_search turn — print the full block breakdown.
  2. Extract URLs from `web_search_tool_result.content`.
  3. WebFetch — fetch a specific URL, show the result block contents.
  4. Streaming version of (1) with manual SSE parsing — proves the wire shape
     described in PASSTHROUGH_INTEGRATION.md §5.

Usage:
    conda run -n conduit --no-capture-output python scripts/passthrough_test.py
    conda run -n conduit --no-capture-output python scripts/passthrough_test.py --url http://127.0.0.1:9000
    conda run -n conduit --no-capture-output python scripts/passthrough_test.py --model claude-sonnet-4-6
    conda run -n conduit --no-capture-output python scripts/passthrough_test.py --skip stream
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import httpx


WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search"}
WEB_FETCH  = {"type": "web_fetch_20250910",  "name": "web_fetch"}


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
        sys.exit(f"[demo] server at {url} not responding ({e}). Start it with .\\run.ps1")
    print(f"[demo] server OK at {url}")


def extract_links(content: str) -> list[dict]:
    """Pull the `Links: [...]` JSON array out of a web_search_tool_result.content string."""
    m = re.search(r"Links: (\[.*?\])\n", content or "", re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return []


def format_block(b: dict) -> str:
    t = b.get("type", "?")
    if t == "server_tool_use":
        return (f"  server_tool_use  id={b.get('id')!r}  name={b.get('name')!r}\n"
                f"                   input={b.get('input')!r}")
    if t == "web_search_tool_result":
        links = extract_links(b.get("content", ""))
        head = b.get("content", "")[:120].replace("\n", " ")
        return (f"  web_search_tool_result  tool_use_id={b.get('tool_use_id')!r}\n"
                f"                          content[:120]={head!r}\n"
                f"                          extracted_links={len(links)}")
    if t == "web_fetch_tool_result":
        head = b.get("content", "")[:120].replace("\n", " ")
        return (f"  web_fetch_tool_result  tool_use_id={b.get('tool_use_id')!r}\n"
                f"                         content[:120]={head!r}")
    if t == "text":
        head = b.get("text", "")[:200].replace("\n", " ")
        return f"  text             {head!r}"
    if t == "thinking":
        return f"  thinking         (filtered upstream, shouldn't appear)"
    return f"  {t}  {b!r}"


def print_blocks(content: list[dict]) -> None:
    for i, b in enumerate(content):
        print(f"[{i}]")
        print(format_block(b))


# ---------------------------------------------------------------------------
# Demo 1 — full visibility on a single web_search turn
# ---------------------------------------------------------------------------

def demo_websearch_visibility(url: str, model: str) -> None:
    banner("Demo 1 — WebSearch visibility (one search turn)")
    t0 = time.perf_counter()
    r = httpx.post(f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 400,
        "tools": [WEB_SEARCH],
        "messages": [{"role": "user",
                      "content": "What is the most recent stable Python release? "
                                 "Use web_search, then answer in one short sentence."}],
    }, timeout=180)
    dt = time.perf_counter() - t0
    r.raise_for_status()
    msg = r.json()

    print(f"latency:      {dt:.2f}s")
    print(f"stop_reason:  {msg['stop_reason']!r}")
    print(f"# of blocks:  {len(msg['content'])}")
    print(f"block types:  {[b['type'] for b in msg['content']]}")
    print()
    print_blocks(msg["content"])

    # Verify the contract
    sts = [b for b in msg["content"] if b["type"] == "server_tool_use"]
    wsrs = [b for b in msg["content"] if b["type"] == "web_search_tool_result"]
    if not sts:
        print("\n  [WARN] no server_tool_use block — was the tool actually used?")
    if sts and wsrs:
        if wsrs[0]["tool_use_id"] != sts[0]["id"]:
            print(f"\n  [WARN] tool_use_id correlation broken: "
                  f"call={sts[0]['id']!r} result={wsrs[0]['tool_use_id']!r}")
        else:
            print(f"\n  ✓ tool_use_id correlation OK: {sts[0]['id']!r}")


# ---------------------------------------------------------------------------
# Demo 2 — extracting URLs from the result string
# ---------------------------------------------------------------------------

def demo_extract_links(url: str, model: str) -> None:
    banner("Demo 2 — extracting URLs from web_search_tool_result")
    r = httpx.post(f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 200,
        "tools": [WEB_SEARCH],
        "messages": [{"role": "user",
                      "content": "Search for: latest Rust stable release. One-sentence answer."}],
    }, timeout=120)
    r.raise_for_status()
    msg = r.json()

    wsrs = [b for b in msg["content"] if b["type"] == "web_search_tool_result"]
    if not wsrs:
        print("  (model didn't run a search — no result block to parse)")
        return

    links = extract_links(wsrs[0]["content"])
    print(f"extracted {len(links)} links:")
    for i, lnk in enumerate(links, 1):
        title = lnk.get("title", "(no title)")[:80]
        url_ = lnk.get("url", "")
        print(f"  {i:>2}. {title}")
        print(f"      {url_}")


# ---------------------------------------------------------------------------
# Demo 3 — WebFetch visibility
# ---------------------------------------------------------------------------

def demo_webfetch_visibility(url: str, model: str) -> None:
    banner("Demo 3 — WebFetch visibility")
    target = "https://docs.anthropic.com/en/api/overview"
    r = httpx.post(f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 400,
        "tools": [WEB_FETCH],
        "messages": [{"role": "user",
                      "content": f"Fetch {target} and summarise what kind of page it is in one sentence."}],
    }, timeout=180)
    r.raise_for_status()
    msg = r.json()

    print(f"stop_reason:  {msg['stop_reason']!r}")
    print(f"block types:  {[b['type'] for b in msg['content']]}")
    print()
    print_blocks(msg["content"])


# ---------------------------------------------------------------------------
# Demo 4 — streaming with manual SSE reconstruction
# ---------------------------------------------------------------------------

def demo_streaming_visibility(url: str, model: str) -> None:
    banner("Demo 4 — streaming visibility (manual SSE parsing)")
    print("Receiving events as they arrive, reconstructing content[]...\n")

    call_inputs: dict[int, str] = {}    # idx -> accumulating json
    block_meta: dict[int, dict] = {}    # idx -> the content_block dict
    text_chunks: list[str] = []
    event_log: list[str] = []

    t0 = time.perf_counter()
    with httpx.stream("POST", f"{url}/v1/messages", json={
        "model": model,
        "max_tokens": 400,
        "stream": True,
        "tools": [WEB_SEARCH],
        "messages": [{"role": "user",
                      "content": "Search for the latest Node.js LTS version. Answer in one sentence."}],
    }, timeout=180) as resp:
        resp.raise_for_status()
        event_name = None
        for line in resp.iter_lines():
            if not line:
                event_name = None
                continue
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                t = d.get("type")
                if t == "content_block_start":
                    idx = d["index"]
                    cb = dict(d["content_block"])
                    block_meta[idx] = cb
                    ev = f"[{time.perf_counter()-t0:5.2f}s] start  idx={idx}  type={cb.get('type')!r}"
                    if cb.get("type") == "server_tool_use":
                        ev += f"  name={cb.get('name')!r}"
                        call_inputs[idx] = ""
                    event_log.append(ev)
                elif t == "content_block_delta":
                    idx = d["index"]
                    delta = d["delta"]
                    if delta["type"] == "input_json_delta" and idx in call_inputs:
                        call_inputs[idx] += delta["partial_json"]
                    elif delta["type"] == "text_delta":
                        text_chunks.append(delta["text"])
                elif t == "content_block_stop":
                    idx = d["index"]
                    meta = block_meta.get(idx, {})
                    if meta.get("type") == "server_tool_use" and idx in call_inputs:
                        try:
                            meta["input"] = json.loads(call_inputs.pop(idx) or "{}")
                        except json.JSONDecodeError:
                            meta["input"] = {}
                    event_log.append(f"         stop   idx={idx}  type={meta.get('type')!r}")
                elif t == "message_delta":
                    event_log.append(f"         message_delta  stop_reason={(d.get('delta') or {}).get('stop_reason')!r}")
                elif t == "message_stop":
                    event_log.append(f"         message_stop")

    dt = time.perf_counter() - t0
    print("\n".join(event_log))
    print()
    print(f"total time: {dt:.2f}s")
    print(f"reconstructed text: {''.join(text_chunks)!r}")
    print()
    print("reconstructed content[]:")
    for idx in sorted(block_meta):
        cb = block_meta[idx]
        if cb.get("type") == "text":
            cb = dict(cb)
            cb["text"] = "".join(text_chunks)
        print(f"  [{idx}] {format_block(cb).lstrip()}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8765", help="Conduit base URL")
    p.add_argument("--model", default=os.environ.get("CONDUIT_MODEL", "claude-haiku-4-5-20251001"),
                   help="Model to use. Defaults to $CONDUIT_MODEL or Haiku (cheapest).")
    p.add_argument("--skip", action="append", default=[],
                   choices=["search", "links", "fetch", "stream"],
                   help="Skip a demo (repeatable)")
    args = p.parse_args()

    check_health(args.url)

    if "search" not in args.skip:
        demo_websearch_visibility(args.url, args.model)
    if "links" not in args.skip:
        demo_extract_links(args.url, args.model)
    if "fetch" not in args.skip:
        demo_webfetch_visibility(args.url, args.model)
    if "stream" not in args.skip:
        demo_streaming_visibility(args.url, args.model)

    banner("All demos finished")


if __name__ == "__main__":
    main()
