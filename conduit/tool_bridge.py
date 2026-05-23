"""Per-session MCP bridge for client-defined tools.

Mirrors the request's `tools` array into an in-process SDK MCP server whose
handlers suspend on Futures owned by the session. When Claude invokes a tool,
our handler parks; the HTTP layer surfaces the tool_use block to the client,
ends the SSE response, and waits for the client to come back with a
tool_result that resolves the Future.

See TOOLS_HOWTO.md for the full design.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

if TYPE_CHECKING:
    from conduit.sessions import Session  # avoid runtime circular import

MCP_SERVER_NAME = "conduit"


# Hosted (SDK built-in) tools — the SDK runs them itself, no client callback.
# Client declares them in `tools[]` with an Anthropic-compatible `type` prefix;
# Conduit maps to the SDK's tool name.
HOSTED_TOOL_TYPE_TO_SDK_NAME = {
    "web_search": "WebSearch",
    "web_fetch":  "WebFetch",
}
HOSTED_SDK_NAMES = set(HOSTED_TOOL_TYPE_TO_SDK_NAME.values())


def make_tool_name(local_name: str) -> str:
    """Translate a client tool name into the SDK's `mcp__server__tool` form."""
    return f"mcp__{MCP_SERVER_NAME}__{local_name}"


def local_name_from_full(full: str) -> str | None:
    """Inverse of make_tool_name. Returns None if `full` isn't one of ours."""
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    return full[len(prefix):] if full.startswith(prefix) else None


def is_hosted_sdk_tool(name: str) -> bool:
    """True if `name` is an SDK built-in hosted tool (WebSearch, WebFetch)."""
    return name in HOSTED_SDK_NAMES


def classify_request_tool(tool: dict) -> tuple[str, str | None]:
    """Return (kind, sdk_name) for a tool declaration in a request's `tools` array.

    kind:
      - "hosted"  — an SDK-executed tool. sdk_name is e.g. "WebSearch".
      - "custom"  — a client-defined tool needing a bridge. sdk_name is None.

    Detection:
      - hosted by Anthropic-compatible `type` prefix (`web_search_20250305`, etc.)
      - hosted by bare `name` ("WebSearch", "WebFetch") for convenience
      - everything else → custom
    """
    ttype = tool.get("type", "") or ""
    for prefix, sdk_name in HOSTED_TOOL_TYPE_TO_SDK_NAME.items():
        if ttype.startswith(prefix):
            return "hosted", sdk_name
    name = tool.get("name", "") or ""
    if name in HOSTED_SDK_NAMES:
        return "hosted", name
    return "custom", None


def split_tools(tools: list[dict]) -> tuple[list[dict], list[str]]:
    """Partition the request's `tools` array.

    Returns (custom_specs, hosted_sdk_names).
    """
    custom: list[dict] = []
    hosted: list[str] = []
    for t in tools:
        kind, sdk_name = classify_request_tool(t)
        if kind == "hosted" and sdk_name:
            if sdk_name not in hosted:
                hosted.append(sdk_name)
        else:
            custom.append(t)
    return custom, hosted


def build_bridge(
    tools_spec: list[dict[str, Any]],
    session: "Session",
) -> tuple[Any, list[str]]:
    """Build the MCP server + the list of fully qualified names to allow.

    Each handler delegates to `session.await_tool_result(name, input)`, which
    parks the SDK loop until the corresponding tool_result is delivered.
    """
    sdk_tools = []
    for spec in tools_spec:
        local_name = spec["name"]
        description = spec.get("description", "")
        input_schema = spec.get("input_schema") or {"type": "object"}

        async def handler(arg: dict, _local=local_name) -> dict:
            result = await session.await_tool_result(_local, arg)
            return result

        sdk_tools.append(
            tool(name=local_name, description=description, input_schema=input_schema)(handler)
        )

    server = create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=sdk_tools)
    allowed = [make_tool_name(s["name"]) for s in tools_spec]
    return server, allowed
