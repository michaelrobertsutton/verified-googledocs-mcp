"""Unit tests for server.py using FastMCP's in-memory test client.

All Google API calls are mocked; no network or credentials required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from verified_googledocs_mcp.server import mcp
from tests.unit.fixtures.docs_api import (
    multi_tab_doc,
    nested_tabs_doc,
    tabless_doc,
)


def _mock_fetch(doc: dict[str, Any]):
    """Return a patcher that mocks _get_service and fetch_document."""

    def _fake_get_credentials():
        return MagicMock()

    def _fake_fetch(service, doc_id):
        return doc

    return patch("verified_googledocs_mcp.server.get_credentials", _fake_get_credentials), \
           patch("verified_googledocs_mcp.server.fetch_document", _fake_fetch)


class TestListTabsTool:
    @pytest.mark.asyncio
    async def test_returns_tab_ids(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool("list_tabs", {"doc_id": "doc-multi-tab"})
        data = result.data
        assert isinstance(data, dict)
        tab_ids = {t["tab_id"] for t in data["tabs"]}
        assert tab_ids == {"tab-1", "tab-2"}

    @pytest.mark.asyncio
    async def test_tabless_doc_returns_implicit_tab(self) -> None:
        doc = tabless_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool("list_tabs", {"doc_id": "doc-tabless"})
        data = result.data
        assert len(data["tabs"]) == 1
        assert data["tabs"][0]["tab_id"] == "_body"

    @pytest.mark.asyncio
    async def test_nested_tabs_structure_returned(self) -> None:
        doc = nested_tabs_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool("list_tabs", {"doc_id": "doc-nested"})
        data = result.data
        assert data["tabs"][0]["tab_id"] == "parent-tab"
        assert data["tabs"][0]["child_tabs"][0]["tab_id"] == "child-tab"


class TestReadDocumentTool:
    @pytest.mark.asyncio
    async def test_markdown_format_default(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "read_document", {"doc_id": "doc-multi-tab", "tab_id": "tab-1"}
                )
        data = result.data
        assert data["format"] == "markdown"
        assert "Introduction" in data["content"]

    @pytest.mark.asyncio
    async def test_structured_format(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "read_document",
                    {"doc_id": "doc-multi-tab", "tab_id": "tab-1", "format": "structured"},
                )
        data = result.data
        assert data["format"] == "structured"
        assert "paragraphs" in data["content"]

    @pytest.mark.asyncio
    async def test_revision_id_in_response(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "read_document", {"doc_id": "doc-multi-tab", "tab_id": "tab-1"}
                )
        assert result.data["revision_id"] == "rev-001"

    @pytest.mark.asyncio
    async def test_unknown_tab_returns_error(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "read_document",
                    {"doc_id": "doc-multi-tab", "tab_id": "bad-tab"},
                    raise_on_error=False,
                )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_tabless_doc_readable(self) -> None:
        doc = tabless_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "read_document", {"doc_id": "doc-tabless", "tab_id": "_body"}
                )
        data = result.data
        assert "Legacy Document" in data["content"]

    @pytest.mark.asyncio
    async def test_lossy_elements_key_present_when_nonempty(self) -> None:
        from tests.unit.fixtures.docs_api import lossy_elements_doc
        doc = lossy_elements_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "read_document", {"doc_id": "doc-lossy", "tab_id": "tab-main"}
                )
        data = result.data
        assert "lossy_elements" in data
        assert len(data["lossy_elements"]) >= 3

    @pytest.mark.asyncio
    async def test_no_lossy_elements_key_for_plain_doc(self) -> None:
        doc = tabless_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "read_document", {"doc_id": "doc-tabless", "tab_id": "_body"}
                )
        # lossy_elements should be absent or empty for a doc with no lossy content.
        data = result.data
        lossy = data.get("lossy_elements", [])
        assert lossy == []


class TestFindSectionsTool:
    @pytest.mark.asyncio
    async def test_finds_heading(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "find_sections",
                    {"doc_id": "doc-multi-tab", "heading": "Introduction", "tab_id": "tab-1"},
                )
        data = result.data
        assert len(data["matches"]) == 1
        assert data["matches"][0]["matched_text"] == "Introduction"

    @pytest.mark.asyncio
    async def test_returns_revision_stamp(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "find_sections",
                    {"doc_id": "doc-multi-tab", "heading": "Introduction", "tab_id": "tab-1"},
                )
        m = result.data["matches"][0]
        assert m["computed_at_revision"] == "rev-001"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty_list(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "find_sections",
                    {"doc_id": "doc-multi-tab", "heading": "Nonexistent", "tab_id": "tab-1"},
                )
        assert result.data["matches"] == []

    @pytest.mark.asyncio
    async def test_tab_scoping(self) -> None:
        # "Results" is only in tab-2.
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result1 = await client.call_tool(
                    "find_sections",
                    {"doc_id": "doc-multi-tab", "heading": "Results", "tab_id": "tab-1"},
                )
                result2 = await client.call_tool(
                    "find_sections",
                    {"doc_id": "doc-multi-tab", "heading": "Results", "tab_id": "tab-2"},
                )
        assert result1.data["matches"] == []
        assert len(result2.data["matches"]) == 1

    @pytest.mark.asyncio
    async def test_unknown_tab_returns_error(self) -> None:
        doc = multi_tab_doc()
        p1, p2 = _mock_fetch(doc)
        with p1, p2:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "find_sections",
                    {"doc_id": "doc-multi-tab", "heading": "Anything", "tab_id": "bad-tab"},
                    raise_on_error=False,
                )
        assert result.is_error
