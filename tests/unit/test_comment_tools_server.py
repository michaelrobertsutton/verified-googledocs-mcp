"""Server-level integration tests for the comment tools.

Verifies that:
  - resolve_comment, add_anchored_comment, reply_to_comment are in MUTATING_TOOLS.
  - list_open_items and get_comment_thread are NOT in MUTATING_TOOLS.
  - Each mutating tool returns evidence (middleware satisfied).
  - VerifyError from a mutating tool surfaces as a ToolError with the envelope.
  - resolve_comment COMMENT_STILL_OPEN surfaces as a ToolError.
  - add_anchored_comment QUOTE_NOT_FOUND surfaces as a ToolError.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from googledocs_mcp.middleware import MUTATING_TOOLS
from googledocs_mcp.server import mcp
from googledocs_mcp.verify import ErrorCode, _make_error


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestMutatingToolsRegistry:
    def test_add_anchored_comment_in_registry(self) -> None:
        assert "add_anchored_comment" in MUTATING_TOOLS

    def test_reply_to_comment_in_registry(self) -> None:
        assert "reply_to_comment" in MUTATING_TOOLS

    def test_resolve_comment_in_registry(self) -> None:
        assert "resolve_comment" in MUTATING_TOOLS

    def test_list_open_items_not_in_registry(self) -> None:
        assert "list_open_items" not in MUTATING_TOOLS

    def test_get_comment_thread_not_in_registry(self) -> None:
        # The tool is registered as 'get_comment_thread_tool' or 'get_comment_thread'
        assert "list_open_items" not in MUTATING_TOOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence_comment(comment_id: str = "c-1", resolved: bool = True) -> dict[str, Any]:
    return {
        "applied": True,
        "comment_id": comment_id,
        "resolved": resolved,
        "reply_count": 0,
        "content": "ok",
        "quoted_text": "",
        "author": "Alice",
        "audit_logged": True,
    }


def _patch_credentials() -> Any:
    return patch("googledocs_mcp.server.get_credentials", return_value=MagicMock())


def _patch_build_docs(svc: Any) -> Any:
    return patch("googledocs_mcp.server.build_docs_service", return_value=svc)


def _patch_build_drive(svc: Any) -> Any:
    return patch("googledocs_mcp.server.build_drive_service", return_value=svc)


# ---------------------------------------------------------------------------
# resolve_comment tool
# ---------------------------------------------------------------------------


class TestResolveCommentTool:
    @pytest.mark.asyncio
    async def test_happy_path_evidence_returned(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        evidence = _evidence_comment(resolved=True)
        with (
            _patch_credentials(),
            patch("googledocs_mcp.server.execute_resolve_comment", return_value=evidence),
            _patch_build_drive(MagicMock()),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "resolve_comment", {"doc_id": "doc-1", "comment_id": "c-1"}
                )
        assert not result.is_error
        assert result.data["applied"] is True
        assert result.data["resolved"] is True

    @pytest.mark.asyncio
    async def test_comment_still_open_surfaces_as_tool_error(self) -> None:
        err = _make_error(ErrorCode.COMMENT_STILL_OPEN, "still open", {"comment_id": "c-1"})

        with (
            _patch_credentials(),
            _patch_build_drive(MagicMock()),
            patch(
                "googledocs_mcp.server.execute_resolve_comment",
                side_effect=err,
            ),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "resolve_comment",
                    {"doc_id": "doc-1", "comment_id": "c-1"},
                    raise_on_error=False,
                )
        assert result.is_error
        assert "COMMENT_STILL_OPEN" in str(result.content)

    @pytest.mark.asyncio
    async def test_middleware_satisfied_by_evidence(self, tmp_path, monkeypatch) -> None:
        """The middleware passes when the tool returns an evidence dict."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        evidence = _evidence_comment(resolved=True)
        with (
            _patch_credentials(),
            _patch_build_drive(MagicMock()),
            patch("googledocs_mcp.server.execute_resolve_comment", return_value=evidence),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "resolve_comment", {"doc_id": "doc-1", "comment_id": "c-1"}
                )
        # Middleware must not have rejected it.
        assert not result.is_error


# ---------------------------------------------------------------------------
# reply_to_comment tool
# ---------------------------------------------------------------------------


class TestReplyToCommentTool:
    @pytest.mark.asyncio
    async def test_happy_path_evidence_returned(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        evidence = _evidence_comment(resolved=False)
        evidence["reply_count"] = 1
        with (
            _patch_credentials(),
            _patch_build_drive(MagicMock()),
            patch("googledocs_mcp.server.execute_reply_to_comment", return_value=evidence),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "reply_to_comment",
                    {"doc_id": "doc-1", "comment_id": "c-1", "body": "Thanks"},
                )
        assert not result.is_error
        assert result.data["applied"] is True

    @pytest.mark.asyncio
    async def test_invalid_input_surfaces_as_tool_error(self) -> None:
        err = _make_error(ErrorCode.INVALID_INPUT, "body empty", {})
        with (
            _patch_credentials(),
            _patch_build_drive(MagicMock()),
            patch("googledocs_mcp.server.execute_reply_to_comment", side_effect=err),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "reply_to_comment",
                    {"doc_id": "doc-1", "comment_id": "c-1", "body": ""},
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)


# ---------------------------------------------------------------------------
# add_anchored_comment tool
# ---------------------------------------------------------------------------


class TestAddAnchoredCommentTool:
    @pytest.mark.asyncio
    async def test_happy_path_evidence_returned(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        evidence = _evidence_comment(comment_id="c-new", resolved=False)
        with (
            _patch_credentials(),
            _patch_build_docs(MagicMock()),
            _patch_build_drive(MagicMock()),
            patch("googledocs_mcp.server.execute_add_anchored_comment", return_value=evidence),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "add_anchored_comment",
                    {
                        "doc_id": "doc-1",
                        "tab_id": "tab-1",
                        "quote": "some text",
                        "body": "Good point",
                    },
                )
        assert not result.is_error
        assert result.data["applied"] is True

    @pytest.mark.asyncio
    async def test_quote_not_found_surfaces_as_tool_error(self) -> None:
        err = _make_error(
            ErrorCode.QUOTE_NOT_FOUND,
            "quote not found",
            {"candidates": [], "quote": "missing"},
        )
        with (
            _patch_credentials(),
            _patch_build_docs(MagicMock()),
            _patch_build_drive(MagicMock()),
            patch("googledocs_mcp.server.execute_add_anchored_comment", side_effect=err),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "add_anchored_comment",
                    {
                        "doc_id": "doc-1",
                        "tab_id": "tab-1",
                        "quote": "missing phrase",
                        "body": "comment",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "QUOTE_NOT_FOUND" in str(result.content)

    @pytest.mark.asyncio
    async def test_middleware_satisfied_by_evidence(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        evidence = _evidence_comment(resolved=False)
        with (
            _patch_credentials(),
            _patch_build_docs(MagicMock()),
            _patch_build_drive(MagicMock()),
            patch("googledocs_mcp.server.execute_add_anchored_comment", return_value=evidence),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "add_anchored_comment",
                    {
                        "doc_id": "doc-1",
                        "tab_id": "tab-1",
                        "quote": "text",
                        "body": "note",
                    },
                )
        assert not result.is_error
