"""Unit tests for the list_open_items tool via the FastMCP in-memory client.

Coverage:
  - Merges open Drive comments and Docs suggestions into one response.
  - Comments are labeled scope='document'; suggestions carry tab_id.
  - tab_id provided: only suggestions for that tab are returned.
  - tab_id omitted: suggestions for all tabs are merged.
  - Open comments present but no suggestions: returns empty suggestions list.
  - Suggestions present but no open comments: returns empty comments list.
  - list_open_items returns the doc_id in the response.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from googledocs_mcp.server import mcp


# ---------------------------------------------------------------------------
# Document / comment builders
# ---------------------------------------------------------------------------


def _tab_doc_with_suggestion(
    tab_id: str = "tab-1",
    suggestion_id: str = "s-001",
    text: str = "suggested word",
) -> dict[str, Any]:
    """Minimal Docs API document with one suggested insertion in a single tab."""
    raw_text = text + "\n"
    end = 1 + len(raw_text)
    return {
        "documentId": "doc-1",
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": "Tab", "index": 0},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": end,
                                            "textRun": {
                                                "content": raw_text,
                                                "suggestedInsertionIds": [suggestion_id],
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }


def _two_tab_doc(
    tab_a: str = "tab-a",
    tab_b: str = "tab-b",
    suggestion_a: str = "s-a",
    suggestion_b: str = "s-b",
) -> dict[str, Any]:
    """Document with two tabs, one suggested insertion per tab."""

    def _tab(tab_id: str, sid: str, text: str) -> dict[str, Any]:
        raw = text + "\n"
        end = 1 + len(raw)
        return {
            "tabProperties": {"tabId": tab_id, "title": tab_id, "index": 0},
            "documentTab": {
                "body": {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {
                                        "startIndex": 1,
                                        "endIndex": end,
                                        "textRun": {
                                            "content": raw,
                                            "suggestedInsertionIds": [sid],
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            },
            "childTabs": [],
        }

    return {
        "documentId": "doc-2",
        "revisionId": "rev-1",
        "tabs": [
            _tab(tab_a, suggestion_a, "Alpha text"),
            _tab(tab_b, suggestion_b, "Beta text"),
        ],
    }


def _no_suggestion_doc(tab_id: str = "tab-1") -> dict[str, Any]:
    raw = "no suggestions here\n"
    end = 1 + len(raw)
    return {
        "documentId": "doc-clean",
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": "Tab", "index": 0},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": end,
                                            "textRun": {"content": raw},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }


def _raw_comment(
    comment_id: str = "c-001",
    resolved: bool = False,
    content: str = "Please fix this",
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "content": content,
        "resolved": resolved,
        "quotedFileContent": {"mimeType": "text/plain", "value": "some text"},
        "author": {"displayName": "Bob"},
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "replies": [],
    }


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_patches(
    doc: dict[str, Any],
    drive_comments: list[dict[str, Any]] | None = None,
) -> tuple:
    """Return patches for list_open_items tool calls.

    Mocks: get_credentials, build_docs_service, build_drive_service,
    the Drive comments.list call, and the docs.get call (for SUGGESTIONS_INLINE).
    """
    if drive_comments is None:
        drive_comments = []

    mock_creds = MagicMock()
    mock_docs_svc = MagicMock()
    mock_drive_svc = MagicMock()

    # docs.get with suggestionsViewMode returns the document.
    mock_docs_svc.documents.return_value.get.return_value.execute.return_value = doc

    # drive.comments.list returns the comments (one page).
    mock_drive_svc.comments.return_value.list.return_value.execute.return_value = {
        "comments": drive_comments,
        "nextPageToken": None,
    }

    p_creds = patch("googledocs_mcp.server.get_credentials", return_value=mock_creds)
    p_docs = patch("googledocs_mcp.server.build_docs_service", return_value=mock_docs_svc)
    p_drive = patch("googledocs_mcp.server.build_drive_service", return_value=mock_drive_svc)
    return p_creds, p_docs, p_drive


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListOpenItemsMerge:
    @pytest.mark.asyncio
    async def test_merges_comments_and_suggestions(self) -> None:
        """Response includes both open_comments and pending_suggestions."""
        doc = _tab_doc_with_suggestion(tab_id="tab-1", suggestion_id="s-1")
        comments = [_raw_comment(comment_id="c-1")]
        p1, p2, p3 = _make_patches(doc, drive_comments=comments)
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool("list_open_items", {"doc_id": "doc-1"})
        assert not result.is_error
        data = result.data
        assert "open_comments" in data
        assert "pending_suggestions" in data
        assert len(data["open_comments"]) == 1
        assert len(data["pending_suggestions"]) >= 1

    @pytest.mark.asyncio
    async def test_doc_id_in_response(self) -> None:
        doc = _no_suggestion_doc()
        p1, p2, p3 = _make_patches(doc)
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool("list_open_items", {"doc_id": "doc-clean"})
        assert result.data["doc_id"] == "doc-clean"


class TestListOpenItemsComments:
    @pytest.mark.asyncio
    async def test_open_comments_labeled_scope_document(self) -> None:
        doc = _no_suggestion_doc()
        comments = [_raw_comment(comment_id="c-99")]
        p1, p2, p3 = _make_patches(doc, drive_comments=comments)
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool("list_open_items", {"doc_id": "doc-clean"})
        data = result.data
        assert len(data["open_comments"]) == 1
        assert data["open_comments"][0]["scope"] == "document"

    @pytest.mark.asyncio
    async def test_resolved_comments_excluded(self) -> None:
        doc = _no_suggestion_doc()
        # One resolved, one open
        comments = [
            _raw_comment(comment_id="c-resolved", resolved=True),
            _raw_comment(comment_id="c-open", resolved=False),
        ]
        p1, p2, p3 = _make_patches(doc, drive_comments=comments)
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool("list_open_items", {"doc_id": "doc-clean"})
        open_ids = [c["comment_id"] for c in result.data["open_comments"]]
        assert "c-open" in open_ids
        assert "c-resolved" not in open_ids

    @pytest.mark.asyncio
    async def test_no_comments_returns_empty_list(self) -> None:
        doc = _no_suggestion_doc()
        p1, p2, p3 = _make_patches(doc, drive_comments=[])
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool("list_open_items", {"doc_id": "doc-clean"})
        assert result.data["open_comments"] == []


class TestListOpenItemsSuggestions:
    @pytest.mark.asyncio
    async def test_suggestions_carry_tab_id(self) -> None:
        doc = _tab_doc_with_suggestion(tab_id="tab-1", suggestion_id="s-42")
        p1, p2, p3 = _make_patches(doc)
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "list_open_items", {"doc_id": "doc-1", "tab_id": "tab-1"}
                )
        suggestions = result.data["pending_suggestions"]
        assert len(suggestions) >= 1
        assert all(s["tab_id"] == "tab-1" for s in suggestions)

    @pytest.mark.asyncio
    async def test_tab_id_filters_suggestions(self) -> None:
        """When tab_id is supplied, only suggestions from that tab are returned."""
        doc = _two_tab_doc(tab_a="tab-a", tab_b="tab-b", suggestion_a="s-a", suggestion_b="s-b")
        p1, p2, p3 = _make_patches(doc)
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "list_open_items", {"doc_id": "doc-2", "tab_id": "tab-a"}
                )
        suggestion_ids = [s["suggestion_id"] for s in result.data["pending_suggestions"]]
        assert "s-a" in suggestion_ids
        assert "s-b" not in suggestion_ids

    @pytest.mark.asyncio
    async def test_no_tab_id_merges_all_tabs(self) -> None:
        """When tab_id is omitted, suggestions from all tabs are merged."""
        doc = _two_tab_doc(tab_a="tab-a", tab_b="tab-b", suggestion_a="s-a", suggestion_b="s-b")
        p1, p2, p3 = _make_patches(doc)
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool("list_open_items", {"doc_id": "doc-2"})
        suggestion_ids = [s["suggestion_id"] for s in result.data["pending_suggestions"]]
        assert "s-a" in suggestion_ids
        assert "s-b" in suggestion_ids

    @pytest.mark.asyncio
    async def test_no_suggestions_returns_empty_list(self) -> None:
        doc = _no_suggestion_doc()
        p1, p2, p3 = _make_patches(doc, drive_comments=[])
        with p1, p2, p3:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "list_open_items", {"doc_id": "doc-clean", "tab_id": "tab-1"}
                )
        assert result.data["pending_suggestions"] == []
