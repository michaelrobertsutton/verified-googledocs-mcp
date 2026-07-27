"""Contract guard: the registered tool surface must match its snapshot.

Catches accidental tool renames, additions, or drops — including the case
where a tool's public name silently diverges from what clients and the README
expect (this happened once with ``get_comment_thread``). Runs fully offline
against the in-memory FastMCP client.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from verified_googledocs_mcp.middleware import MUTATING_TOOLS
from verified_googledocs_mcp.server import mcp

# The 19 tools that make up the public surface. Updating this set is a
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
    "list_tables",
    "get_table",
    "replace_table_row",
    "insert_table",
    "export_pdf",
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


class TestOutputSchemaContract:
    """Guards the assumption EvidenceEnforcementMiddleware depends on
    (middleware.py:74-76): it reads evidence keys off the TOP LEVEL of
    structured_content, which only holds while a tool's output schema stays
    object-typed and FastMCP doesn't wrap the return under a ``result`` key.

    Companion guards: tests/unit/test_middleware.py
    ::TestStructuredContentShapeAssumption pins the FastMCP behaviour this
    relies on; tests/unit/fixtures/evidence.py::assert_top_level_evidence is
    used at real mutating-tool call sites to prove the contract holds on
    live results, not just on the manifest.
    """

    @pytest.mark.asyncio
    async def test_every_tool_exposes_an_object_output_schema(self) -> None:
        for tool in await _list_tools():
            schema = tool.outputSchema
            assert isinstance(schema, dict), f"{tool.name} has no output schema"
            assert schema.get("type") == "object", (
                f"{tool.name} output schema is not object-typed: {schema!r} — "
                "a non-object schema gets wrapped by FastMCP under a 'result' "
                "key, which the evidence-enforcement middleware cannot see"
            )

    @pytest.mark.asyncio
    async def test_mutating_tools_are_not_wrapped(self) -> None:
        tools = {t.name: t for t in await _list_tools()}
        for name in MUTATING_TOOLS:
            assert name in tools, f"{name!r} is in MUTATING_TOOLS but not registered"
            schema = tools[name].outputSchema or {}
            assert not schema.get("x-fastmcp-wrap-result"), (
                f"{name!r} output schema carries FastMCP's wrap marker — its "
                "evidence would be nested under 'result' and the middleware "
                "would stop seeing it"
            )
            # Forward guard: today's schema has no 'properties' key at all
            # (see the additionalProperties-style schema every tool emits), so
            # this cannot fail yet. If FastMCP or a future annotation change
            # starts emitting 'properties', this is what catches the
            # single-'result'-key wrap shape. test_middleware.py's FastMCP
            # pin is what keeps this clause from going silently vacuous.
            properties = schema.get("properties")
            if properties is not None:
                assert set(properties.keys()) != {"result"}, (
                    f"{name!r} output schema's only property is 'result' — "
                    "this is the wrapped-result shape"
                )

    @pytest.mark.asyncio
    async def test_mutating_tools_are_all_registered(self) -> None:
        """A tool renamed or removed in server.py but left in MUTATING_TOOLS
        would silently stop being enforced. Every entry in the middleware's
        registry must correspond to an actually-registered tool."""
        names = {t.name for t in await _list_tools()}
        assert MUTATING_TOOLS <= names, MUTATING_TOOLS - names
