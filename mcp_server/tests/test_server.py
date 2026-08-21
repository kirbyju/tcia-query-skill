from __future__ import annotations

import unittest

from mcp.server.fastmcp import FastMCP

from mcp_server.tcia_query_mcp.server import (
    LEGACY_MCP_TOOL_NAMES,
    PUBLIC_V2_TOOL_NAMES,
    mcp,
    register_legacy_mcp_tools,
)


class McpToolSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_surface_advertises_only_supported_v2_tools(self) -> None:
        tools = await mcp.list_tools()
        names = tuple(tool.name for tool in tools)

        self.assertEqual(names, PUBLIC_V2_TOOL_NAMES)
        self.assertTrue(set(names).isdisjoint(LEGACY_MCP_TOOL_NAMES))

    async def test_legacy_tools_require_explicit_registration(self) -> None:
        legacy_server = FastMCP("TCIA legacy compatibility test")
        register_legacy_mcp_tools(legacy_server)

        tools = await legacy_server.list_tools()
        self.assertEqual(tuple(tool.name for tool in tools), LEGACY_MCP_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
