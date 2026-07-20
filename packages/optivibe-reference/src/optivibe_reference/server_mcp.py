"""server_mcp.py — thin MCP adapter over the reference dispatch core (LAZY mcp).

Re-implemented (NOT imported) from the harness ``server_mcp.py``. ``mcp`` is NOT
imported at module top — ``build_mcp_server`` imports it INSIDE the function body
so this module loads anywhere; the dependency is needed only when an MCP server
is actually built (§6).

The adapter is intentionally thin: it mirrors ``Dispatcher.list_tools()`` as the
MCP tool list and routes every ``call_tool`` straight to ``Dispatcher.dispatch``,
whose never-raise envelope becomes the tool result.
"""


def build_mcp_server(dispatcher):
    """Build an MCP ``Server`` exposing the dispatcher's tools (LAZY mcp import).

    Imports ``mcp`` HERE (never at module top). Mirrors
    ``Dispatcher.list_tools()`` into MCP tool descriptors and routes each
    ``call_tool`` to ``Dispatcher.dispatch``, returning its envelope.
    """
    import mcp.types as mcp_types  # noqa: E402 — lazy: mcp may be absent at import
    from mcp.server import Server  # noqa: E402

    server = Server("optivibe-reference")

    @server.list_tools()
    async def _list_tools():
        tools = []
        for entry in dispatcher.list_tools():
            tools.append(
                mcp_types.Tool(
                    name=entry["name"],
                    description=entry["description"],
                    inputSchema={
                        "type": "object",
                        "properties": {
                            p: {"type": "string"} for p in entry["required_params"]
                        },
                        "required": list(entry["required_params"]),
                    },
                )
            )
        return tools

    @server.call_tool()
    async def _call_tool(name, arguments):
        envelope = dispatcher.dispatch(name, arguments or {})
        return [mcp_types.TextContent(type="text", text=repr(envelope))]

    return server
