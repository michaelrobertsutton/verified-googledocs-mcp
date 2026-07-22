"""Unit tests for verified_googledocs_mcp.tables.

Tests execute_* functions directly against MagicMock services — no fastmcp
Client, no network, no real credentials. Tool registration/wrappers land in a
later work unit; these tests exercise the pipeline logic only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.unit.fixtures.tables import (
    build_table,
    doc_with_content,
    embed_nested_table,
    heading,
    plain_paragraph,
)
from verified_googledocs_mcp.index_sim import simulate_requests
from verified_googledocs_mcp.markdown_writer import _table_cell_index
from verified_googledocs_mcp.tables import (
    _assert_anchor_not_in_table,
    _cell_text,
    _has_merged_cells,
    _preceding_heading,
    _row_texts,
    _top_level_tables,
    execute_get_table,
    execute_insert_table,
    execute_list_tables,
    execute_replace_table_row,
)
from verified_googledocs_mcp.verify import (
    ErrorCode,
    VerifyError,
    _normalize_cell_text,
    assemble_insert_table_evidence,
    assemble_table_row_evidence,
)


# ---------------------------------------------------------------------------
# Audit isolation (autouse: these tests must never touch the real audit log)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_audit_dir(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the audit log at a per-test tmp dir (mirrors tests/live/conftest.py)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_pending_suggestions(monkeypatch):  # type: ignore[no-untyped-def]
    """Stub the issue #56 suggestion guard as a no-op for these pipeline tests.

    These tests exercise execute_insert_table/execute_replace_table_row logic
    directly against a hand-built MagicMock service whose get().execute() is a
    fixed side_effect sequence (pre-read, post-read, ...). The guard issues its
    own SUGGESTIONS_INLINE get() through that same service, which would consume
    an extra item from the sequence and break every test's response ordering.
    Suggestion-detection itself is exercised by the dedicated guard tests, not
    these pipeline-logic tests.
    """
    monkeypatch.setattr(
        "verified_googledocs_mcp.tables.assert_no_pending_suggestions",
        lambda **kwargs: None,
    )


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_service(get_responses: list[dict[str, Any]]) -> MagicMock:
    """A MagicMock docs service whose documents().get().execute() yields each
    dict in get_responses in order (pre-read, then post-read, ...)."""
    service = MagicMock()
    service.documents.return_value.get.return_value.execute.side_effect = list(get_responses)
    service.documents.return_value.batchUpdate.return_value.execute.return_value = {}
    return service


def _iter_locations_and_ranges(node: Any):  # type: ignore[no-untyped-def]
    """Yield every dict that is the value of a 'location' or 'range' key."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("location", "range") and isinstance(value, dict):
                yield value
            yield from _iter_locations_and_ranges(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_locations_and_ranges(item)


# ---------------------------------------------------------------------------
# Pure walkers
# ---------------------------------------------------------------------------


class TestWalkers:
    def test_top_level_tables_enumeration_order_and_nested_exclusion(self) -> None:
        outer, _ = build_table(1, [["A", "B"], ["C", "D"]])
        inner, _ = build_table(200, [["n1", "n2"]])
        embed_nested_table(outer, 1, 1, inner)
        body = {"content": [outer]}

        tables = _top_level_tables(body)
        assert len(tables) == 1
        assert tables[0] is outer

    def test_top_level_tables_two_tables_in_document_order(self) -> None:
        h1, cursor = heading(1, "First Table", 1)
        t1, cursor = build_table(cursor, [["Header A", "Header B"]])
        h2, cursor = heading(1, "Second Table", cursor)
        t2, cursor = build_table(cursor, [["X", "Y"]])
        body = {"content": [h1, t1, h2, t2]}

        tables = _top_level_tables(body)
        assert len(tables) == 2
        assert tables[0] is t1
        assert tables[1] is t2

    def test_preceding_heading_attributes_each_table_to_its_own_heading(self) -> None:
        h1, cursor = heading(1, "First Table", 1)
        t1, cursor = build_table(cursor, [["Header A", "Header B"]])
        h2, cursor = heading(1, "Second Table", cursor)
        t2, cursor = build_table(cursor, [["X", "Y"]])
        body = {"content": [h1, t1, h2, t2]}

        assert _preceding_heading(body, t1["startIndex"]) == "First Table"
        assert _preceding_heading(body, t2["startIndex"]) == "Second Table"

    def test_preceding_heading_none_when_nothing_precedes(self) -> None:
        table, _ = build_table(1, [["A"]])
        body = {"content": [table]}
        assert _preceding_heading(body, table["startIndex"]) is None

    def test_cell_text_empty_cell(self) -> None:
        table, _ = build_table(1, [[""]])
        cell = table["table"]["tableRows"][0]["tableCells"][0]
        assert _cell_text(cell) == ""

    def test_cell_text_multi_paragraph_cell(self) -> None:
        table, _ = build_table(1, [[["Line1", "Line2"]]])
        cell = table["table"]["tableRows"][0]["tableCells"][0]
        assert _cell_text(cell) == "Line1\nLine2"

    def test_cell_text_strips_exactly_one_trailing_newline(self) -> None:
        table, _ = build_table(1, [["Some text"]])
        cell = table["table"]["tableRows"][0]["tableCells"][0]
        assert _cell_text(cell) == "Some text"

    def test_row_texts_reads_across_a_row(self) -> None:
        table, _ = build_table(1, [["A", "B", "C"]])
        assert _row_texts(table, 0) == ["A", "B", "C"]

    def test_has_merged_cells_true_for_row_span(self) -> None:
        table, _ = build_table(1, [["A", "B"]], merges={(0, 0): {"rowSpan": 2}})
        assert _has_merged_cells(table) is True

    def test_has_merged_cells_true_for_column_span(self) -> None:
        table, _ = build_table(1, [["A", "B"]], merges={(0, 1): {"columnSpan": 2}})
        assert _has_merged_cells(table) is True

    def test_has_merged_cells_false_when_span_is_one(self) -> None:
        table, _ = build_table(1, [["A", "B"]], merges={(0, 0): {"rowSpan": 1, "columnSpan": 1}})
        assert _has_merged_cells(table) is False

    def test_has_merged_cells_false_for_plain_table(self) -> None:
        table, _ = build_table(1, [["A", "B"], ["C", "D"]])
        assert _has_merged_cells(table) is False

    def test_assert_anchor_not_in_table_passes_for_paragraph(self) -> None:
        para, _ = plain_paragraph("Body text", 1)
        body = {"content": [para]}
        _assert_anchor_not_in_table(body, 1)  # must not raise

    def test_assert_anchor_not_in_table_raises_for_table(self) -> None:
        table, _ = build_table(1, [["Inside Cell"]])
        body = {"content": [table]}
        cell_start = table["table"]["tableRows"][0]["tableCells"][0]["content"][0]["startIndex"]
        with pytest.raises(VerifyError) as exc_info:
            _assert_anchor_not_in_table(body, cell_start)
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------------------
# execute_list_tables / execute_get_table
# ---------------------------------------------------------------------------


def _two_table_doc() -> dict[str, Any]:
    h1, cursor = heading(1, "First Table", 1)
    t1, cursor = build_table(cursor, [["Header A", "Header B"], ["Cell 1", "Cell 2"]])
    h2, cursor = heading(1, "Second Table", cursor)
    t2, cursor = build_table(cursor, [["X", "Y"]])
    return doc_with_content([h1, t1, h2, t2], doc_id="doc-two-tables", revision="rev-1")


class TestListTables:
    def test_happy_path(self) -> None:
        doc = _two_table_doc()
        service = _mock_service([doc])
        result = execute_list_tables(service=service, doc_id=doc["documentId"], tab_id="tab-1")

        assert result["doc_id"] == doc["documentId"]
        assert result["tab_id"] == "tab-1"
        assert result["revision_id"] == "rev-1"
        assert len(result["tables"]) == 2

        t0 = result["tables"][0]
        assert t0["table_index"] == 0
        assert t0["rows"] == 2
        assert t0["columns"] == 2
        assert t0["preceding_heading"] == "First Table"
        assert t0["first_row"] == ["Header A", "Header B"]
        assert t0["has_merged_cells"] is False

        t1 = result["tables"][1]
        assert t1["table_index"] == 1
        assert t1["rows"] == 1
        assert t1["columns"] == 2
        assert t1["preceding_heading"] == "Second Table"
        assert t1["first_row"] == ["X", "Y"]

    def test_tab_not_found(self) -> None:
        doc = _two_table_doc()
        service = _mock_service([doc])
        with pytest.raises(VerifyError) as exc_info:
            execute_list_tables(service=service, doc_id=doc["documentId"], tab_id="bad-tab")
        assert exc_info.value.envelope.error_code == ErrorCode.TAB_NOT_FOUND
        assert exc_info.value.envelope.diagnostics == {"available_tabs": ["tab-1"]}


class TestGetTable:
    def test_happy_path(self) -> None:
        doc = _two_table_doc()
        service = _mock_service([doc])
        result = execute_get_table(
            service=service, doc_id=doc["documentId"], tab_id="tab-1", table_index=0
        )
        assert result["rows"] == 2
        assert result["columns"] == 2
        assert result["cells"] == [["Header A", "Header B"], ["Cell 1", "Cell 2"]]
        assert result["has_merged_cells"] is False
        assert result["revision_id"] == "rev-1"

    def test_table_not_found(self) -> None:
        doc = _two_table_doc()
        service = _mock_service([doc])
        with pytest.raises(VerifyError) as exc_info:
            execute_get_table(
                service=service, doc_id=doc["documentId"], tab_id="tab-1", table_index=5
            )
        assert exc_info.value.envelope.error_code == ErrorCode.TABLE_NOT_FOUND
        assert exc_info.value.envelope.diagnostics == {"table_count": 2, "table_index": 5}

    def test_tab_not_found(self) -> None:
        doc = _two_table_doc()
        service = _mock_service([doc])
        with pytest.raises(VerifyError) as exc_info:
            execute_get_table(
                service=service, doc_id=doc["documentId"], tab_id="bad-tab", table_index=0
            )
        assert exc_info.value.envelope.error_code == ErrorCode.TAB_NOT_FOUND


# ---------------------------------------------------------------------------
# execute_replace_table_row
# ---------------------------------------------------------------------------


def _row_replace_doc() -> dict[str, Any]:
    """A 2x2 table: row0 = ["Header A"(bold), "Header B"], row1 = ["", "Cell 2"]."""
    table, _ = build_table(
        1,
        [["Header A", "Header B"], ["", "Cell 2"]],
        styles={(0, 0): {"bold": True}},
    )
    return doc_with_content([table], doc_id="doc-row-replace", revision="rev-1")


class TestReplaceTableRow:
    def test_descending_order_tab_id_write_control_and_style_carryover(self) -> None:
        pre = _row_replace_doc()
        post_table, _ = build_table(1, [["New A", "New B"], ["", "Cell 2"]])
        post = doc_with_content([post_table], doc_id="doc-row-replace", revision="rev-2")
        service = _mock_service([pre, post])

        result = execute_replace_table_row(
            service=service,
            doc_id=pre["documentId"],
            tab_id="tab-1",
            table_index=0,
            row_index=0,
            cells=["New A", "New B"],
        )

        assert service.documents.return_value.batchUpdate.call_count == 1
        body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
        requests = body["requests"]

        # Descending cell order: cell (0,1) [higher index] before cell (0,0).
        assert requests[0] == {
            "deleteContentRange": {"range": {"startIndex": 14, "endIndex": 22, "tabId": "tab-1"}}
        }
        assert requests[1] == {
            "insertText": {"location": {"index": 14, "tabId": "tab-1"}, "text": "New B"}
        }
        assert requests[2] == {
            "deleteContentRange": {"range": {"startIndex": 3, "endIndex": 11, "tabId": "tab-1"}}
        }
        assert requests[3] == {
            "insertText": {"location": {"index": 3, "tabId": "tab-1"}, "text": "New A"}
        }
        # Style carried over from cell (0,0)'s pre-existing bold; cell (0,1)
        # had no style, so it gets no updateTextStyle request.
        assert requests[4] == {
            "updateTextStyle": {
                "range": {"startIndex": 3, "endIndex": 8, "tabId": "tab-1"},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        }
        assert len(requests) == 5

        # writeControl carries the pre-read revision.
        assert body["writeControl"]["requiredRevisionId"] == "rev-1"

        # Every location/range in the batch is scoped to the target tab.
        locs = list(_iter_locations_and_ranges(requests))
        assert locs
        assert all(o.get("tabId") == "tab-1" for o in locs)

        assert result["applied"] is True
        assert result["cells_match"] is True
        assert result["row_before"] == ["Header A", "Header B"]
        assert result["row_after"] == ["New A", "New B"]
        assert result["revision_before"] == "rev-1"
        assert result["revision_after"] == "rev-2"
        assert result["audit_logged"] is True

    def test_empty_existing_cell_skips_delete_and_empty_replacement_skips_insert(self) -> None:
        pre = _row_replace_doc()
        post_table, _ = build_table(1, [["Header A", "Header B"], ["New1", ""]])
        post = doc_with_content([post_table], doc_id="doc-row-replace", revision="rev-2")
        service = _mock_service([pre, post])

        result = execute_replace_table_row(
            service=service,
            doc_id=pre["documentId"],
            tab_id="tab-1",
            table_index=0,
            row_index=1,
            cells=["New1", ""],
        )

        body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
        requests = body["requests"]
        # cell (1,1) had text "Cell 2" -> delete only (empty replacement, no insert).
        # cell (1,0) was empty -> insert only (no delete of an empty cell).
        assert requests == [
            {"deleteContentRange": {"range": {"startIndex": 28, "endIndex": 34, "tabId": "tab-1"}}},
            {"insertText": {"location": {"index": 25, "tabId": "tab-1"}, "text": "New1"}},
        ]
        assert result["row_after"] == ["New1", ""]
        assert result["cells_match"] is True

    def test_cells_match_normalizes_curly_quotes_and_control_chars(self) -> None:
        pre_table, _ = build_table(1, [["Old"]])
        pre = doc_with_content([pre_table], doc_id="doc-normalize", revision="rev-1")
        post_table, _ = build_table(1, [["Plain\x0bA"]])
        post = doc_with_content([post_table], doc_id="doc-normalize", revision="rev-2")
        service = _mock_service([pre, post])

        result = execute_replace_table_row(
            service=service,
            doc_id=pre["documentId"],
            tab_id="tab-1",
            table_index=0,
            row_index=0,
            cells=["Plain A"],
        )
        assert result["applied"] is True
        assert result["cells_match"] is True
        assert result["row_after"] == ["Plain\x0bA"]  # raw readback, unnormalized

    def test_verification_failed_when_post_read_differs(self) -> None:
        pre_table, _ = build_table(1, [["Old"]])
        pre = doc_with_content([pre_table], doc_id="doc-mismatch", revision="rev-1")
        post_table, _ = build_table(1, [["Completely Different"]])
        post = doc_with_content([post_table], doc_id="doc-mismatch", revision="rev-2")
        service = _mock_service([pre, post])

        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                table_index=0,
                row_index=0,
                cells=["New Text"],
            )
        envelope = exc_info.value.envelope
        assert envelope.error_code == ErrorCode.VERIFICATION_FAILED
        assert envelope.diagnostics["row_after"] == ["Completely Different"]
        assert envelope.diagnostics["requested_cells"] == ["New Text"]
        evidence = envelope.diagnostics["evidence"]
        assert evidence["applied"] is False
        assert evidence["audit_logged"] is True
        # issue #56 defect 3: batchUpdate WAS sent and the revision DID
        # advance (rev-1 -> rev-2) even though the written row doesn't match
        # what was requested — the envelope must say so, not just applied=false.
        assert evidence["document_mutated"] is True
        assert evidence["needs_manual_restore"] is True

    def test_dry_run_skips_batch_update(self) -> None:
        table, _ = build_table(1, [["Header A", "Header B"]])
        pre = doc_with_content([table], doc_id="doc-dry-run", revision="rev-1")
        service = _mock_service([pre])

        result = execute_replace_table_row(
            service=service,
            doc_id=pre["documentId"],
            tab_id="tab-1",
            table_index=0,
            row_index=0,
            cells=["New A", "New B"],
            dry_run=True,
        )
        assert service.documents.return_value.batchUpdate.call_count == 0
        assert result == {
            "applied": False,
            "dry_run": True,
            "table_index": 0,
            "row_index": 0,
            "row_before": ["Header A", "Header B"],
            "row_after_preview": ["New A", "New B"],
            "planned_requests": result["planned_requests"],
            "revision_before": "rev-1",
            "revision_after": "",
            "audit_logged": False,
        }
        assert isinstance(result["planned_requests"], int)
        assert result["planned_requests"] > 0

    def test_merged_cells_guard(self) -> None:
        table, _ = build_table(1, [["A", "B"]], merges={(0, 0): {"rowSpan": 2}})
        pre = doc_with_content([table], doc_id="doc-merged", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                table_index=0,
                row_index=0,
                cells=["x", "y"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert "merged cells" in exc_info.value.envelope.message

    def test_row_index_out_of_range(self) -> None:
        table, _ = build_table(1, [["A", "B"]])
        pre = doc_with_content([table], doc_id="doc-row-oob", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                table_index=0,
                row_index=5,
                cells=["x", "y"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert exc_info.value.envelope.diagnostics == {"row_count": 1, "row_index": 5}

    def test_cells_length_mismatch(self) -> None:
        table, _ = build_table(1, [["A", "B"]])
        pre = doc_with_content([table], doc_id="doc-len-mismatch", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                table_index=0,
                row_index=0,
                cells=["only one"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert exc_info.value.envelope.diagnostics == {
            "expected_cells": 2,
            "provided_cells": 1,
            "row_texts": ["A", "B"],
        }

    def test_nested_table_in_cell_guard(self) -> None:
        outer, _ = build_table(1, [["A", "B"], ["C", "D"]])
        inner, _ = build_table(500, [["n1", "n2"]])
        embed_nested_table(outer, 1, 1, inner)
        pre = doc_with_content([outer], doc_id="doc-nested", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                table_index=0,
                row_index=1,
                cells=["x", "y"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert "nested table" in exc_info.value.envelope.message
        assert exc_info.value.envelope.diagnostics == {
            "table_index": 0,
            "row_index": 1,
            "cell_index": 1,
        }

    def test_table_not_found(self) -> None:
        table, _ = build_table(1, [["A"]])
        pre = doc_with_content([table], doc_id="doc-table-oob", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                table_index=5,
                row_index=0,
                cells=["x"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.TABLE_NOT_FOUND
        assert exc_info.value.envelope.diagnostics == {"table_count": 1, "table_index": 5}

    def test_tab_not_found(self) -> None:
        table, _ = build_table(1, [["A"]])
        pre = doc_with_content([table], doc_id="doc-tab-oob", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_replace_table_row(
                service=service,
                doc_id=pre["documentId"],
                tab_id="bad-tab",
                table_index=0,
                row_index=0,
                cells=["x"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.TAB_NOT_FOUND


# ---------------------------------------------------------------------------
# execute_insert_table
# ---------------------------------------------------------------------------


def _insert_table_anchor_doc() -> tuple[dict[str, Any], int, int, int]:
    """Two-paragraph doc; anchor is the first (non-last) paragraph.

    'Anchor here\\n' spans [1, 13); 'Trailing text\\n' spans [13, 27). So
    anchor_para_end=13, tab_end=27, and insert_at=min(13, 26)=13 (unclamped).
    Returns (doc, insert_at, table_start, tab_end).
    """
    p1, cursor = plain_paragraph("Anchor here", 1)
    p2, tab_end = plain_paragraph("Trailing text", cursor)
    doc = doc_with_content([p1, p2], doc_id="doc-insert-table", revision="rev-1")
    insert_at = 13
    table_start = insert_at + 1
    return doc, insert_at, table_start, tab_end


class TestInsertTable:
    def test_emits_table_cell_index_formula_and_passes_simulation(self) -> None:
        pre, insert_at, table_start, tab_end = _insert_table_anchor_doc()
        rows = [["a", "b"], ["c", "d"], ["e", "f"]]
        post_table, _ = build_table(table_start, rows)
        post_content = pre["tabs"][0]["documentTab"]["body"]["content"] + [post_table]
        post = doc_with_content(post_content, doc_id="doc-insert-table", revision="rev-2")
        service = _mock_service([pre, post])

        execute_insert_table(
            service=service,
            doc_id=pre["documentId"],
            tab_id="tab-1",
            anchor="Anchor here",
            rows=rows,
        )

        body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
        requests = body["requests"]

        assert requests[0] == {
            "insertTable": {
                "rows": 3,
                "columns": 2,
                "location": {"index": insert_at, "tabId": "tab-1"},
            }
        }

        # Reversed row-major order: (2,1), (2,0), (1,1), (1,0), (0,1), (0,0).
        expected_order = [(2, 1), (2, 0), (1, 1), (1, 0), (0, 1), (0, 0)]
        insert_text_requests = requests[1:]
        assert len(insert_text_requests) == len(expected_order)
        for req, (r, c) in zip(insert_text_requests, expected_order):
            expected_index = _table_cell_index(table_start, 2, r, c)
            assert req["insertText"]["location"]["index"] == expected_index
            assert req["insertText"]["location"]["tabId"] == "tab-1"
            assert req["insertText"]["text"] == rows[r][c]

        # The exact request list this pipeline sent must also pass the
        # offline index simulator independently.
        simulate_requests(requests, tab_start=1, tab_end=tab_end)

    def test_happy_path_confirms_table(self) -> None:
        pre, insert_at, table_start, _tab_end = _insert_table_anchor_doc()
        rows = [["r0c0", "r0c1"], ["r1c0", "r1c1"]]
        post_table, _ = build_table(table_start, rows)
        post_content = pre["tabs"][0]["documentTab"]["body"]["content"] + [post_table]
        post = doc_with_content(post_content, doc_id="doc-insert-table", revision="rev-2")
        service = _mock_service([pre, post])

        result = execute_insert_table(
            service=service,
            doc_id=pre["documentId"],
            tab_id="tab-1",
            anchor="Anchor here",
            rows=rows,
        )
        assert result["applied"] is True
        assert result["table_confirmed"] is True
        assert result["table_index"] == 0
        assert result["rows"] == 2
        assert result["columns"] == 2
        assert result["first_row"] == ["r0c0", "r0c1"]
        assert result["revision_before"] == "rev-1"
        assert result["revision_after"] == "rev-2"
        assert result["audit_logged"] is True

    def test_table_not_confirmed_raises_verification_failed_with_mutation_flags(self) -> None:
        # Post-read shows no table at the expected location at all (e.g. the
        # API accepted the batchUpdate but the write landed somewhere the
        # verifier doesn't recognize as the expected table) — table_confirmed
        # is False, but a batchUpdate WAS sent and the revision DID advance
        # (issue #56 defect 3: the envelope must say the document was
        # mutated, not just applied=false).
        pre, insert_at, table_start, _tab_end = _insert_table_anchor_doc()
        rows = [["r0c0", "r0c1"]]
        # post has no table at all -- same content as pre, just a new revision.
        post = doc_with_content(
            pre["tabs"][0]["documentTab"]["body"]["content"],
            doc_id="doc-insert-table",
            revision="rev-2",
        )
        service = _mock_service([pre, post])

        with pytest.raises(VerifyError) as exc_info:
            execute_insert_table(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                anchor="Anchor here",
                rows=rows,
            )
        envelope = exc_info.value.envelope
        assert envelope.error_code == ErrorCode.VERIFICATION_FAILED
        assert envelope.diagnostics["expected_table_start"] == table_start
        evidence = envelope.diagnostics["evidence"]
        assert evidence["applied"] is False
        assert evidence["table_confirmed"] is False
        assert evidence["document_mutated"] is True
        assert evidence["needs_manual_restore"] is True

    def test_dry_run_clamps_when_anchor_is_in_the_last_paragraph(self) -> None:
        p1, tab_end = plain_paragraph("Anchor here", 1)
        pre = doc_with_content([p1], doc_id="doc-clamp", revision="rev-1")
        service = _mock_service([pre])

        result = execute_insert_table(
            service=service,
            doc_id=pre["documentId"],
            tab_id="tab-1",
            anchor="Anchor here",
            rows=[["a"]],
            dry_run=True,
        )
        assert service.documents.return_value.batchUpdate.call_count == 0
        assert result["insert_at"] == tab_end - 1
        assert result["dry_run"] is True
        assert result["applied"] is False
        assert result["rows"] == 1
        assert result["columns"] == 1
        assert result["revision_after"] == ""
        assert result["audit_logged"] is False

    def test_ragged_rows_is_invalid_input(self) -> None:
        p1, _ = plain_paragraph("Anchor here", 1)
        pre = doc_with_content([p1], doc_id="doc-ragged", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_insert_table(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                anchor="Anchor here",
                rows=[["a", "b"], ["c"]],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert exc_info.value.envelope.diagnostics == {"row_lengths": [2, 1]}

    def test_anchor_missing_is_quote_not_found(self) -> None:
        p1, _ = plain_paragraph("Something else entirely", 1)
        pre = doc_with_content([p1], doc_id="doc-anchor-missing", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_insert_table(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                anchor="nonexistent phrase",
                rows=[["a"]],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.QUOTE_NOT_FOUND

    def test_anchor_inside_table_is_invalid_input(self) -> None:
        table, _ = build_table(1, [["Inside Cell"]])
        pre = doc_with_content([table], doc_id="doc-anchor-in-table", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_insert_table(
                service=service,
                doc_id=pre["documentId"],
                tab_id="tab-1",
                anchor="Inside Cell",
                rows=[["a"]],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert "inside a table" in exc_info.value.envelope.message

    def test_tab_not_found(self) -> None:
        p1, _ = plain_paragraph("Anchor here", 1)
        pre = doc_with_content([p1], doc_id="doc-tab-oob", revision="rev-1")
        service = _mock_service([pre])
        with pytest.raises(VerifyError) as exc_info:
            execute_insert_table(
                service=service,
                doc_id=pre["documentId"],
                tab_id="bad-tab",
                anchor="Anchor here",
                rows=[["a"]],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.TAB_NOT_FOUND


# ---------------------------------------------------------------------------
# verify.py additions
# ---------------------------------------------------------------------------


class TestNormalizeCellText:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Plain text", "Plain text"),
            ("It’s fine", "It's fine"),  # curly apostrophe -> straight
            ("Multi\x0bspace", "Multi space"),  # vertical tab collapses
            ("soft­hyphen", "softhyphen"),  # soft hyphen stripped
            ("  padded  ", "padded"),  # leading/trailing whitespace stripped
            ("“Quoted”", '"Quoted"'),  # curly double quotes -> straight
            ("", ""),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert _normalize_cell_text(raw) == expected


class TestAssembleTableRowEvidence:
    def test_shape_and_match(self) -> None:
        evidence = assemble_table_row_evidence(
            row_before=["Old A", "Old B"],
            requested_cells=["New A", "New B"],
            row_after=["New A", "New B"],
            table_index=0,
            row_index=1,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        for key in (
            "applied",
            "table_index",
            "row_index",
            "row_before",
            "row_after",
            "cells_match",
            "revision_before",
            "revision_after",
            "audit_logged",
        ):
            assert key in evidence
        assert evidence["cells_match"] is True
        assert "audit_log_reason" not in evidence

    def test_mismatch(self) -> None:
        evidence = assemble_table_row_evidence(
            row_before=["Old"],
            requested_cells=["New"],
            row_after=["Different"],
            table_index=0,
            row_index=0,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        assert evidence["cells_match"] is False

    def test_match_normalizes_curly_quotes(self) -> None:
        evidence = assemble_table_row_evidence(
            row_before=["x"],
            requested_cells=["It's fine"],
            row_after=["It’s fine"],
            table_index=0,
            row_index=0,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        assert evidence["cells_match"] is True

    def test_audit_log_reason_present_when_nonempty(self) -> None:
        evidence = assemble_table_row_evidence(
            row_before=["x"],
            requested_cells=["x"],
            row_after=["x"],
            table_index=0,
            row_index=0,
            revision_before="r1",
            revision_after="r2",
            applied=False,
            audit_logged=False,
            audit_log_reason="disk full",
        )
        assert evidence["audit_log_reason"] == "disk full"


class TestAssembleInsertTableEvidence:
    def test_confirmed(self) -> None:
        found = {"table_index": 2, "rows": 2, "columns": 2, "first_row": ["a", "b"]}
        evidence = assemble_insert_table_evidence(
            found_table=found,
            expected_rows=2,
            expected_columns=2,
            expected_first_row=["a", "b"],
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        for key in (
            "applied",
            "table_index",
            "rows",
            "columns",
            "first_row",
            "table_confirmed",
            "revision_before",
            "revision_after",
            "audit_logged",
        ):
            assert key in evidence
        assert evidence["table_confirmed"] is True
        assert evidence["table_index"] == 2

    def test_not_found(self) -> None:
        evidence = assemble_insert_table_evidence(
            found_table=None,
            expected_rows=2,
            expected_columns=2,
            expected_first_row=["a", "b"],
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        assert evidence["table_confirmed"] is False
        assert evidence["table_index"] == -1
        assert evidence["rows"] == 0
        assert evidence["columns"] == 0
        assert evidence["first_row"] == []

    def test_dims_mismatch_not_confirmed(self) -> None:
        found = {"table_index": 0, "rows": 3, "columns": 2, "first_row": ["a", "b"]}
        evidence = assemble_insert_table_evidence(
            found_table=found,
            expected_rows=2,
            expected_columns=2,
            expected_first_row=["a", "b"],
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        assert evidence["table_confirmed"] is False

    def test_first_row_normalizes_for_comparison(self) -> None:
        found = {"table_index": 0, "rows": 1, "columns": 1, "first_row": ["It’s fine"]}
        evidence = assemble_insert_table_evidence(
            found_table=found,
            expected_rows=1,
            expected_columns=1,
            expected_first_row=["It's fine"],
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        assert evidence["table_confirmed"] is True
