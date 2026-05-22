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


def make_tool_name(local_name: str) -> str:
    """Translate a client tool name into the SDK's `mcp__server__tool` form."""
    return f"mcp__{MCP_SERVER_NAME}__{local_name}"


def local_name_from_full(full: str) -> str | None:
    """Inverse of make_tool_name. Returns None if `full` isn't one of ours."""
    prefix = f"mcp__{MCP_SERVER_NAME}__"
    return full[len(prefix):] if full.startswith(prefix) else None


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
