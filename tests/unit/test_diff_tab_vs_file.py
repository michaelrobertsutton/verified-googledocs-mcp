"""Unit tests for diff_tab_vs_file tool.

Tests:
- Returns a structured diff when tab and file differ.
- Returns identical=True when tab and file are the same.
- Returns TAB_NOT_FOUND when the tab does not exist.
- Returns INVALID_INPUT when the file does not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from googledocs_mcp.server import mcp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _para(text: str, start: int, end: int | None = None) -> dict[str, Any]:
    if end is None:
        end = start + len(text)
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text},
                }
            ],
        },
    }


def _heading_para(level: int, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": f"HEADING_{level}"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text + "\n"},
                }
            ],
        },
    }


def _doc_with_content(content_elements: list[dict[str, Any]], revision: str = "rev-1") -> dict[str, Any]:
    return {
        "documentId": "doc-diff",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": {"content": content_elements}},
                "childTabs": [],
            }
        ],
    }


def _mock_fetch_doc(doc: dict[str, Any]):
    def _fake_get_creds():
        return MagicMock()

    def _fake_build_service(_creds: Any) -> Any:
        return MagicMock()

    def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
        return doc

    return [
        patch("googledocs_mcp.server.get_credentials", _fake_get_creds),
        patch("googledocs_mcp.server.build_docs_service", _fake_build_service),
        patch("googledocs_mcp.server.fetch_document", _fake_fetch),
        patch("googledocs_mcp.markdown_mutations.fetch_document", _fake_fetch),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiffTabVsFile:
    @pytest.mark.asyncio
    async def test_identical_returns_identical_true(self, tmp_path: Path) -> None:
        doc = _doc_with_content([
            _heading_para(1, "Hello", 1, 8),
        ])
        # Export the doc to find what markdown it produces, then match that exactly.
        # The tab will produce "# Hello\n" via to_markdown.
        file = tmp_path / "test.md"
        file.write_text("# Hello\n", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(file),
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["identical"] is True
        assert "hunks" in data
        assert "unified_diff" in data

    @pytest.mark.asyncio
    async def test_different_content_returns_diff(self, tmp_path: Path) -> None:
        doc = _doc_with_content([
            _para("Hello world\n", 1, 13),
        ])
        file = tmp_path / "test.md"
        file.write_text("Hello planet\n", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(file),
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["identical"] is False
        assert len(data["hunks"]) > 0
        # At least one hunk should be replace or delete/insert
        tags = {h["tag"] for h in data["hunks"]}
        assert tags - {"equal"} != set()  # some non-equal hunks

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self, tmp_path: Path) -> None:
        doc = _doc_with_content([_para("Hello\n", 1, 7)])
        file = tmp_path / "test.md"
        file.write_text("content", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "missing-tab",
                        "file_path": str(file),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "TAB_NOT_FOUND" in str(result.content)

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(self, tmp_path: Path) -> None:
        doc = _doc_with_content([_para("Hello\n", 1, 7)])
        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(tmp_path / "nonexistent.md"),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_result_contains_metadata(self, tmp_path: Path) -> None:
        doc = _doc_with_content([_para("Hello\n", 1, 7)])
        file = tmp_path / "test.md"
        file.write_text("Hello\n", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(file),
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["doc_id"] == "doc-diff"
        assert data["tab_id"] == "tab-1"
        assert data["file_path"] == str(file)
        assert data["revision_id"] == "rev-1"

    @pytest.mark.asyncio
    async def test_diff_is_not_read_only_blocked(self) -> None:
        """diff_tab_vs_file is a READ tool and should not be in MUTATING_TOOLS."""
        from googledocs_mcp.middleware import MUTATING_TOOLS

        assert "diff_tab_vs_file" not in MUTATING_TOOLS
