from __future__ import annotations

import unittest

from mcp_server.tcia_query_mcp.server import (
    LEGACY_MCP_TOOL_NAMES,
    PUBLIC_V2_TOOL_NAMES,
    mcp,
)


class McpToolSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_surface_advertises_only_supported_v2_tools(self) -> None:
        tools = await mcp.list_tools()
        names = tuple(tool.name for tool in tools)

        self.assertEqual(names, PUBLIC_V2_TOOL_NAMES)
        self.assertTrue(set(names).isdisjoint(LEGACY_MCP_TOOL_NAMES))

    async def test_participant_tools_advertise_data_facets_and_geometry(self) -> None:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        search_properties = tools["search_participants"].inputSchema["properties"]
        asset_properties = tools["get_participant_assets"].inputSchema["properties"]
        for name in (
            "data_categories",
            "data_types",
            "file_formats",
            "geometry_statuses",
        ):
            self.assertIn(name, search_properties)
            self.assertIn(name, asset_properties)
        public_properties = tools["find_public_non_dicom_assets"].inputSchema["properties"]
        self.assertIn("modalities", public_properties)
        self.assertIn("geometry_statuses", public_properties)

    async def test_public_tools_advertise_safe_structured_contracts(self) -> None:
        tools = await mcp.list_tools()
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertFalse(tool.annotations.destructiveHint)
                self.assertTrue(tool.annotations.idempotentHint)
                self.assertFalse(tool.annotations.openWorldHint)
                self.assertEqual(tool.meta["snapshot_local"], True)
                self.assertIsNotNone(tool.outputSchema)
                self.assertNotIn("include_hidden", tool.inputSchema.get("properties", {}))

        by_name = {tool.name: tool for tool in tools}
        for name in (
            "search_datasets",
            "search_participants",
            "get_current_downloads",
            "get_participant_assets",
            "find_public_non_dicom_assets",
            "get_dataset_v1_releases",
        ):
            self.assertIn("cursor", by_name[name].inputSchema["properties"])


if __name__ == "__main__":
    unittest.main()
