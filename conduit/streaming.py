"""Agent SDK message stream → Anthropic-compatible SSE event stream.

Two stream functions:

  * ``stream_anthropic_events`` / ``collect_non_streaming`` — plain chat
    (no tools). Iterates ``receive_response()`` inline under the session
    lock. The non-streaming variant folds the same SSE events into one
    Message via ``collect_message_from_sse`` (shared with the tool path), so
    text, citations, thinking and structured output render identically.

  * ``stream_tool_events`` — tool-use sessions. Uses a per-session
    background pump that drains ``receive_response()`` into a queue so
    that the HTTP response can complete after the SDK pauses inside a
    `@tool` handler. The next HTTP request resumes consumption from the
    same queue.

Both functions emit byte-identical Anthropic SSE wire format.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from typing import Any, AsyncIterator

from fastapi import HTTPException

from claude_agent_sdk import (
    ResultMessage,
    StreamEvent,
    UserMessage,
)

from conduit.sessions import Session, SessionState
from conduit.tool_bridge import is_hosted_sdk_tool, local_name_from_full

log = logging.getLogger(__name__)


# Map SDK hosted tool name to the wire-format result block type.
_HOSTED_RESULT_TYPE = {
    "WebSearch": "web_search_tool_result",
    "WebFetch":  "web_fetch_tool_result",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, separators=(",", ":"))}


def _as_sdk_prompt(user_content: str | list[dict]) -> str | AsyncIterator[dict]:
    """Adapt a route-level prompt to what ``ClaudeSDKClient.query()`` accepts.

    ``query()`` takes either a plain string or an async iterable of full
    message dicts. Content-block prompts (turns carrying image blocks) need
    the latter: wrap the blocks in a single Anthropic-shaped user message.
    """
    if isinstance(user_content, str):
        return user_content

    async def _one() -> AsyncIterator[dict]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": user_content},
            "parent_tool_use_id": None,
        }

    return _one()


def _error_event(exc: HTTPException) -> dict:
    """Anthropic wire-format ``error`` SSE event carrying an HTTPException.

    Same shape the real API emits when a request fails after the stream has
    opened. The Anthropic SDKs raise it client-side as an ``APIStatusError``
    whose body carries our ``type`` / ``message``.
    """
    detail = exc.detail
    if not isinstance(detail, dict):
        detail = {"type": "api_error", "message": str(detail)}
    return _sse(
        "error",
        {
            "type": "error",
            "error": {
                "type": detail.get("type") or "api_error",
                "message": detail.get("message") or f"HTTP {exc.status_code}",
            },
        },
    )


async def guard_sse(events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Deliver a stream failure to the client as an ``error`` event.

    sse_starlette writes the response head (``200`` + SSE headers) before it
    pulls the first item from the body iterator, so by the time any stream
    function raises, the HTTP status is already on the wire. Left alone,
    Starlette re-raises the exception as "Caught handled exception, but
    response already started", the connection is cut, and the client sees a
    truncated ``200`` stream with no error at all. Instead, emit the failure
    as an Anthropic ``error`` event and end the stream cleanly.

    Only the streaming routes go through this. The non-streaming collectors
    let the HTTPException propagate so FastAPI returns a real 4xx/5xx body.
    """
    try:
        async for ev in events:
            yield ev
    except HTTPException as exc:
        yield _error_event(exc)
    except Exception as exc:  # noqa: BLE001 — anything else would also cut the stream
        log.exception("streaming turn failed")
        yield _error_event(
            HTTPException(
                status_code=500,
                detail={"type": "api_error", "message": f"{type(exc).__name__}: {exc}"},
            )
        )


# Map an upstream HTTP status (surfaced by the SDK on a recoverable API error)
# to the Anthropic error `type`. Anything else falls back to a generic api_error.
_API_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}


# Substrings that mark a subscription (Claude Max/Pro) usage-limit exhaustion —
# distinct from a transient server rate limit. The Agent SDK surfaces the CLI's
# usage-limit result as ordinary error text rather than a distinct status code,
# so we detect it by content. Matched case-insensitively.
_QUOTA_MARKERS = (
    "usage limit reached",
    "usage limit",
    "reached your usage limit",
    "credit balance is too low",
    "out of credits",
    "insufficient credits",
    "quota exceeded",
)


# Shown when a turn ends with no assistant content and no explicit error signal.
# The overwhelmingly common cause in practice is an exhausted Claude subscription
# quota, so we name it rather than returning a bare "no content" placeholder.
_EMPTY_TURN_DETAIL = (
    "The model returned an empty response (no content and no error from the "
    "upstream). The most common cause is an exhausted Claude subscription quota "
    "(Max/Pro plan usage limit); it can also mean the turn was interrupted "
    "before any output. Check your plan's usage limits — if quota is the cause, "
    "retrying will not help until it resets."
)


def _looks_like_quota(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _QUOTA_MARKERS)


def _quota_error(text: str) -> HTTPException:
    """Build a clear, client-facing 429 for a subscription usage-limit hit."""
    reset_hint = ""
    # Claude Code emits "Claude AI usage limit reached|<unix_ts>"; surface the
    # reset timestamp when present.
    if "|" in text:
        tail = text.rsplit("|", 1)[-1].strip()
        if tail.isdigit():
            reset_hint = f" Quota resets at unix time {tail}."
    return HTTPException(
        status_code=429,
        detail={
            "type": "rate_limit_error",
            "message": (
                "Claude subscription quota exhausted — your Max/Pro plan usage "
                f"limit has been reached.{reset_hint} This is a plan-level limit, "
                "not a transient rate limit, so retrying will not help until the "
                f"quota resets. Upstream reported: {text.strip()}"
            ),
        },
    )


def _result_error(msg: ResultMessage) -> HTTPException | None:
    """Inspect a ResultMessage for upstream/SDK failure signaling.

    The SDK reports a recoverable upstream API error (429 / 5xx / 529 overloaded,
    etc.) as a normal ResultMessage with ``is_error=True`` and/or
    ``api_error_status`` set, carrying NO assistant content — not as a raised
    exception. Subscription usage-limit ("out of quota") is surfaced the same
    way, only with the reason carried as error text. If we don't catch these
    here we'd return a 200 with empty content and zero tokens, which clients
    read as "no content in LLM response". Translate them into a proper
    Anthropic-format error so the caller can show something meaningful.

    Returns an HTTPException to raise, or None if the result was a clean turn.
    """
    is_error = bool(getattr(msg, "is_error", False))
    api_status = getattr(msg, "api_error_status", None)
    subtype = getattr(msg, "subtype", None)

    errors = getattr(msg, "errors", None) or []
    result_text = getattr(msg, "result", None)
    if errors:
        text_blob = "; ".join(str(e) for e in errors)
    elif result_text:
        text_blob = str(result_text)
    else:
        text_blob = ""

    # Only read `text_blob` as a failure reason when the SDK actually flagged
    # this turn as an error. On a clean success turn `result` holds the model's
    # real answer, which may legitimately contain phrases like "usage limit" —
    # we must not misclassify that as a quota failure.
    errorish = is_error or (isinstance(subtype, str) and subtype.startswith("error"))

    if errorish and text_blob and _looks_like_quota(text_blob):
        return _quota_error(text_blob)

    if not is_error and api_status is None:
        return None

    status = api_status if isinstance(api_status, int) and 400 <= api_status < 600 else 502
    etype = _API_ERROR_TYPE.get(status) or ("overloaded_error" if status == 503 else "api_error")

    if text_blob:
        detail_msg = text_blob
    else:
        detail_msg = f"upstream turn failed (subtype={subtype!r}, api_error_status={api_status})"

    return HTTPException(status_code=status, detail={"type": etype, "message": detail_msg})


def _rewrite_message_start(ev: dict, model: str) -> dict:
    msg = dict(ev.get("message", {}))
    msg["model"] = model
    return {**ev, "message": msg}


def _clean_tool_use_block(content_block: dict) -> dict:
    """Rewrite SDK-prefixed tool name back to its bare form and drop
    SDK-internal fields (`caller`, etc.) that aren't part of Anthropic's
    documented `tool_use` block shape."""
    out = dict(content_block)
    name = out.get("name")
    if isinstance(name, str):
        local = local_name_from_full(name)
        if local is not None:
            out["name"] = local
    # SDK adds fields like `caller` that aren't in the Anthropic wire format
    for k in ("caller",):
        out.pop(k, None)
    return out


# Delta types that belong to forwarded blocks and pass through unchanged:
# text_delta (text), citations_delta (text, when the request carried document
# blocks with citations enabled), input_json_delta (tool_use/server_tool_use),
# thinking_delta + signature_delta (thinking).
_FORWARDED_DELTAS = ("text_delta", "citations_delta", "input_json_delta",
                     "thinking_delta", "signature_delta")


# Name of the CLI-internal end-turn tool the Agent SDK registers when
# `output_format` (structured output) is set. The model "calls" it with the
# JSON document as the tool input, and the turn ends there.
STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


def _is_structured_output_call(cb: dict) -> bool:
    return cb.get("type") == "tool_use" and cb.get("name") == STRUCTURED_OUTPUT_TOOL


def _structured_text_start(ev: dict, out_idx: int) -> dict:
    """Present the StructuredOutput call as the upstream API would: a plain
    `text` block whose text is the JSON document."""
    return _sse("content_block_start",
                {**ev, "index": out_idx, "content_block": {"type": "text", "text": ""}})


def _structured_text_delta(ev: dict, out_idx: int) -> dict | None:
    delta = ev.get("delta") or {}
    if delta.get("type") != "input_json_delta":
        return None
    return _sse("content_block_delta",
                {**ev, "index": out_idx,
                 "delta": {"type": "text_delta", "text": delta.get("partial_json", "")}})


def _structured_end_turn(ev: dict) -> dict:
    """A StructuredOutput call ends the turn: upstream reports `end_turn`,
    not `tool_use` (there is nothing for the client to resume)."""
    delta = dict(ev.get("delta") or {})
    if delta.get("stop_reason") == "tool_use":
        delta["stop_reason"] = "end_turn"
    return {**ev, "delta": delta}


# ---------------------------------------------------------------------------
# Plain-chat path
# ---------------------------------------------------------------------------

async def stream_anthropic_events(
    session: Session,
    user_content: str | list[dict],
    model: str,
) -> AsyncIterator[dict]:
    await session.client.query(_as_sdk_prompt(user_content))

    block_map: dict[int, dict[str, Any]] = {}
    next_out_index = 0
    sent_message_start = False
    # Structured output (output_config.format): only the StructuredOutput call
    # is surfaced, as a JSON text block; any prose text blocks are suppressed so
    # content[] holds exactly the JSON document, as upstream returns it.
    structured_mode = bool(getattr(session, "output_format", None))
    structured_seen = False
    # In structured mode a message may end before the JSON arrives (the model
    # answered in prose; the CLI prompts it again and the StructuredOutput
    # call comes in a follow-up message). Hold that ending: a later
    # message_start supersedes it, the ResultMessage flushes it.
    held: list[dict] = []

    async for msg in session.client.receive_response():
        if isinstance(msg, StreamEvent):
            ev = msg.event
            etype = ev.get("type")

            if etype == "message_start":
                structured_seen = False
                held.clear()
                if not sent_message_start:
                    yield _sse("message_start", _rewrite_message_start(ev, model))
                    sent_message_start = True

            elif etype == "content_block_start":
                sdk_idx = ev.get("index", 0)
                cb = ev.get("content_block") or {}
                block_type = cb.get("type")
                if _is_structured_output_call(cb):
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": "structured"}
                    structured_seen = True
                    yield _structured_text_start(ev, out_idx)
                elif (block_type == "text" and not structured_mode) or (
                    block_type == "thinking" and session.include_thinking
                ):
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": block_type}
                    yield _sse("content_block_start", {**ev, "index": out_idx})
                else:
                    block_map[sdk_idx] = {"out_index": None, "type": block_type}

            elif etype == "content_block_delta":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx)
                if info and info["out_index"] is not None:
                    if info.get("type") == "structured":
                        out = _structured_text_delta(ev, info["out_index"])
                        if out is not None:
                            yield out
                    elif (ev.get("delta") or {}).get("type") in _FORWARDED_DELTAS:
                        yield _sse("content_block_delta", {**ev, "index": info["out_index"]})

            elif etype == "content_block_stop":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx)
                if info and info["out_index"] is not None:
                    yield _sse("content_block_stop", {**ev, "index": info["out_index"]})

            elif etype == "message_delta":
                out = _sse("message_delta", _structured_end_turn(ev) if structured_seen else ev)
                if structured_mode and not structured_seen:
                    held.append(out)
                else:
                    yield out

            elif etype == "message_stop":
                out = _sse("message_stop", ev)
                if structured_mode and not structured_seen:
                    held.append(out)
                else:
                    yield out

        elif isinstance(msg, ResultMessage):
            err = _result_error(msg)
            if err is not None:
                # Flagged failure (incl. quota). Raise even if content was
                # already streamed — the route wraps us in guard_sse, which
                # turns this into an `error` event, exactly how the real API
                # reports a mid-stream failure.
                raise err
            if not sent_message_start:
                # Clean result but the turn produced nothing at all — that is
                # itself the failure to report, not a hollow success.
                raise HTTPException(
                    status_code=502,
                    detail={"type": "api_error", "message": _EMPTY_TURN_DETAIL},
                )
            # Structured mode, JSON never arrived: close the stream as it ended.
            for out in held:
                yield out
            session.message_count += 1
            return


async def collect_message_from_sse(events: AsyncIterator[dict], model: str) -> dict:
    """Fold an Anthropic SSE event stream into a single non-streaming Message.

    Shared by the plain-chat and tool-use non-streaming routes so both paths
    render content identically: text (with `citations` when the request
    carried cited documents), thinking, tool_use / server_tool_use (input
    JSON reassembled), hosted-tool result blocks, and structured-output JSON.
    """
    msg: dict = {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    blocks: dict[int, dict] = {}

    async for sse in events:
        data = json.loads(sse["data"])
        t = data.get("type")
        if t == "message_start":
            m = data["message"]
            msg["id"] = m.get("id", msg["id"])
            msg["model"] = m.get("model", model)
            u = m.get("usage") or {}
            msg["usage"]["input_tokens"] = u.get("input_tokens", 0)
            msg["usage"]["cache_creation_input_tokens"] = u.get("cache_creation_input_tokens", 0)
            msg["usage"]["cache_read_input_tokens"] = u.get("cache_read_input_tokens", 0)
        elif t == "content_block_start":
            idx = data["index"]
            blocks[idx] = dict(data["content_block"])
            # Both custom tool_use and hosted server_tool_use stream their input
            # as input_json_delta chunks that we reassemble.
            if blocks[idx].get("type") in ("tool_use", "server_tool_use"):
                blocks[idx].setdefault("input", {})
                blocks[idx]["_input_partial"] = ""
        elif t == "content_block_delta":
            idx = data["index"]
            delta = data["delta"]
            b = blocks.get(idx)
            if b is None:
                continue
            if delta["type"] == "text_delta":
                b["text"] = b.get("text", "") + delta["text"]
            elif delta["type"] == "citations_delta":
                b.setdefault("citations", []).append(delta.get("citation"))
            elif delta["type"] == "input_json_delta":
                b["_input_partial"] = b.get("_input_partial", "") + delta["partial_json"]
            elif delta["type"] == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + delta.get("thinking", "")
            elif delta["type"] == "signature_delta":
                b["signature"] = (b.get("signature", "") or "") + delta.get("signature", "")
        elif t == "content_block_stop":
            idx = data["index"]
            b = blocks.get(idx) or {}
            if b.get("type") in ("tool_use", "server_tool_use"):
                try:
                    b["input"] = json.loads(b.pop("_input_partial", "") or "{}")
                except json.JSONDecodeError:
                    b["input"] = {}
                    b.pop("_input_partial", None)
            # web_search_tool_result / web_fetch_tool_result blocks come with
            # their content already populated at content_block_start time
            # (synthesized from the SDK's UserMessage) — nothing to reassemble.
        elif t == "message_delta":
            d = data.get("delta") or {}
            msg["stop_reason"] = d.get("stop_reason", msg["stop_reason"])
            msg["stop_sequence"] = d.get("stop_sequence")
            u = data.get("usage") or {}
            if "output_tokens" in u:
                msg["usage"]["output_tokens"] = u["output_tokens"]
            if "cache_creation_input_tokens" in u:
                msg["usage"]["cache_creation_input_tokens"] = u["cache_creation_input_tokens"]
            if "cache_read_input_tokens" in u:
                msg["usage"]["cache_read_input_tokens"] = u["cache_read_input_tokens"]
        # message_stop ends the iteration via the stream function itself

    msg["content"] = [blocks[i] for i in sorted(blocks.keys())]
    return msg


async def collect_non_streaming(
    session: Session,
    user_content: str | list[dict],
    model: str,
) -> dict:
    """Plain-chat non-streaming: fold the streaming path's events into one
    Message. A failed or empty turn raises the same HTTPException the stream
    would, so FastAPI returns a real 4xx/5xx body."""
    return await collect_message_from_sse(
        stream_anthropic_events(session, user_content, model), model
    )


# ---------------------------------------------------------------------------
# Tool-use path: pump + pause/resume
# ---------------------------------------------------------------------------

_PUMP_END_SENTINEL: Any = object()


async def _pump_session(session: Session) -> None:
    """Drain one turn of receive_response() into the session's event queue.

    The SDK's receive_response() is per-query: it yields events from a single
    query() call through to the trailing ResultMessage, then exhausts. The
    pump exits naturally at that point. For Conduit's multi-turn support,
    stream_tool_events creates a fresh pump per new turn.
    """
    try:
        async for msg in session.client.receive_response():
            await session.events_queue.put(msg)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await session.events_queue.put(("ERROR", e))
    finally:
        await session.events_queue.put(_PUMP_END_SENTINEL)


async def stream_tool_events(
    session: Session,
    user_content: str | list[dict] | None,
    model: str,
) -> AsyncIterator[dict]:
    """Stream a turn (initial or resume) for a tool-use session.

    Args:
      user_content:
        - For an **initial** request, the new user prompt (plain text, or a
          text+image content-block list); we call query() to kick off the
          SDK turn before draining the queue.
        - For a **resume** request, pass None. The routes layer has already
          delivered the tool_result(s) to the parked Future(s); the pump is
          continuing to produce events.

    Stops streaming when:
      - We see `message_stop` after `stop_reason: tool_use`  → pause
      - We see `message_stop` after a terminal stop_reason   → done
      - The pump emits its end sentinel                       → done
    """
    if session.events_queue is None:
        raise RuntimeError("stream_tool_events called on non-tool session")

    if user_content is not None:
        # New turn. Each SDK receive_response() is per-query() — it yields
        # events for one query and exhausts after ResultMessage. If the prior
        # pump is still alive (custom-tool pause: parked in @tool handler,
        # holding the iterator open across HTTP requests), reuse it; the new
        # query() flows through the existing iterator. Otherwise start fresh.
        # Note: routes/messages.py awaits pump_task at the end of each turn
        # via _drain_trailing_pump, so by the time a multi-turn request hits
        # us with the prior turn fully done, pump_task.done() == True.
        if session.pump_task is None or session.pump_task.done():
            session.events_queue = asyncio.Queue()
            await session.client.query(_as_sdk_prompt(user_content))
            session.pump_task = asyncio.create_task(_pump_session(session))
        else:
            await session.client.query(_as_sdk_prompt(user_content))
    else:
        # Resume (tool_result delivered above). The pump must still be alive,
        # parked inside the @tool handler awaiting our Future.
        if session.pump_task is None or session.pump_task.done():
            raise RuntimeError("resume request but pump task is no longer running")

    session.state = SessionState.STREAMING

    block_map: dict[int, dict[str, Any]] = {}
    next_out_index = 0
    last_stop_reason: str | None = None
    # True once we've emitted a `message_start` (nothing precedes it on the
    # wire). If a turn ends without this ever flipping, the upstream produced
    # nothing — treat that as a reportable failure, not a hollow success.
    produced_output = False
    # Counts CUSTOM (client-defined) tool_use blocks since the last message_start.
    # Hosted tool_use blocks (WebSearch/WebFetch) are forwarded as server_tool_use
    # but don't increment this — they don't constitute a real client pause.
    visible_tool_use_count = 0
    # Set to True when we suppress a hosted-only stop_reason=tool_use message_delta.
    # The next message_stop + next message_start are also suppressed so the
    # hidden SDK turn boundary is invisible to the client.
    in_hidden_hosted_pause = False
    # Map tool_use_id -> hosted tool name, so when the SDK's UserMessage
    # arrives with results we can synthesize a properly-typed result block.
    hosted_tool_calls: dict[str, str] = {}
    # Structured output (output_config.format): see stream_anthropic_events.
    structured_mode = bool(getattr(session, "output_format", None))
    structured_seen = False
    # A JSON-less message ending withheld in structured mode (the CLI will
    # prompt the model again); flushed only if the turn ends without JSON.
    held: list[dict] = []

    while True:
        msg = await session.events_queue.get()
        if msg is _PUMP_END_SENTINEL:
            session.state = SessionState.IDLE
            if not produced_output:
                raise HTTPException(
                    status_code=502,
                    detail={"type": "api_error", "message": _EMPTY_TURN_DETAIL},
                )
            for out in held:
                yield out
            return
        if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "ERROR":
            session.state = SessionState.IDLE
            raise msg[1]

        if isinstance(msg, StreamEvent):
            ev = msg.event
            etype = ev.get("type")

            if etype == "message_start":
                if in_hidden_hosted_pause:
                    # Continuation of the same logical turn — suppress.
                    # Don't reset next_out_index (indices stay continuous to
                    # the client) but DO clear block_map (SDK resets sdk_idx
                    # to 0 each message_start).
                    in_hidden_hosted_pause = False
                    block_map.clear()
                    visible_tool_use_count = 0
                    last_stop_reason = None
                    structured_seen = False
                    held.clear()  # the follow-up message supersedes the held ending
                else:
                    block_map.clear()
                    next_out_index = 0
                    visible_tool_use_count = 0
                    last_stop_reason = None
                    structured_seen = False
                    produced_output = True
                    yield _sse("message_start", _rewrite_message_start(ev, model))

            elif etype == "content_block_start":
                sdk_idx = ev.get("index", 0)
                cb = ev.get("content_block") or {}
                block_type = cb.get("type")
                name = cb.get("name", "")

                if _is_structured_output_call(cb):
                    # CLI-internal end-turn tool carrying the structured-output
                    # JSON. Surface it as a text block; it is not a client pause,
                    # so it doesn't count toward visible_tool_use_count.
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": "structured"}
                    structured_seen = True
                    yield _structured_text_start(ev, out_idx)

                elif (block_type == "text" and not structured_mode) or (
                    block_type == "thinking" and session.include_thinking
                ):
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": block_type}
                    yield _sse("content_block_start", {**ev, "index": out_idx})

                elif block_type == "tool_use" and is_hosted_sdk_tool(name):
                    # Hosted SDK tool — SDK executes internally. Forward as
                    # `server_tool_use` (Anthropic-canonical wire format) so the
                    # client can see what was called with what input. Does NOT
                    # increment visible_tool_use_count — hosted calls don't pause.
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": "server_tool_use"}
                    tool_use_id = cb.get("id")
                    if tool_use_id:
                        hosted_tool_calls[tool_use_id] = name
                    cb_rewritten = {k: v for k, v in cb.items() if k != "caller"}
                    cb_rewritten["type"] = "server_tool_use"
                    yield _sse(
                        "content_block_start",
                        {**ev, "index": out_idx, "content_block": cb_rewritten},
                    )

                elif block_type == "tool_use":
                    out_idx = next_out_index
                    next_out_index += 1
                    block_map[sdk_idx] = {"out_index": out_idx, "type": "tool_use"}
                    cb_clean = _clean_tool_use_block(cb)
                    local_name = cb_clean.get("name")
                    tool_use_id = cb.get("id")
                    if local_name and tool_use_id:
                        session.record_pending_id(local_name, tool_use_id)
                    visible_tool_use_count += 1
                    yield _sse(
                        "content_block_start",
                        {**ev, "index": out_idx, "content_block": cb_clean},
                    )

                else:
                    # thinking, server_tool_use, web_search_tool_result, etc.
                    block_map[sdk_idx] = {"out_index": None, "type": block_type}

            elif etype == "content_block_delta":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx) or {}
                if info.get("out_index") is None:
                    continue
                if info.get("type") == "structured":
                    out = _structured_text_delta(ev, info["out_index"])
                    if out is not None:
                        yield out
                elif (ev.get("delta") or {}).get("type") in _FORWARDED_DELTAS:
                    yield _sse("content_block_delta", {**ev, "index": info["out_index"]})

            elif etype == "content_block_stop":
                sdk_idx = ev.get("index", 0)
                info = block_map.get(sdk_idx) or {}
                if info.get("out_index") is not None:
                    yield _sse("content_block_stop", {**ev, "index": info["out_index"]})

            elif etype == "message_delta":
                if structured_seen:
                    ev = _structured_end_turn(ev)
                last_stop_reason = (ev.get("delta") or {}).get("stop_reason")
                if last_stop_reason == "tool_use" and visible_tool_use_count == 0:
                    # Hosted-only pause — suppress this signal; SDK continues internally.
                    in_hidden_hosted_pause = True
                elif structured_mode and not structured_seen and last_stop_reason != "tool_use":
                    # Structured mode but no JSON yet: the CLI prompts the model
                    # again in a follow-up message. Treat this ending like a
                    # hidden pause, but keep it in case the turn ends here.
                    in_hidden_hosted_pause = True
                    held = [_sse("message_delta", ev)]
                else:
                    yield _sse("message_delta", ev)

            elif etype == "message_stop":
                if in_hidden_hosted_pause:
                    # Paired with the suppressed message_delta; don't emit, don't exit.
                    if held:
                        held.append(_sse("message_stop", ev))
                else:
                    yield _sse("message_stop", ev)
                    if last_stop_reason == "tool_use":
                        session.state = SessionState.PAUSED_FOR_TOOLS
                        return
                    if last_stop_reason in ("end_turn", "stop_sequence", "max_tokens"):
                        session.state = SessionState.IDLE
                        return

        elif isinstance(msg, ResultMessage):
            err = _result_error(msg)
            if err is not None:
                session.state = SessionState.IDLE
                raise err
            if not produced_output:
                # Clean result but the turn yielded nothing — report it rather
                # than letting the collector return empty `content: []`.
                session.state = SessionState.IDLE
                raise HTTPException(
                    status_code=502,
                    detail={"type": "api_error", "message": _EMPTY_TURN_DETAIL},
                )
            for out in held:  # structured mode, JSON never arrived
                yield out
            session.message_count += 1
            session.state = SessionState.IDLE
            return

        elif isinstance(msg, UserMessage):
            # The SDK feeds hosted-tool results back as a UserMessage between
            # the model's hidden turn boundaries. Each content item is a
            # tool_result-shaped dict (tool_use_id + content). Synthesize
            # wire-format result blocks for any that correspond to a hosted
            # tool we forwarded earlier.
            for item in (getattr(msg, "content", None) or []):
                tu_id = item.get("tool_use_id") if isinstance(item, dict) else getattr(item, "tool_use_id", None)
                content = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
                if not tu_id or content is None:
                    continue
                tool_name = hosted_tool_calls.get(tu_id)
                if tool_name is None:
                    continue
                result_type = _HOSTED_RESULT_TYPE.get(tool_name)
                if result_type is None:
                    continue
                out_idx = next_out_index
                next_out_index += 1
                synth_block = {
                    "type": result_type,
                    "tool_use_id": tu_id,
                    "content": content,
                }
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": out_idx,
                    "content_block": synth_block,
                })
                yield _sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": out_idx,
                })

        # Other message types (HookEventMessage, SystemMessage, AssistantMessage,
        # RateLimitEvent) are not part of the Anthropic wire format for chat —
        # drop silently.
