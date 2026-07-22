"""Integration tests: the issue #56 suggestion guard fires at EVERY mutating
tool entry point, before any batchUpdate is sent.

Each of the 7 verified-write pipelines (insert_table, replace_table_row,
replace_tab_markdown, replace_range_markdown, append_markdown, insert_image,
replace_text) calls assert_no_pending_suggestions right after resolving the
target tab body, before any locate()/compile_markdown/request-building. These
tests exercise each pipeline end-to-end (through the FastMCP client for the
markdown/text tools, matching test_markdown_tools.py's/test_replace_text.py's
own harness pattern; directly for the table tools, matching test_tables.py's
pattern) against a doc whose target tab has a pending suggested insertion,
and assert:

  1. The call raises SUGGESTIONS_PRESENT.
  2. batchUpdate is never called — the write genuinely never happens, not
     just "reported as failed" (issue #56's defect 3: a mutation must not
     land silently even when verification later fails; here there must be no
     mutation attempt at all).

No network calls, no credentials — all docs are synthetic fixtures.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from verified_googledocs_mcp.server import mcp
from verified_googledocs_mcp.tables import execute_insert_table, execute_replace_table_row


# ---------------------------------------------------------------------------
# Shared doc fixtures
# ---------------------------------------------------------------------------


def _para(text: str, start: int) -> dict[str, Any]:
    end = start + len(text)
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{"startIndex": start, "endIndex": end, "textRun": {"content": text}}],
        },
    }


def _preview_doc(revision: str = "rev-1") -> dict[str, Any]:
    """A minimal single-tab, suggestion-free doc — what PREVIEW_WITHOUT_SUGGESTIONS returns."""
    return {
        "documentId": "doc-guard-test",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab One", "index": 0},
                "documentTab": {"body": {"content": [_para("Hello world\n", 1)]}},
                "childTabs": [],
            }
        ],
    }


def _inline_doc_with_suggestion(revision: str = "rev-1") -> dict[str, Any]:
    """Same tab/revision, but SUGGESTIONS_INLINE reveals a pending insertion."""
    return {
        "documentId": "doc-guard-test",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab One", "index": 0},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": 12,
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 12,
                                            "textRun": {
                                                "content": "Hello world\n",
                                                "suggestedInsertionIds": ["ins-guard-test"],
                                            },
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Table tools — direct execute_* calls (mirrors tests/unit/test_tables.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_audit_dir(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _mock_service_for_tables() -> MagicMock:
    """PREVIEW pre-read, then the guard's INLINE read (same revision, has a
    suggestion) — both via the same service.documents().get().execute() chain
    execute_replace_table_row/execute_insert_table use for every read."""
    service = MagicMock()
    service.documents.return_value.get.return_value.execute.side_effect = [
        _preview_doc(),
        _inline_doc_with_suggestion(),
    ]
    service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
    return service


class TestTableToolsBlockedBySuggestion:
    def test_insert_table_raises_and_never_writes(self) -> None:
        from verified_googledocs_mcp.verify import ErrorCode, VerifyError

        service = _mock_service_for_tables()
        with pytest.raises(VerifyError) as exc_info:
            execute_insert_table(
                service=service,
                doc_id="doc-guard-test",
                tab_id="tab-1",
                anchor="Hello world",
                rows=[["A", "B"]],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.SUGGESTIONS_PRESENT
        service.documents.return_value.batchUpdate.assert_not_called()

    def test_replace_table_row_raises_and_never_writes(self) -> None:
        from verified_googledocs_mcp.verify import ErrorCode, VerifyError

        service = _mock_service_for_tables()
        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id="doc-guard-test",
                tab_id="tab-1",
                table_index=0,
                row_index=0,
                cells=["x", "y"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.SUGGESTIONS_PRESENT
        service.documents.return_value.batchUpdate.assert_not_called()


# ---------------------------------------------------------------------------
# Markdown + text tools — via the FastMCP in-memory client (mirrors
# test_markdown_tools.py / test_replace_text.py)
# ---------------------------------------------------------------------------


def _build_client_mock_env(*, module: str):
    """Patch get_credentials/build_docs_service, fetch_document (PREVIEW) for
    both server.py and the target module, and fetch_document_inline (the
    guard's own read) for suggestions.py. Returns (patchers, mock_service)."""
    mock_service = MagicMock()
    mock_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

    def _fake_get_creds() -> Any:
        return MagicMock()

    def _fake_build_service(_creds: Any) -> Any:
        return mock_service

    def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
        return _preview_doc()

    def _fake_fetch_inline(_service: Any, _doc_id: str) -> dict[str, Any]:
        return _inline_doc_with_suggestion()

    patchers = [
        patch("verified_googledocs_mcp.server.get_credentials", _fake_get_creds),
        patch("verified_googledocs_mcp.server.build_docs_service", _fake_build_service),
        patch("verified_googledocs_mcp.server.fetch_document", _fake_fetch),
        patch(f"verified_googledocs_mcp.{module}.fetch_document", _fake_fetch),
        patch("verified_googledocs_mcp.suggestions.fetch_document_inline", _fake_fetch_inline),
    ]
    return patchers, mock_service


def _apply_all(patchers):  # type: ignore[no-untyped-def]
    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


class TestMarkdownAndTextToolsBlockedBySuggestion:
    async def test_replace_range_markdown_raises_and_never_writes(self) -> None:
        patchers, mock_service = _build_client_mock_env(module="markdown_mutations")
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_range_markdown",
                    {
                        "doc_id": "doc-guard-test",
                        "tab_id": "tab-1",
                        "start_index": 1,
                        "end_index": 12,
                        "computed_at_revision": "rev-1",
                        "markdown": "Replacement text",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "SUGGESTIONS_PRESENT" in str(result.content)
        mock_service.documents.return_value.batchUpdate.assert_not_called()

    async def test_replace_tab_markdown_raises_and_never_writes(self) -> None:
        patchers, mock_service = _build_client_mock_env(module="markdown_mutations")
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_tab_markdown",
                    {"doc_id": "doc-guard-test", "tab_id": "tab-1", "markdown": "New content"},
                    raise_on_error=False,
                )
        assert result.is_error
        assert "SUGGESTIONS_PRESENT" in str(result.content)
        mock_service.documents.return_value.batchUpdate.assert_not_called()

    async def test_append_markdown_raises_and_never_writes(self) -> None:
        patchers, mock_service = _build_client_mock_env(module="markdown_mutations")
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "append_markdown",
                    {"doc_id": "doc-guard-test", "tab_id": "tab-1", "markdown": "More content"},
                    raise_on_error=False,
                )
        assert result.is_error
        assert "SUGGESTIONS_PRESENT" in str(result.content)
        mock_service.documents.return_value.batchUpdate.assert_not_called()

    async def test_insert_image_raises_and_never_writes(self) -> None:
        patchers, mock_service = _build_client_mock_env(module="markdown_mutations")
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_image",
                    {
                        "doc_id": "doc-guard-test",
                        "tab_id": "tab-1",
                        "anchor": "Hello world",
                        "source": "https://example.com/image.png",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "SUGGESTIONS_PRESENT" in str(result.content)
        mock_service.documents.return_value.batchUpdate.assert_not_called()

    async def test_replace_text_raises_and_never_writes(self) -> None:
        patchers, mock_service = _build_client_mock_env(module="mutations")
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-guard-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "SUGGESTIONS_PRESENT" in str(result.content)
        mock_service.documents.return_value.batchUpdate.assert_not_called()


# ---------------------------------------------------------------------------
# Control: the same tools succeed on a suggestion-free doc (guard is a no-op
# in the common case)
# ---------------------------------------------------------------------------


class TestControlNoSuggestionsStillSucceeds:
    def test_insert_table_succeeds_without_suggestions(self) -> None:
        service = MagicMock()
        service.documents.return_value.get.return_value.execute.side_effect = [
            _preview_doc(),  # PREVIEW pre-read
            _preview_doc(),  # INLINE guard read — same doc, no suggestions
            _preview_doc(revision="rev-2"),  # post-read (table won't actually be found,
            # but that's a separate VERIFICATION_FAILED concern, not this guard)
        ]
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

        from verified_googledocs_mcp.verify import ErrorCode, VerifyError

        with pytest.raises(VerifyError) as exc_info:
            execute_insert_table(
                service=service,
                doc_id="doc-guard-test",
                tab_id="tab-1",
                anchor="Hello world",
                rows=[["A", "B"]],
            )
        # Reaches (and fails at) post-write verification, NOT the suggestion
        # guard — proving the guard was a no-op here.
        assert exc_info.value.envelope.error_code == ErrorCode.VERIFICATION_FAILED
        service.documents.return_value.batchUpdate.assert_called_once()
