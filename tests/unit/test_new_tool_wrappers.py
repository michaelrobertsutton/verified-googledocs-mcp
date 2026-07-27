"""Client-level smoke tests for the five table/export tool wrappers in server.py.

Exercises list_tables, get_table, replace_table_row, insert_table, and
export_pdf through the in-memory FastMCP client, mirroring the
_build_mock_env pattern in test_markdown_tools.py. All Google API calls are
mocked; no network or credentials required.

The pipeline logic itself (locate/guard/evidence internals) is already
covered by tests/unit/test_tables.py and tests/unit/test_export_pdf.py; this
file's job is the wrapper layer in server.py — argument plumbing, the
VerifyError -> ToolError conversion, and the evidence-enforcement middleware
for the two new mutating tools.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from verified_googledocs_mcp.server import mcp
from tests.unit.fixtures.evidence import assert_top_level_evidence
from tests.unit.fixtures.tables import (
    build_table,
    doc_with_content,
    heading,
    plain_paragraph,
)


# ---------------------------------------------------------------------------
# Audit / file-root isolation (autouse: these tests must never touch the
# real audit log or write outside a tmp dir)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "_state"))
    monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(tmp_path))


# ---------------------------------------------------------------------------
# Mock builder helpers
# ---------------------------------------------------------------------------


def _build_table_mock_env(*docs: dict[str, Any]):
    """Return (patchers, mock_service) for the four table tools.

    fetch_document is patched at verified_googledocs_mcp.tables.fetch_document
    — the reference execute_list_tables/execute_get_table/
    execute_replace_table_row/execute_insert_table call directly — returning
    each doc in *docs* in order and holding on the last one for any further
    calls. batchUpdate succeeds with a no-op.
    """
    mock_service = MagicMock()
    mock_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

    call_count = [0]

    def _fake_get_creds() -> Any:
        return MagicMock()

    def _fake_build_docs_service(_creds: Any) -> Any:
        return mock_service

    def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
        idx = min(call_count[0], len(docs) - 1)
        call_count[0] += 1
        return docs[idx]

    patchers = [
        patch("verified_googledocs_mcp.server.get_credentials", _fake_get_creds),
        patch("verified_googledocs_mcp.server.build_docs_service", _fake_build_docs_service),
        patch("verified_googledocs_mcp.tables.fetch_document", _fake_fetch),
        # Issue #56 suggestion guard: stubbed out here since these tests exercise
        # the wrapper layer, not suggestion detection (covered separately).
        # Without this, the guard's own SUGGESTIONS_INLINE get() would hit the
        # unconfigured mock_service and return a non-dict, non-JSON-serializable
        # MagicMock.
        patch(
            "verified_googledocs_mcp.tables.assert_no_pending_suggestions", lambda **kwargs: None
        ),
    ]
    return patchers, mock_service


def _apply_all(patchers):  # type: ignore[no-untyped-def]
    """Context manager that activates all patchers."""
    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


def _error_payload(result: Any) -> dict[str, Any]:
    assert result.is_error
    assert result.content
    return json.loads(getattr(result.content[0], "text", ""))


# ---------------------------------------------------------------------------
# list_tables
# ---------------------------------------------------------------------------


def _list_tables_doc(revision: str = "rev-1") -> dict[str, Any]:
    h1, cursor = heading(1, "Roster", 1)
    table, _cursor = build_table(cursor, [["Name", "Role"], ["Ann", "Lead"]])
    return doc_with_content([h1, table], doc_id="doc-list-tables", revision=revision)


class TestListTablesWrapper:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        doc = _list_tables_doc()
        patchers, _ = _build_table_mock_env(doc)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "list_tables", {"doc_id": doc["documentId"], "tab_id": "tab-1"}
                )
        assert not result.is_error
        data = result.data
        assert data["doc_id"] == doc["documentId"]
        assert data["tab_id"] == "tab-1"
        assert data["revision_id"] == "rev-1"
        assert len(data["tables"]) == 1
        t0 = data["tables"][0]
        assert t0["table_index"] == 0
        assert t0["rows"] == 2
        assert t0["columns"] == 2
        assert t0["preceding_heading"] == "Roster"
        assert t0["first_row"] == ["Name", "Role"]
        assert t0["has_merged_cells"] is False

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self) -> None:
        doc = _list_tables_doc()
        patchers, _ = _build_table_mock_env(doc)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "list_tables",
                    {"doc_id": doc["documentId"], "tab_id": "bad-tab"},
                    raise_on_error=False,
                )
        payload = _error_payload(result)
        assert payload["error_code"] == "TAB_NOT_FOUND"


# ---------------------------------------------------------------------------
# get_table
# ---------------------------------------------------------------------------


class TestGetTableWrapper:
    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        doc = _list_tables_doc()
        patchers, _ = _build_table_mock_env(doc)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "get_table",
                    {"doc_id": doc["documentId"], "tab_id": "tab-1", "table_index": 0},
                )
        assert not result.is_error
        data = result.data
        assert data["rows"] == 2
        assert data["columns"] == 2
        assert data["cells"] == [["Name", "Role"], ["Ann", "Lead"]]
        assert data["has_merged_cells"] is False
        assert data["revision_id"] == "rev-1"

    @pytest.mark.asyncio
    async def test_bad_index_returns_table_not_found(self) -> None:
        doc = _list_tables_doc()
        patchers, _ = _build_table_mock_env(doc)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "get_table",
                    {"doc_id": doc["documentId"], "tab_id": "tab-1", "table_index": 5},
                    raise_on_error=False,
                )
        payload = _error_payload(result)
        assert payload["error_code"] == "TABLE_NOT_FOUND"


# ---------------------------------------------------------------------------
# replace_table_row
# ---------------------------------------------------------------------------


def _row_replace_doc(revision: str) -> dict[str, Any]:
    table, _ = build_table(1, [["Old A", "Old B"]])
    return doc_with_content([table], doc_id="doc-row-replace", revision=revision)


class TestReplaceTableRowWrapper:
    @pytest.mark.asyncio
    async def test_happy_path_carries_applied_evidence(self) -> None:
        pre = _row_replace_doc("rev-1")
        post_table, _ = build_table(1, [["New A", "New B"]])
        post = doc_with_content([post_table], doc_id="doc-row-replace", revision="rev-2")
        patchers, mock_service = _build_table_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_table_row",
                    {
                        "doc_id": "doc-row-replace",
                        "tab_id": "tab-1",
                        "table_index": 0,
                        "row_index": 0,
                        "cells": ["New A", "New B"],
                    },
                )
        assert not result.is_error  # middleware did not reject for missing evidence
        assert_top_level_evidence(result)
        data = result.data
        assert data["applied"] is True
        assert data["cells_match"] is True
        assert data["row_before"] == ["Old A", "Old B"]
        assert data["row_after"] == ["New A", "New B"]
        assert data["revision_before"] == "rev-1"
        assert data["revision_after"] == "rev-2"
        assert mock_service.documents.return_value.batchUpdate.call_count == 1

    @pytest.mark.asyncio
    async def test_dry_run_no_batchupdate(self) -> None:
        pre = _row_replace_doc("rev-1")
        patchers, mock_service = _build_table_mock_env(pre)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_table_row",
                    {
                        "doc_id": "doc-row-replace",
                        "tab_id": "tab-1",
                        "table_index": 0,
                        "row_index": 0,
                        "cells": ["New A", "New B"],
                        "dry_run": True,
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is False
        assert "planned_requests" in data
        assert mock_service.documents.return_value.batchUpdate.call_count == 0

    @pytest.mark.asyncio
    async def test_table_not_found_returns_error(self) -> None:
        pre = _row_replace_doc("rev-1")
        patchers, _ = _build_table_mock_env(pre)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_table_row",
                    {
                        "doc_id": "doc-row-replace",
                        "tab_id": "tab-1",
                        "table_index": 5,
                        "row_index": 0,
                        "cells": ["x"],
                    },
                    raise_on_error=False,
                )
        payload = _error_payload(result)
        assert payload["error_code"] == "TABLE_NOT_FOUND"


# ---------------------------------------------------------------------------
# insert_table
# ---------------------------------------------------------------------------


def _insert_table_anchor_doc() -> tuple[dict[str, Any], int]:
    """Two-paragraph doc; anchor is the first (non-last) paragraph.

    'Anchor here\\n' spans [1, 13); 'Trailing text\\n' spans [13, 27), so
    anchor_para_end=13 and insert_at=min(13, 26)=13 (unclamped) — a new
    table lands at table_start=14. Returns (doc, table_start).
    """
    p1, cursor = plain_paragraph("Anchor here", 1)
    p2, _tab_end = plain_paragraph("Trailing text", cursor)
    doc = doc_with_content([p1, p2], doc_id="doc-insert-table", revision="rev-1")
    return doc, 14


class TestInsertTableWrapper:
    @pytest.mark.asyncio
    async def test_happy_path_carries_applied_evidence(self) -> None:
        pre, table_start = _insert_table_anchor_doc()
        rows = [["r0c0", "r0c1"], ["r1c0", "r1c1"]]
        post_table, _ = build_table(table_start, rows)
        post_content = pre["tabs"][0]["documentTab"]["body"]["content"] + [post_table]
        post = doc_with_content(post_content, doc_id="doc-insert-table", revision="rev-2")
        patchers, mock_service = _build_table_mock_env(pre, post)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_table",
                    {
                        "doc_id": "doc-insert-table",
                        "tab_id": "tab-1",
                        "anchor": "Anchor here",
                        "rows": rows,
                    },
                )
        assert not result.is_error  # middleware did not reject for missing evidence
        assert_top_level_evidence(result)
        data = result.data
        assert data["applied"] is True
        assert data["table_confirmed"] is True
        assert data["table_index"] == 0
        assert data["rows"] == 2
        assert data["columns"] == 2
        assert data["first_row"] == ["r0c0", "r0c1"]
        assert data["revision_before"] == "rev-1"
        assert data["revision_after"] == "rev-2"
        assert mock_service.documents.return_value.batchUpdate.call_count == 1

    @pytest.mark.asyncio
    async def test_anchor_not_found_returns_quote_not_found(self) -> None:
        p1, _ = plain_paragraph("Some other text", 1)
        pre = doc_with_content([p1], doc_id="doc-insert-table-2", revision="rev-1")
        patchers, _ = _build_table_mock_env(pre)
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "insert_table",
                    {
                        "doc_id": "doc-insert-table-2",
                        "tab_id": "tab-1",
                        "anchor": "nonexistent anchor",
                        "rows": [["a"]],
                    },
                    raise_on_error=False,
                )
        payload = _error_payload(result)
        assert payload["error_code"] == "QUOTE_NOT_FOUND"


# ---------------------------------------------------------------------------
# export_pdf
# ---------------------------------------------------------------------------


def _build_export_patchers(drive_service: Any | None = None):  # type: ignore[no-untyped-def]
    def _fake_get_creds() -> Any:
        return MagicMock()

    def _fake_build_drive_service(_creds: Any) -> Any:
        return drive_service if drive_service is not None else MagicMock()

    return [
        patch("verified_googledocs_mcp.server.get_credentials", _fake_get_creds),
        patch("verified_googledocs_mcp.server.build_drive_service", _fake_build_drive_service),
    ]


_FAKE_PDF_BYTES = b"%PDF-1.4\n...fake pdf bytes...\n%%EOF"


class TestExportPdfWrapper:
    @pytest.mark.asyncio
    async def test_happy_path_no_applied_key(self, tmp_path) -> None:
        target = tmp_path / "out.pdf"
        patchers = _build_export_patchers() + [
            patch("verified_googledocs_mcp.exports._download_pdf", return_value=_FAKE_PDF_BYTES)
        ]
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "export_pdf", {"doc_id": "doc-1", "output_path": str(target)}
                )
        assert not result.is_error
        data = result.data
        assert "applied" not in data
        assert data["bytes_written"] == len(_FAKE_PDF_BYTES)
        assert data["existed_before"] is False
        assert data["doc_id"] == "doc-1"
        assert target.read_bytes() == _FAKE_PDF_BYTES

    @pytest.mark.asyncio
    async def test_output_path_outside_allowed_roots_returns_invalid_input(
        self, tmp_path, monkeypatch
    ) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(allowed))
        outside = tmp_path / "outside.pdf"
        patchers = _build_export_patchers()
        with _apply_all(patchers):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "export_pdf",
                    {"doc_id": "doc-1", "output_path": str(outside)},
                    raise_on_error=False,
                )
        payload = _error_payload(result)
        assert payload["error_code"] == "INVALID_INPUT"
