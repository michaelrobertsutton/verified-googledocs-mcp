"""§ Table tools — list_tables, get_table, replace_table_row, insert_table.

Each write is exercised against a fresh disposable copy and confirmed by
re-reading the document, following the style of test_markdown_writes.py. The
two Probe classes pin real-API contracts our compiler and guards rely on
(terminal-newline delete boundary, merged-cell tableCellStyle shape) as living
contract tests, independent of tables.py itself.
"""

from __future__ import annotations

import pytest

from verified_googledocs_mcp.docs import _find_tab_body, fetch_document

pytestmark = pytest.mark.live

# The canonical fixture's HEADING_1 (seeded for #31); inherited by every copy.
HEADING = "Text Hazards"


def _err(result) -> str:  # type: ignore[no-untyped-def]
    return str(result.content)


async def _read(client, doc_id, tab_id) -> str:  # type: ignore[no-untyped-def]
    r = await client.call_tool(
        "read_document", {"doc_id": doc_id, "tab_id": tab_id, "format": "markdown"}
    )
    return r.data["content"]


# ---------------------------------------------------------------------------
# list_tables / get_table
# ---------------------------------------------------------------------------


class TestListAndGetTable:
    async def test_seeded_table_appears_in_list_and_get(self, client, scratch_doc):
        s = scratch_doc
        markdown = "# Table Section\n\n| Metric | Detail |\n|---|---|\n| seed1 | seed2 |\n"
        await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "markdown": markdown},
        )

        listed = await client.call_tool(
            "list_tables", {"doc_id": s.doc_id, "tab_id": s.primary_tab}
        )
        tables = listed.data["tables"]
        assert len(tables) == 1
        seeded = tables[0]
        assert seeded["table_index"] == 0
        assert seeded["rows"] == 1
        assert seeded["columns"] == 2
        assert seeded["first_row"] == ["seed1", "seed2"]
        assert seeded["preceding_heading"] == "Table Section"
        assert seeded["has_merged_cells"] is False

        got = await client.call_tool(
            "get_table",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "table_index": 0},
        )
        assert got.data["cells"] == [["seed1", "seed2"]]

    async def test_bad_table_index_is_table_not_found(self, client, scratch_doc):
        s = scratch_doc
        await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "markdown": "| A | B |\n|---|---|\n| 1 | 2 |\n",
            },
        )
        r = await client.call_tool(
            "get_table",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "table_index": 5},
            raise_on_error=False,
        )
        assert r.is_error and "TABLE_NOT_FOUND" in _err(r)


# ---------------------------------------------------------------------------
# replace_table_row
# ---------------------------------------------------------------------------


class TestReplaceTableRow:
    async def test_round_trip_with_curly_quotes_and_empty_cell(self, client, scratch_doc):
        s = scratch_doc
        await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "markdown": "| A | B |\n|---|---|\n| old1 | old2 |\n",
            },
        )

        r = await client.call_tool(
            "replace_table_row",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "table_index": 0,
                "row_index": 0,
                "cells": ["It’s updated", ""],
            },
        )
        assert r.data["applied"] is True
        assert r.data["cells_match"] is True
        assert r.data["revision_before"] != r.data["revision_after"]

        got = await client.call_tool(
            "get_table",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "table_index": 0},
        )
        assert got.data["cells"][0][0] == "It’s updated"
        assert got.data["cells"][0][1] == ""

    async def test_dry_run_does_not_mutate(self, client, scratch_doc):
        s = scratch_doc
        await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "markdown": "| A | B |\n|---|---|\n| keep1 | keep2 |\n",
            },
        )
        dry = await client.call_tool(
            "replace_table_row",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "table_index": 0,
                "row_index": 0,
                "cells": ["changed1", "changed2"],
                "dry_run": True,
            },
        )
        assert dry.data["applied"] is False
        assert dry.data["dry_run"] is True

        got = await client.call_tool(
            "get_table",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "table_index": 0},
        )
        assert got.data["cells"] == [["keep1", "keep2"]]


# ---------------------------------------------------------------------------
# insert_table
# ---------------------------------------------------------------------------


class TestInsertTable:
    async def test_insert_after_heading_then_get_table(self, client, scratch_doc):
        s = scratch_doc
        r = await client.call_tool(
            "insert_table",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "anchor": HEADING,
                "rows": [["h1", "h2"], ["v1", "v2"]],
            },
        )
        assert r.data["applied"] is True
        assert r.data["table_confirmed"] is True
        table_index = r.data["table_index"]

        got = await client.call_tool(
            "get_table",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "table_index": table_index},
        )
        assert got.data["cells"] == [["h1", "h2"], ["v1", "v2"]]

    async def test_dry_run_and_real_agree_and_dry_run_does_not_mutate(self, client, scratch_doc):
        s = scratch_doc
        before = await _read(client, s.doc_id, s.primary_tab)

        dry = await client.call_tool(
            "insert_table",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "anchor": HEADING,
                "rows": [["p1", "p2"]],
                "dry_run": True,
            },
        )
        assert dry.data["applied"] is False
        assert dry.data["dry_run"] is True

        after_dry = await _read(client, s.doc_id, s.primary_tab)
        assert after_dry == before  # dry run must not mutate the document

        real = await client.call_tool(
            "insert_table",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "anchor": HEADING,
                "rows": [["p1", "p2"]],
            },
        )
        assert real.data["applied"] is True

        # The real write's table must land at the exact index dry_run predicted.
        listed = await client.call_tool(
            "list_tables", {"doc_id": s.doc_id, "tab_id": s.primary_tab}
        )
        new_table = listed.data["tables"][real.data["table_index"]]
        assert new_table["start_index"] == dry.data["insert_at"] + 1

    async def test_anchor_inside_table_is_invalid_input(self, client, scratch_doc):
        s = scratch_doc
        await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "markdown": "| A | B |\n|---|---|\n| cell text here | other |\n",
            },
        )
        r = await client.call_tool(
            "insert_table",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "anchor": "cell text here",
                "rows": [["x"]],
            },
            raise_on_error=False,
        )
        assert r.is_error and "INVALID_INPUT" in _err(r)


# ---------------------------------------------------------------------------
# TestCellDeleteBoundaryProbe — pins the terminal-newline delete constraint
# ---------------------------------------------------------------------------


class TestCellDeleteBoundaryProbe:
    """Pins the constraint execute_replace_table_row relies on: deleting a
    cell's full [start, end) range (through its terminal newline) 400s;
    stopping at end-1 succeeds. Raw batchUpdate, independent of tables.py —
    same style as TestTableGeometryProbe in test_markdown_writes.py."""

    async def test_delete_through_terminal_newline_fails_but_end_minus_one_succeeds(
        self, live_services, scratch_doc
    ):
        try:
            from googleapiclient.errors import HttpError
        except ImportError:
            pytest.skip("googleapiclient not available")

        docs, _ = live_services
        s = scratch_doc

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertTable": {
                            "rows": 1,
                            "columns": 1,
                            "location": {"index": 1, "tabId": s.primary_tab},
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        doc = fetch_document(docs, s.doc_id)
        body = _find_tab_body(doc, s.primary_tab)
        table_elem = next(el for el in body["content"] if "table" in el)
        cell = table_elem["table"]["tableRows"][0]["tableCells"][0]
        cell_start = cell["content"][0]["startIndex"]

        # Give the cell a terminal newline distinct from its own start (an
        # empty cell's paragraph start already equals end-1).
        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": cell_start, "tabId": s.primary_tab},
                            "text": "probe text",
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        doc = fetch_document(docs, s.doc_id)
        body = _find_tab_body(doc, s.primary_tab)
        table_elem = next(el for el in body["content"] if "table" in el)
        cell = table_elem["table"]["tableRows"][0]["tableCells"][0]
        cell_end = cell["content"][0]["endIndex"]

        with pytest.raises(HttpError):
            docs.documents().batchUpdate(
                documentId=s.doc_id,
                body={
                    "requests": [
                        {
                            "deleteContentRange": {
                                "range": {
                                    "startIndex": cell_start,
                                    "endIndex": cell_end,
                                    "tabId": s.primary_tab,
                                }
                            }
                        }
                    ]
                },
            ).execute(num_retries=3)

        # Stopping at end-1 (never touching the terminal newline) succeeds.
        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": cell_start,
                                "endIndex": cell_end - 1,
                                "tabId": s.primary_tab,
                            }
                        }
                    }
                ]
            },
        ).execute(num_retries=3)


# ---------------------------------------------------------------------------
# TestMergedCellProbe — pins the merged-cell tableCellStyle shape
# ---------------------------------------------------------------------------


class TestMergedCellProbe:
    """Pins what a real merged-cell tableCellStyle looks like (what
    _has_merged_cells keys on), and that replace_table_row refuses a table
    containing one."""

    async def test_merge_table_cells_then_replace_table_row_refuses(
        self, client, live_services, scratch_doc
    ):
        docs, _ = live_services
        s = scratch_doc

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertTable": {
                            "rows": 2,
                            "columns": 2,
                            "location": {"index": 1, "tabId": s.primary_tab},
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        doc = fetch_document(docs, s.doc_id)
        body = _find_tab_body(doc, s.primary_tab)
        table_elem = next(el for el in body["content"] if "table" in el)
        table_start = table_elem["startIndex"]

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "mergeTableCells": {
                            "tableRange": {
                                "tableCellLocation": {
                                    "tableStartLocation": {
                                        "index": table_start,
                                        "tabId": s.primary_tab,
                                    },
                                    "rowIndex": 0,
                                    "columnIndex": 0,
                                },
                                "rowSpan": 1,
                                "columnSpan": 2,
                            }
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        doc = fetch_document(docs, s.doc_id)
        body = _find_tab_body(doc, s.primary_tab)
        table_elem = next(el for el in body["content"] if "table" in el)
        merged_cell = table_elem["table"]["tableRows"][0]["tableCells"][0]
        assert "tableCellStyle" in merged_cell
        assert merged_cell["tableCellStyle"].get("columnSpan") == 2

        r = await client.call_tool(
            "replace_table_row",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "table_index": 0,
                "row_index": 0,
                "cells": ["x", "y"],
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "INVALID_INPUT" in content
        assert "merged" in content.lower()
