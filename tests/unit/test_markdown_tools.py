"""Unit tests for the markdown write tools via the FastMCP in-memory client.

All Google API calls are mocked; no network or credentials required.
Pre-read and post-read documents use different revisionIds so revision_before
vs revision_after can be verified.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from verified_googledocs_mcp.server import mcp
from tests.unit.fixtures.markdown_tools import (
    doc_with_chip,
    doc_with_footnote,
    doc_with_image,
    simple_markdown_doc,
    doc_with_heading_and_table,
)


# ---------------------------------------------------------------------------
# Mock builder helpers
# ---------------------------------------------------------------------------


def _build_mock_env(pre_doc: dict[str, Any], post_doc: dict[str, Any]):
    """Return (patchers, mock_service) for markdown tools.

    fetch_document: returns pre_doc on first call, post_doc on all subsequent.
    batchUpdate: succeeds with no-op.
    """
    mock_service = MagicMock()
    mock_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

    call_count = [0]

    def _fake_get_creds():
        return MagicMock()

    def _fake_build_service(_creds: Any) -> Any:
        return mock_service

    def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
        idx = call_count[0]
        call_count[0] += 1
        return pre_doc if idx == 0 else post_doc

    patchers = [
        patch("verified_googledocs_mcp.server.get_credentials", _fake_get_creds),
        patch("verified_googledocs_mcp.server.build_docs_service", _fake_build_service),
        patch("verified_googledocs_mcp.server.fetch_document", _fake_fetch),
        patch("verified_googledocs_mcp.markdown_mutations.fetch_document", _fake_fetch),
    ]
    return patchers, mock_service


def _apply_all(patchers):
    """Context-manager that activates all patchers."""
    from contextlib import ExitStack

    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


# ---------------------------------------------------------------------------
# replace_range_markdown
# ---------------------------------------------------------------------------


class TestReplaceRangeMarkdown:
    @pytest.mark.asyncio
    async def test_happy_path_returns_evidence(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello planet", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "start_index": 1,
                        "end_index": 13,
                        "computed_at_revision": "rev-1",
                        "markdown": "Hello planet",
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        assert "revision_before" in data
        assert "revision_after" in data
        assert "audit_logged" in data

    @pytest.mark.asyncio
    async def test_stale_range_returns_error(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-2")
        post = simple_markdown_doc("Hello world", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "start_index": 1,
                        "end_index": 13,
                        "computed_at_revision": "rev-1",  # stale
                        "markdown": "New content",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "STALE_RANGE" in str(result.content)

    @pytest.mark.asyncio
    async def test_stale_range_is_retryable(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-2")
        post = simple_markdown_doc("Hello world", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "start_index": 1,
                        "end_index": 13,
                        "computed_at_revision": "rev-1",
                        "markdown": "New content",
                    },
                    raise_on_error=False,
                )
        content_str = str(result.content)
        assert "True" in content_str  # retryable: True

    @pytest.mark.asyncio
    async def test_unsupported_markdown_returns_error(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello world", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "start_index": 1,
                        "end_index": 13,
                        "computed_at_revision": "rev-1",
                        "markdown": "```python\ncode block\n```",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "UNSUPPORTED_MARKDOWN" in str(result.content)

    @pytest.mark.asyncio
    async def test_structural_guardrail_refuses_table_loss(self) -> None:
        pre = doc_with_heading_and_table(revision="rev-1")
        post = doc_with_heading_and_table(revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                # Range [15,60) contains the table; markdown has no table
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-table",
                        "tab_id": "tab-1",
                        "start_index": 15,
                        "end_index": 60,
                        "computed_at_revision": "rev-1",
                        "markdown": "Just text, no table",
                        "allow_structural_loss": False,
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_structural_guardrail_allows_with_flag(self) -> None:
        pre = doc_with_heading_and_table(revision="rev-1")
        post = simple_markdown_doc("Just text", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-table",
                        "tab_id": "tab-1",
                        "start_index": 15,
                        "end_index": 60,
                        "computed_at_revision": "rev-1",
                        "markdown": "Just text, no table",
                        "allow_structural_loss": True,
                    },
                )
        assert not result.is_error
        assert result.data["applied"] is True

    @pytest.mark.asyncio
    async def test_dry_run_no_batchupdate(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello world", revision="rev-1")
        patchers, mock_service = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "start_index": 1,
                        "end_index": 13,
                        "computed_at_revision": "rev-1",
                        "markdown": "Hello planet",
                        "dry_run": True,
                    },
                )
        assert mock_service.documents.return_value.batchUpdate.call_count == 0
        data = result.data
        assert data["applied"] is False
        assert "planned_requests" in data

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "bad-tab",
                        "start_index": 1,
                        "end_index": 10,
                        "computed_at_revision": "rev-1",
                        "markdown": "text",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "TAB_NOT_FOUND" in str(result.content)


# ---------------------------------------------------------------------------
# replace_tab_markdown
# ---------------------------------------------------------------------------


class TestReplaceTabMarkdown:
    @pytest.mark.asyncio
    async def test_happy_path_returns_evidence(self) -> None:
        pre = simple_markdown_doc("Old content", revision="rev-1")
        post = simple_markdown_doc("New content", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "New content",
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        assert data["revision_before"] == "rev-1"
        assert data["revision_after"] == "rev-2"

    @pytest.mark.asyncio
    async def test_missing_tab_id_returns_error(self) -> None:
        """replace_tab_markdown must refuse an empty tab_id."""
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "",
                        "markdown": "text",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "TAB_NOT_FOUND" in str(result.content)

    @pytest.mark.asyncio
    async def test_unsupported_markdown_returns_error(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "> blockquote text",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "UNSUPPORTED_MARKDOWN" in str(result.content)

    @pytest.mark.asyncio
    async def test_structural_guardrail_with_image_refuses(self) -> None:
        pre = doc_with_image(revision="rev-1")
        post = doc_with_image(revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-image",
                        "tab_id": "tab-1",
                        "markdown": "No image here",
                        "allow_structural_loss": False,
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_structural_guardrail_with_chip_refuses(self) -> None:
        pre = doc_with_chip(revision="rev-1")
        post = doc_with_chip(revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-chip",
                        "tab_id": "tab-1",
                        "markdown": "No chip here",
                        "allow_structural_loss": False,
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_structural_guardrail_with_footnote_refuses(self) -> None:
        pre = doc_with_footnote(revision="rev-1")
        post = doc_with_footnote(revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-footnote",
                        "tab_id": "tab-1",
                        "markdown": "No footnote here",
                        "allow_structural_loss": False,
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_structural_guardrail_allows_with_flag(self) -> None:
        pre = doc_with_image(revision="rev-1")
        post = simple_markdown_doc("No image", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-image",
                        "tab_id": "tab-1",
                        "markdown": "No image here",
                        "allow_structural_loss": True,
                    },
                )
        assert not result.is_error
        assert result.data["applied"] is True

    @pytest.mark.asyncio
    async def test_dry_run_returns_planned_requests(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, mock_service = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "# New heading\n\nNew paragraph",
                        "dry_run": True,
                    },
                )
        assert mock_service.documents.return_value.batchUpdate.call_count == 0
        data = result.data
        assert data["applied"] is False
        assert "planned_requests" in data
        assert isinstance(data["planned_requests"], int)


# ---------------------------------------------------------------------------
# append_markdown
# ---------------------------------------------------------------------------


class TestAppendMarkdown:
    @pytest.mark.asyncio
    async def test_happy_path_returns_evidence(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello world\n\nNew paragraph", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "New paragraph",
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        assert data["revision_before"] == "rev-1"
        assert data["revision_after"] == "rev-2"

    @pytest.mark.asyncio
    async def test_dry_run_no_batchupdate(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, mock_service = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "# New section",
                        "dry_run": True,
                    },
                )
        assert mock_service.documents.return_value.batchUpdate.call_count == 0
        data = result.data
        assert data["applied"] is False
        assert "planned_requests" in data

    @pytest.mark.asyncio
    async def test_unsupported_markdown_returns_error(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "inline `code` here",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "UNSUPPORTED_MARKDOWN" in str(result.content)

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "missing-tab",
                        "markdown": "text",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "TAB_NOT_FOUND" in str(result.content)

    @pytest.mark.asyncio
    async def test_append_opens_fresh_paragraph_before_content(self) -> None:
        """Fix #37: the first request must be a newline at insert_at, and compiled
        content must start at insert_at+1 so appended blocks land in a fresh
        paragraph rather than fusing with the existing trailing paragraph.

        The rendered no-fusion outcome is covered by the live test (network-gated).
        """
        # simple_markdown_doc("Hello world") → paragraph "Hello world\n" at [1,13)
        # tab_end=13, insert_at=max(1,13-1)=12, content_start=13
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello world", revision="rev-2")
        patchers, mock_service = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "## Appended",
                    },
                )
        assert not result.is_error

        call_args = mock_service.documents.return_value.batchUpdate.call_args
        sent_requests = call_args.kwargs["body"]["requests"]

        # The leading newline must be the very first request, at insert_at=12.
        first = sent_requests[0]
        assert first["insertText"]["text"] == "\n"
        assert first["insertText"]["location"]["index"] == 12

        # The first compiled request (heading insertText) must target content_start=13.
        insert_text_requests = [r for r in sent_requests[1:] if "insertText" in r]
        assert insert_text_requests[0]["insertText"]["location"]["index"] == 13


# ---------------------------------------------------------------------------
# insert_image
# ---------------------------------------------------------------------------


class TestInsertImage:
    @pytest.mark.asyncio
    async def test_happy_path_returns_structural_evidence(self) -> None:
        pre = doc_with_image(revision="rev-1")
        post = doc_with_image(revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-image",
                        "tab_id": "tab-1",
                        "anchor": "anchor text",
                        "source": "https://example.com/image.png",
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        assert "inline_object_confirmed" in data
        assert "revision_before" in data
        assert "revision_after" in data
        assert "audit_logged" in data

    @pytest.mark.asyncio
    async def test_local_path_source_returns_error(self) -> None:
        pre = doc_with_image(revision="rev-1")
        post = doc_with_image(revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-image",
                        "tab_id": "tab-1",
                        "anchor": "anchor text",
                        "source": "/home/user/image.png",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "IMAGE_SOURCE_UNSUPPORTED" in str(result.content)

    @pytest.mark.asyncio
    async def test_windows_path_source_returns_error(self) -> None:
        pre = doc_with_image(revision="rev-1")
        post = doc_with_image(revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-image",
                        "tab_id": "tab-1",
                        "anchor": "anchor text",
                        "source": "C:\\Users\\user\\image.png",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "IMAGE_SOURCE_UNSUPPORTED" in str(result.content)

    @pytest.mark.asyncio
    async def test_anchor_not_found_returns_quote_not_found(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello world", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "anchor": "nonexistent anchor text",
                        "source": "https://example.com/image.png",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "QUOTE_NOT_FOUND" in str(result.content)

    @pytest.mark.asyncio
    async def test_dry_run_returns_anchor_span(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello world", revision="rev-1")
        patchers, mock_service = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "anchor": "Hello",
                        "source": "https://example.com/image.png",
                        "dry_run": True,
                    },
                )
        assert mock_service.documents.return_value.batchUpdate.call_count == 0
        data = result.data
        assert data["applied"] is False
        assert "anchor_span" in data
        assert "insert_at" in data

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-1")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "bad-tab",
                        "anchor": "Hello",
                        "source": "https://example.com/img.png",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "TAB_NOT_FOUND" in str(result.content)


# ---------------------------------------------------------------------------
# Middleware enforcement
# ---------------------------------------------------------------------------


class TestMiddlewareEnforcement:
    """Mutating markdown tools must carry an 'applied' key in their results."""

    @pytest.mark.asyncio
    async def test_replace_range_markdown_carries_applied(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "start_index": 1,
                        "end_index": 6,
                        "computed_at_revision": "rev-1",
                        "markdown": "Hi",
                    },
                )
        assert not result.is_error
        assert "applied" in result.data

    @pytest.mark.asyncio
    async def test_replace_tab_markdown_carries_applied(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("World", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "World",
                    },
                )
        assert not result.is_error
        assert "applied" in result.data

    @pytest.mark.asyncio
    async def test_append_markdown_carries_applied(self) -> None:
        pre = simple_markdown_doc("Hello", revision="rev-1")
        post = simple_markdown_doc("Hello\nWorld", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "markdown": "World",
                    },
                )
        assert not result.is_error
        assert "applied" in result.data

    @pytest.mark.asyncio
    async def test_insert_image_carries_applied(self) -> None:
        pre = simple_markdown_doc("Hello world", revision="rev-1")
        post = simple_markdown_doc("Hello world", revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "anchor": "Hello",
                        "source": "https://example.com/img.png",
                    },
                )
        assert not result.is_error
        assert "applied" in result.data


# ---------------------------------------------------------------------------
# Secondary-tab false success (issue #48)
# ---------------------------------------------------------------------------


def _two_tab_doc(
    second_tab_content: list[dict[str, Any]], revision: str = "rev-1"
) -> dict[str, Any]:
    """Two-tab doc: a populated first tab 't.0' and a second tab 't.second'."""
    first = {
        "content": [
            {
                "startIndex": 1,
                "endIndex": 12,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [
                        {"startIndex": 1, "endIndex": 12, "textRun": {"content": "First tab.\n"}}
                    ],
                },
            }
        ]
    }
    return {
        "documentId": "doc-2tab",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "Body", "index": 0},
                "documentTab": {"body": first},
                "childTabs": [],
            },
            {
                "tabProperties": {"tabId": "t.second", "title": "Backup", "index": 1},
                "documentTab": {"body": {"content": second_tab_content}},
                "childTabs": [],
            },
        ],
    }


def _empty_tab_content() -> list[dict[str, Any]]:
    """A tab body holding only an empty paragraph (no real content)."""
    return [
        {
            "startIndex": 1,
            "endIndex": 2,
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "elements": [{"startIndex": 1, "endIndex": 2, "textRun": {"content": "\n"}}],
            },
        }
    ]


def _populated_tab_content() -> list[dict[str, Any]]:
    """A tab body holding a heading and a paragraph (a landed write)."""
    return [
        {
            "startIndex": 2,
            "endIndex": 18,
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "elements": [
                    {"startIndex": 2, "endIndex": 18, "textRun": {"content": "Backup content\n"}}
                ],
            },
        },
        {
            "startIndex": 18,
            "endIndex": 34,
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "elements": [
                    {"startIndex": 18, "endIndex": 34, "textRun": {"content": "Body paragraph.\n"}}
                ],
            },
        },
    ]


_BACKUP_MD = "## Backup content\n\nBody paragraph."


class TestSecondaryTabFalseSuccess:
    """A write the API accepted but that left the tab empty must report
    applied=false, never a false success (issue #48)."""

    @pytest.mark.asyncio
    async def test_append_to_empty_secondary_tab_reports_not_applied(self) -> None:
        pre = _two_tab_doc(_empty_tab_content(), revision="rev-1")
        post = _two_tab_doc(
            _empty_tab_content(), revision="rev-2"
        )  # still empty: write did not land
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {"doc_id": "doc-2tab", "tab_id": "t.second", "markdown": _BACKUP_MD},
                )
        assert not result.is_error
        data = result.data
        assert data["post_blocks"] == 0
        assert data["input_blocks"] > 0
        assert data["applied"] is False  # the bug was: True
        assert any("did not land" in d for d in data.get("structural_diff", []))

    @pytest.mark.asyncio
    async def test_replace_tab_to_empty_secondary_tab_reports_not_applied(self) -> None:
        pre = _two_tab_doc(_empty_tab_content(), revision="rev-1")
        post = _two_tab_doc(_empty_tab_content(), revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {"doc_id": "doc-2tab", "tab_id": "t.second", "markdown": _BACKUP_MD},
                )
        assert not result.is_error
        data = result.data
        assert data["post_blocks"] == 0
        assert data["applied"] is False

    @pytest.mark.asyncio
    async def test_append_that_lands_in_secondary_tab_is_applied(self) -> None:
        # Control: when the post-read shows the content landed, applied stays True.
        pre = _two_tab_doc(_empty_tab_content(), revision="rev-1")
        post = _two_tab_doc(_populated_tab_content(), revision="rev-2")
        patchers, _ = _build_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {"doc_id": "doc-2tab", "tab_id": "t.second", "markdown": _BACKUP_MD},
                )
        assert not result.is_error
        data = result.data
        assert data["post_blocks"] > 0
        assert data["applied"] is True
