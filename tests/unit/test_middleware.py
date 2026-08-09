"""Unit tests for EvidenceEnforcementMiddleware.

Verifies that:
  - Mutating tools that return evidence pass through.
  - Mutating tools that return no evidence raise.
  - Non-mutating tools always pass through.
  - Tools that raise ToolError (VerifyError surface) pass through.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from verified_googledocs_mcp.middleware import EvidenceEnforcementMiddleware, MUTATING_TOOLS
from tests.unit.fixtures.evidence import assert_top_level_evidence


# ---------------------------------------------------------------------------
# Isolated test server (avoids coupling to the real mcp instance)
# ---------------------------------------------------------------------------


def _make_test_server() -> FastMCP:
    """Create a minimal FastMCP server with enforcement middleware."""
    test_mcp = FastMCP("test-server")
    test_mcp.add_middleware(EvidenceEnforcementMiddleware())
    return test_mcp


# ---------------------------------------------------------------------------
# MUTATING_TOOLS registry
# ---------------------------------------------------------------------------


class TestMutatingToolsRegistry:
    def test_replace_text_in_registry(self) -> None:
        assert "replace_text" in MUTATING_TOOLS

    def test_format_text_in_registry(self) -> None:
        assert "format_text" in MUTATING_TOOLS

    def test_list_tabs_not_in_registry(self) -> None:
        assert "list_tabs" not in MUTATING_TOOLS

    def test_read_document_not_in_registry(self) -> None:
        assert "read_document" not in MUTATING_TOOLS

    def test_replace_table_row_in_registry(self) -> None:
        assert "replace_table_row" in MUTATING_TOOLS

    def test_insert_table_in_registry(self) -> None:
        assert "insert_table" in MUTATING_TOOLS

    def test_list_tables_not_in_registry(self) -> None:
        assert "list_tables" not in MUTATING_TOOLS

    def test_get_table_not_in_registry(self) -> None:
        assert "get_table" not in MUTATING_TOOLS

    def test_export_pdf_not_in_registry(self) -> None:
        assert "export_pdf" not in MUTATING_TOOLS


# ---------------------------------------------------------------------------
# Middleware enforcement on a stub server
# ---------------------------------------------------------------------------


class TestEvidenceEnforcement:
    @pytest.mark.asyncio
    async def test_mutating_tool_with_evidence_passes(self) -> None:
        """A mutating tool that returns evidence passes through."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="replace_text")
        def good_mutating() -> dict:
            return {"applied": True, "match_count": 1, "rung": "exact"}

        async with Client(test_mcp) as client:
            result = await client.call_tool("replace_text", {})
        assert not result.is_error
        assert result.data["applied"] is True

    @pytest.mark.asyncio
    async def test_mutating_tool_without_evidence_raises(self) -> None:
        """A mutating tool that returns no evidence key causes a RuntimeError."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="replace_text")
        def bad_mutating() -> dict:
            return {"some_key": "some_value"}  # no 'applied' or 'error_code'

        with pytest.raises(Exception):
            async with Client(test_mcp) as client:
                await client.call_tool("replace_text", {})

    @pytest.mark.asyncio
    async def test_non_mutating_tool_passes_without_evidence(self) -> None:
        """A non-mutating tool can return anything without enforcement."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="list_tabs")
        def non_mutating() -> dict:
            return {"tabs": []}

        async with Client(test_mcp) as client:
            result = await client.call_tool("list_tabs", {})
        assert not result.is_error
        assert result.data["tabs"] == []

    @pytest.mark.asyncio
    async def test_mutating_tool_with_error_code_passes(self) -> None:
        """A mutating tool that returns error_code (VerifyError surface) passes."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="replace_text")
        def error_returning() -> dict:
            return {
                "error_code": "ZERO_MATCH",
                "message": "not found",
                "diagnostics": {},
                "retryable": False,
            }

        async with Client(test_mcp) as client:
            result = await client.call_tool("replace_text", {})
        assert not result.is_error
        assert result.data["error_code"] == "ZERO_MATCH"

    @pytest.mark.asyncio
    async def test_mutating_tool_that_raises_tool_error_passes(self) -> None:
        """A mutating tool that raises ToolError is a typed failure — passes through."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="replace_text")
        def raises_tool_error() -> dict:
            raise ToolError("ZERO_MATCH: not found")

        async with Client(test_mcp) as client:
            result = await client.call_tool("replace_text", {}, raise_on_error=False)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_unknown_mutating_tool_without_evidence_raises(self) -> None:
        """A tool not in MUTATING_TOOLS is not enforced regardless of name."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="some_read_tool")
        def random_read() -> dict:
            return {"foo": "bar"}

        async with Client(test_mcp) as client:
            result = await client.call_tool("some_read_tool", {})
        # Not in MUTATING_TOOLS, so enforcement does not apply.
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_multiple_non_mutating_calls_all_pass(self) -> None:
        """Multiple non-mutating calls within a single session all pass."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="read_document")
        def read() -> dict:
            return {"content": "some text", "format": "markdown"}

        @test_mcp.tool(name="find_sections")
        def sections() -> dict:
            return {"matches": []}

        async with Client(test_mcp) as client:
            r1 = await client.call_tool("read_document", {})
            r2 = await client.call_tool("find_sections", {})
        assert not r1.is_error
        assert not r2.is_error


# ---------------------------------------------------------------------------
# Structured-content shape assumption (issue #58)
#
# EvidenceEnforcementMiddleware only works while FastMCP hands back the bare
# tool dict as structured_content — see middleware.py:74-79. FastMCP wraps a
# tool's return under a single "result" key whenever the inferred output
# schema is not object-typed. Every real tool here returns dict[str, Any],
# which infers an object schema, so nothing is wrapped today. These tests pin
# that FastMCP behaviour so a future FastMCP version (or a narrowed return
# annotation) that starts wrapping trips a test here, not a silent regression
# in production. See also test_tool_manifest.py::TestOutputSchemaContract
# (the manifest-level guard this pins) and
# fixtures/evidence.py::assert_top_level_evidence (used at real mutating-tool
# call sites).
# ---------------------------------------------------------------------------


class TestStructuredContentShapeAssumption:
    @pytest.mark.asyncio
    async def test_dict_return_stays_unwrapped_and_satisfies_middleware(self) -> None:
        """The exact annotation every mutating tool in this server uses:
        dict[str, Any]. Registered under a MUTATING_TOOLS name so the
        middleware actually runs — proves the contract on a real enforced
        call, not just on FastMCP's client-side result shape."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="replace_text")
        def dict_tool() -> dict[str, Any]:
            return {"applied": True, "match_count": 1}

        async with Client(test_mcp) as client:
            result = await client.call_tool("replace_text", {})
        assert not result.is_error
        assert_top_level_evidence(result)

    @pytest.mark.asyncio
    async def test_non_object_return_gets_wrapped_by_fastmcp(self) -> None:
        """Canary for the hazard test_tool_manifest.py guards against: if
        FastMCP ever changes how it marks or shapes a wrapped result, this
        test — not the manifest guard — is what fails and names the reason.
        Registered under a non-mutating name deliberately: middleware
        enforcement is not the point here, FastMCP's own schema inference is."""
        test_mcp = _make_test_server()

        @test_mcp.tool(name="list_tabs")
        def list_tool() -> list[str]:
            return ["applied"]

        async with Client(test_mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            result = await client.call_tool("list_tabs", {})
        assert not result.is_error
        assert result.structured_content == {"result": ["applied"]}
        assert tools["list_tabs"].outputSchema is not None
        assert tools["list_tabs"].outputSchema.get("x-fastmcp-wrap-result") is True
