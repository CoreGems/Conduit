"""Image blocks inside a client tool_result must reach the SDK in MCP shape.

The SDK's in-process MCP bridge reads ``item["data"]`` / ``item["mimeType"]``
for image content. Clients send Anthropic-format blocks
(``{"type": "image", "source": {"type": "base64", ...}}``), so passing them
through verbatim raised KeyError and a screenshot returned from a tool such
as ``screenshot`` never reached the model. ``deliver_tool_result`` now
translates them.
"""
import asyncio

from conduit.sessions import Session, _to_mcp_block


_PNG = "iVBORw0KGgo="
_ANTHROPIC_IMG = {
    "type": "image",
    "source": {"type": "base64", "media_type": "image/png", "data": _PNG},
}
_MCP_IMG = {"type": "image", "data": _PNG, "mimeType": "image/png"}


def _session() -> Session:
    return Session(id="s", client=None)


# --- _to_mcp_block ---------------------------------------------------------------

def test_base64_image_converted_to_mcp_shape():
    assert _to_mcp_block(_ANTHROPIC_IMG) == _MCP_IMG


def test_jpeg_media_type_preserved():
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}}
    assert _to_mcp_block(block) == {"type": "image", "data": "abc", "mimeType": "image/jpeg"}


def test_already_mcp_shaped_image_passes_through():
    assert _to_mcp_block(_MCP_IMG) == _MCP_IMG


def test_url_image_degrades_to_text():
    block = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    out = _to_mcp_block(block)
    assert out["type"] == "text"
    assert "https://example.com/a.png" in out["text"]


def test_text_block_untouched():
    block = {"type": "text", "text": "clicked at (10, 20)"}
    assert _to_mcp_block(block) is block


# --- deliver_tool_result: both delivery paths -------------------------------------

def test_deferred_path_converts_screenshot():
    # Handler hasn't fired yet (parallel-tool case) → stashed payload.
    s = _session()
    s.deliver_tool_result("toolu_1", [{"type": "text", "text": "screen:"}, _ANTHROPIC_IMG])
    assert s.deferred_results["toolu_1"] == {
        "content": [{"type": "text", "text": "screen:"}, _MCP_IMG],
    }


async def test_live_future_path_converts_screenshot():
    # Handler already parked on a Future → resolved with the converted payload.
    s = _session()
    s.record_pending_id("screenshot", "toolu_2")
    waiter = asyncio.create_task(s.await_tool_result("screenshot", {}))
    await asyncio.sleep(0)  # let the handler park on its Future
    assert "toolu_2" in s.pending_futures

    s.deliver_tool_result("toolu_2", [_ANTHROPIC_IMG])
    assert await waiter == {"content": [_MCP_IMG]}


def test_string_result_still_wrapped_as_text():
    s = _session()
    s.deliver_tool_result("toolu_3", "ok")
    assert s.deferred_results["toolu_3"] == {"content": [{"type": "text", "text": "ok"}]}
