"""Live end-to-end check for document blocks, citations, output_config.effort
and structured output through a running Conduit server.

Usage: python scripts/rag_live_check.py [base_url]   (default http://127.0.0.1:8799)
Requires: Conduit running, `claude` CLI logged in, ANTHROPIC_API_KEY unset.
"""
import json
import os
import sys

import httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"
MODEL = os.environ.get("CONDUIT_TEST_MODEL", "claude-haiku-4-5-20251001")

DOC = {
    "type": "document",
    "source": {"type": "text", "media_type": "text/plain",
               "data": "Internal memo. The launch code is ZEBRA-4471. The CFO is Priya Natarajan."},
    "title": "Memo Q3",
    "citations": {"enabled": True},
}
WINDOWS = {
    "type": "document",
    "source": {"type": "content", "content": [
        {"type": "text", "text": "Window 1: The warehouse moved to Tallinn in March."},
        {"type": "text", "text": "Window 2: Headcount is 37 as of September."},
    ]},
    "title": "Retrieved windows",
    "citations": {"enabled": True},
}
Q = {"type": "text", "text": "Using only the documents: what is the launch code, and where is the warehouse? One short sentence each."}

ok = True


def check(label, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label} {detail}")


def t_documents_non_streaming():
    print("\n== 1. documents + citations, non-streaming ==")
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 300,
        "messages": [{"role": "user", "content": [DOC, WINDOWS, Q]}],
    }, timeout=180)
    check("HTTP 200", r.status_code == 200, r.text[:200] if r.status_code != 200 else "")
    msg = r.json()
    text = "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")
    cites = [c for b in msg["content"] if b["type"] == "text" for c in (b.get("citations") or [])]
    print("  text:", text[:200].replace("\n", " "))
    print("  citations:", json.dumps(cites)[:400])
    check("answer grounded in doc", "ZEBRA-4471" in text and "Tallinn" in text)
    check("at least one citation", len(cites) >= 1)
    check("citation shapes", all(c.get("type") in ("char_location", "content_block_location") for c in cites))
    check("document_index spans both docs", {c.get("document_index") for c in cites} <= {0, 1})
    check("stop_reason end_turn", msg["stop_reason"] == "end_turn")


def t_documents_streaming():
    print("\n== 2. documents + citations, streaming ==")
    kinds, cites, text = [], [], []
    with httpx.stream("POST", f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 300, "stream": True,
        "messages": [{"role": "user", "content": [DOC, Q]}],
    }, timeout=180) as resp:
        check("HTTP 200", resp.status_code == 200)
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            d = json.loads(line[6:])
            kinds.append(d.get("type"))
            if d.get("type") == "content_block_delta":
                dl = d["delta"]
                if dl["type"] == "citations_delta":
                    cites.append(dl["citation"])
                elif dl["type"] == "text_delta":
                    text.append(dl["text"])
    print("  events:", sorted(set(kinds)))
    print("  text:", "".join(text)[:200].replace("\n", " "))
    check("citations_delta events arrived", len(cites) >= 1, f"n={len(cites)}")
    check("message_stop seen", "message_stop" in kinds)
    check("no error event", "error" not in kinds)


def t_structured_output():
    print("\n== 3. output_config.format (structured output), non-streaming ==")
    schema = {"type": "object",
              "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
              "required": ["name", "age"], "additionalProperties": False}
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 300,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        "messages": [{"role": "user", "content": "Give me a fictional person: name and age."}],
    }, timeout=180)
    check("HTTP 200", r.status_code == 200, r.text[:200] if r.status_code != 200 else "")
    msg = r.json()
    print("  content:", json.dumps(msg["content"])[:300])
    check("exactly one text block", [b["type"] for b in msg["content"]] == ["text"])
    try:
        obj = json.loads(msg["content"][0]["text"])
        check("text parses as JSON with name+age", isinstance(obj.get("name"), str) and isinstance(obj.get("age"), int))
    except Exception as e:  # noqa: BLE001
        check("text parses as JSON", False, repr(e))
    check("stop_reason end_turn", msg["stop_reason"] == "end_turn")


def t_structured_output_streaming():
    print("\n== 4. structured output, streaming ==")
    text, stops, starts = [], [], []
    with httpx.stream("POST", f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 300, "stream": True,
        "output_config": {"format": {"type": "json_schema", "schema": {
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}},
        "messages": [{"role": "user", "content": "Name one European capital."}],
    }, timeout=180) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            d = json.loads(line[6:])
            if d.get("type") == "content_block_start":
                starts.append(d["content_block"]["type"])
            elif d.get("type") == "content_block_delta" and d["delta"]["type"] == "text_delta":
                text.append(d["delta"]["text"])
            elif d.get("type") == "message_delta":
                stops.append(d["delta"].get("stop_reason"))
    joined = "".join(text)
    print("  starts:", starts, "stops:", stops, "text:", joined[:120])
    check("single text block", starts == ["text"])
    check("JSON text", joined.strip().startswith("{") and "city" in joined)
    check("end_turn", stops == ["end_turn"])


def t_structured_with_tools():
    print("\n== 5. structured output on the tool path (custom tool declared, unused) ==")
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 300,
        "tools": [{"name": "get_weather", "description": "Weather for a city. Only call if asked about weather.",
                   "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
        "output_config": {"format": {"type": "json_schema", "schema": {
            "type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}}},
        "messages": [{"role": "user", "content": "What is 6 times 7? Do not use tools."}],
    }, timeout=180)
    check("HTTP 200", r.status_code == 200, r.text[:200] if r.status_code != 200 else "")
    msg = r.json()
    print("  content:", json.dumps(msg["content"])[:300], "stop:", msg["stop_reason"])
    check("no tool_use pause", msg["stop_reason"] == "end_turn")
    check("JSON answer 42", json.loads(msg["content"][0]["text"]).get("answer") == 42)
    sid = r.headers.get("x-conduit-session-id")
    if sid:
        httpx.delete(f"{URL}/v1/sessions/{sid}", timeout=10)


def t_document_without_citations():
    print("\n== 6. plain document (no citations key), non-streaming ==")
    r = httpx.post(f"{URL}/v1/messages", json={
        "model": MODEL, "max_tokens": 200,
        "messages": [{"role": "user", "content": [
            {"type": "document",
             "source": {"type": "text", "media_type": "text/plain",
                        "data": "Project codename: BLUE HERON. Owner: Dana."}},
            {"type": "text", "text": "What is the project codename? Answer with just the codename."},
        ]}],
    }, timeout=180)
    check("HTTP 200", r.status_code == 200, r.text[:200] if r.status_code != 200 else "")
    msg = r.json()
    text = "".join(b.get("text", "") for b in msg["content"] if b["type"] == "text")
    print("  text:", text[:120])
    check("answer from document", "HERON" in text.upper())
    check("no citations arrays", all(not b.get("citations") for b in msg["content"]))


def t_official_sdk_output_config():
    print("\n== 7. official anthropic SDK: output_config as a first-class kwarg ==")
    from anthropic import Anthropic
    client = Anthropic(base_url=URL, api_key="not-used", max_retries=0, timeout=180)
    msg = client.messages.create(
        model=MODEL, max_tokens=200,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": {
            "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}},
        messages=[{"role": "user", "content": "How many legs does a spider have?"}],
    )
    print("  content:", [b.type for b in msg.content], getattr(msg.content[0], "text", "")[:80])
    check("SDK accepted output_config", msg.stop_reason == "end_turn")
    check("JSON n == 8", json.loads(msg.content[0].text).get("n") == 8)
    if hasattr(client.messages, "parse"):
        from pydantic import BaseModel

        class Spider(BaseModel):
            n: int

        parsed = client.messages.parse(
            model=MODEL, max_tokens=200, output_format=Spider,
            messages=[{"role": "user", "content": "How many legs does a spider have?"}],
        )
        print("  parse():", parsed.parsed_output)
        check("messages.parse() round-trips", getattr(parsed.parsed_output, "n", None) == 8)
    else:
        print("  (installed anthropic SDK has no messages.parse — skipped)")


if __name__ == "__main__":
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("WARN: ANTHROPIC_API_KEY set — metered API would be used")
    which = sys.argv[2:] or ["1", "2", "3", "4", "5", "6", "7"]
    for n, fn in [("1", t_documents_non_streaming), ("2", t_documents_streaming),
                  ("3", t_structured_output), ("4", t_structured_output_streaming),
                  ("5", t_structured_with_tools), ("6", t_document_without_citations),
                  ("7", t_official_sdk_output_config)]:
        if n in which:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  [FAIL] {fn.__name__} raised {type(e).__name__}: {e}")
    print("\nOVERALL:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
