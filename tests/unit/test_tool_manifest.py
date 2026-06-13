"""Contract guard: the registered tool surface must match its snapshot.

Catches accidental tool renames, additions, or drops — including the case
where a tool's public name silently diverges from what clients and the README
expect (this happened once with ``get_comment_thread``). Runs fully offline
against the in-memory FastMCP client.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from verified_googledocs_mcp.server import mcp

# The 14 tools that make up the public surface. Updating this set is a
# deliberate act: a rename, addition, or removal must be reflected here, which
# is the point — the test fails loudly when the surface drifts unintentionally.
EXPECTED_TOOLS = {
    "read_document",
    "list_tabs",
    "find_sections",
    "replace_text",
    "replace_range_markdown",
    "replace_tab_markdown",
    "append_markdown",
    "insert_image",
    "list_open_items",
    "get_comment_thread",
    "add_anchored_comment",
    "reply_to_comment",
    "resolve_comment",
    "diff_tab_vs_file",
}


async def _list_tools():
    async with Client(mcp) as client:
        return await client.list_tools()


class TestToolManifest:
    @pytest.mark.asyncio
    async def test_exact_tool_set(self) -> None:
        names = {t.name for t in await _list_tools()}
        assert names == EXPECTED_TOOLS

    @pytest.mark.asyncio
    async def test_tool_count(self) -> None:
        assert len(await _list_tools()) == len(EXPECTED_TOOLS)

    @pytest.mark.asyncio
    async def test_every_tool_has_a_description(self) -> None:
        for tool in await _list_tools():
            assert tool.description and tool.description.strip(), f"{tool.name} has no description"

    @pytest.mark.asyncio
    async def test_every_tool_exposes_an_input_schema(self) -> None:
        for tool in await _list_tools():
            schema = tool.inputSchema
            assert isinstance(schema, dict), f"{tool.name} has no input schema"
            assert "properties" in schema, f"{tool.name} input schema has no properties"
