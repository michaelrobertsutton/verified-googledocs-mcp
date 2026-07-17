"""Verified table tools: list/get (read-only) and replace_table_row/insert_table
(verified writes).

Tables are structural and cell-scoped. A cell's content lives in its own
paragraph range, delimited by structural markers the Docs API manages — never
string-replace across a table boundary (locate()'s STRUCTURAL_BOUNDARY guard
exists for exactly this reason). Every write here targets an explicit
(table_index, row_index) or an anchor outside any table, never free text
inside a cell.

table_index counts TOP-LEVEL tables only, in document order — tables nested
inside another table's cell are not addressable by table_index and are never
counted. A cell containing a nested table refuses replace_table_row
(INVALID_INPUT) rather than silently destroying the nested table's structure
via deleteContentRange.

API calls live here; verify.py stays pure. Reuses the tab-stamping,
index-simulation, and post-write-failure helpers from markdown_mutations.py
and the HTTP-error translation from mutations.py rather than duplicating them.
"""

from __future__ import annotations

from typing import Any

from .docs import _available_tab_ids, _find_tab_body, fetch_document
from .markdown_mutations import (
    _find_paragraph_end,
    _raise_post_write_verification_failure,
    _simulate_or_raise,
    _stamp_tab_id,
    _tab_extent,
)
from .markdown_writer import _table_cell_index, _utf16_len
from .mutations import _translate_http_error
from .verify import (
    ErrorCode,
    VerifyError,
    _make_error,
    append_audit,
    assemble_insert_table_evidence,
    assemble_table_row_evidence,
    locate,
)

# Text-style fields replace_table_row will carry over from the cell's
# pre-existing first textRun onto the newly inserted replacement text.
_STYLE_ALLOWLIST = ("bold", "italic", "underline")


# ---------------------------------------------------------------------------
# Pure walkers
# ---------------------------------------------------------------------------


def _top_level_tables(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return top-level table structural elements, in document order.

    Nested tables (inside another table's cell content) are never included —
    only elements of body["content"] itself that carry a "table" key.
    """
    return [elem for elem in body.get("content", []) if "table" in elem]


def _cell_text(cell: dict[str, Any]) -> str:
    """Render a table cell's plain text.

    Each paragraph in cell["content"] contributes its textRuns' content with
    exactly one trailing newline stripped; paragraphs are then joined with
    "\\n". Non-text inline elements (images, chips, footnotes) contribute
    nothing — a cell is not expected to carry those in practice, and a full
    placeholder scheme mirrors markdown.py's converter more than this
    cell-comparison helper needs.
    """
    paragraphs: list[str] = []
    for elem in cell.get("content", []):
        if "paragraph" not in elem:
            continue
        para = elem["paragraph"]
        text = "".join(
            inline["textRun"].get("content", "")
            for inline in para.get("elements", [])
            if "textRun" in inline
        )
        if text.endswith("\n"):
            text = text[:-1]
        paragraphs.append(text)
    return "\n".join(paragraphs)


def _row_texts(table_elem: dict[str, Any], row_index: int) -> list[str]:
    """Return the rendered text of every cell in one row of a table element."""
    rows = table_elem.get("table", {}).get("tableRows", [])
    row = rows[row_index]
    return [_cell_text(cell) for cell in row.get("tableCells", [])]


def _has_merged_cells(table_elem: dict[str, Any]) -> bool:
    """True if any cell carries a rowSpan or columnSpan other than 1."""
    for row in table_elem.get("table", {}).get("tableRows", []):
        for cell in row.get("tableCells", []):
            style = cell.get("tableCellStyle", {})
            for key in ("rowSpan", "columnSpan"):
                value = style.get(key)
                if value is not None and value != 1:
                    return True
    return False


def _preceding_heading(body: dict[str, Any], table_start: int) -> str | None:
    """Return the text of the last HEADING_* paragraph before table_start.

    Document order lets this stop at the first paragraph whose own startIndex
    reaches or passes table_start, rather than scanning the whole body.
    """
    best: str | None = None
    for elem in body.get("content", []):
        if "paragraph" not in elem:
            continue
        start = elem.get("startIndex", 0)
        if start >= table_start:
            break
        para = elem["paragraph"]
        style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        if not style.startswith("HEADING_"):
            continue
        text_parts = [
            inline["textRun"].get("content", "")
            for inline in para.get("elements", [])
            if "textRun" in inline
        ]
        best = "".join(text_parts).rstrip("\n")
    return best


def _find_table_at(body: dict[str, Any], start_index: int) -> dict[str, Any] | None:
    """Find the top-level table whose own startIndex equals start_index.

    Returns the extracted values assemble_insert_table_evidence needs — kept
    here (not in verify.py) to avoid duplicating table-walking logic there.
    """
    for idx, table_elem in enumerate(_top_level_tables(body)):
        if table_elem.get("startIndex") == start_index:
            rows = table_elem.get("table", {}).get("tableRows", [])
            n_cols = len(rows[0].get("tableCells", [])) if rows else 0
            first_row = _row_texts(table_elem, 0) if rows else []
            return {
                "table_index": idx,
                "rows": len(rows),
                "columns": n_cols,
                "first_row": first_row,
            }
    return None


def _assert_anchor_not_in_table(body: dict[str, Any], span_start: int) -> None:
    """Raise INVALID_INPUT if span_start falls inside a top-level table element.

    A table's own [startIndex, endIndex) spans every cell's nested content, so
    this single top-level check also catches an anchor nested inside a cell —
    no recursion into cell content is needed.
    """
    for elem in body.get("content", []):
        start = elem.get("startIndex", 0)
        end = elem.get("endIndex", 0)
        if start <= span_start < end:
            if "table" in elem:
                raise _make_error(
                    ErrorCode.INVALID_INPUT,
                    "anchor is inside a table; anchor must be body text",
                    {"anchor_span_start": span_start, "element_start": start, "element_end": end},
                )
            return


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


def execute_list_tables(*, service: Any, doc_id: str, tab_id: str) -> dict[str, Any]:
    """List every top-level table in a tab with lightweight per-table metadata."""
    doc = fetch_document(service, doc_id)
    revision_id = doc.get("revisionId", "")

    body = _find_tab_body(doc, tab_id)
    if body is None:
        available = _available_tab_ids(doc)
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            f"Tab {tab_id!r} not found in document {doc_id!r}.",
            {"available_tabs": available},
        )

    tables_out: list[dict[str, Any]] = []
    for idx, table_elem in enumerate(_top_level_tables(body)):
        rows = table_elem.get("table", {}).get("tableRows", [])
        n_cols = len(rows[0].get("tableCells", [])) if rows else 0
        tables_out.append(
            {
                "table_index": idx,
                "rows": len(rows),
                "columns": n_cols,
                "start_index": table_elem.get("startIndex", 0),
                "end_index": table_elem.get("endIndex", 0),
                "preceding_heading": _preceding_heading(body, table_elem.get("startIndex", 0)),
                "first_row": _row_texts(table_elem, 0) if rows else [],
                "has_merged_cells": _has_merged_cells(table_elem),
            }
        )

    return {
        "doc_id": doc_id,
        "tab_id": tab_id,
        "revision_id": revision_id,
        "tables": tables_out,
    }


def execute_get_table(
    *, service: Any, doc_id: str, tab_id: str, table_index: int
) -> dict[str, Any]:
    """Return the full cell grid of one top-level table."""
    doc = fetch_document(service, doc_id)
    revision_id = doc.get("revisionId", "")

    body = _find_tab_body(doc, tab_id)
    if body is None:
        available = _available_tab_ids(doc)
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            f"Tab {tab_id!r} not found in document {doc_id!r}.",
            {"available_tabs": available},
        )

    tables = _top_level_tables(body)
    if table_index < 0 or table_index >= len(tables):
        raise _make_error(
            ErrorCode.TABLE_NOT_FOUND,
            f"table_index {table_index} is out of range; tab has {len(tables)} table(s).",
            {"table_count": len(tables), "table_index": table_index},
        )

    table_elem = tables[table_index]
    rows = table_elem.get("table", {}).get("tableRows", [])
    n_cols = len(rows[0].get("tableCells", [])) if rows else 0
    cells = [_row_texts(table_elem, r) for r in range(len(rows))]

    return {
        "doc_id": doc_id,
        "tab_id": tab_id,
        "revision_id": revision_id,
        "table_index": table_index,
        "rows": len(rows),
        "columns": n_cols,
        "cells": cells,
        "has_merged_cells": _has_merged_cells(table_elem),
    }


# ---------------------------------------------------------------------------
# Verified writes
# ---------------------------------------------------------------------------


def execute_replace_table_row(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    table_index: int,
    row_index: int,
    cells: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verified replace-table-row pipeline.

    Replaces every cell in one row of an existing top-level table with plain
    text, preserving each cell's pre-existing bold/italic/underline style (as
    carried by its first textRun). Refuses on merged cells, a nested table in
    any target cell, or a cells list whose length doesn't match the row's
    column count.
    """
    # --- Pre-read --------------------------------------------------------
    pre_doc = fetch_document(service, doc_id)
    revision_before = pre_doc.get("revisionId", "")

    body = _find_tab_body(pre_doc, tab_id)
    if body is None:
        available = _available_tab_ids(pre_doc)
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            f"Tab {tab_id!r} not found in document {doc_id!r}.",
            {"available_tabs": available},
        )

    # --- Table lookup ------------------------------------------------------
    tables = _top_level_tables(body)
    if table_index < 0 or table_index >= len(tables):
        raise _make_error(
            ErrorCode.TABLE_NOT_FOUND,
            f"table_index {table_index} is out of range; tab has {len(tables)} table(s).",
            {"table_count": len(tables), "table_index": table_index},
        )
    table_elem = tables[table_index]

    # --- Merged-cell guard ---------------------------------------------------
    if _has_merged_cells(table_elem):
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "table contains merged cells; unmerge in the Docs UI first",
            {"table_index": table_index},
        )

    # --- Row bounds ----------------------------------------------------------
    rows = table_elem.get("table", {}).get("tableRows", [])
    row_count = len(rows)
    if row_index < 0 or row_index >= row_count:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"row_index {row_index} is out of range for a table with {row_count} row(s).",
            {"row_count": row_count, "row_index": row_index},
        )

    table_cells = rows[row_index].get("tableCells", [])
    n_cols = len(table_cells)
    if len(cells) != n_cols:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"expected {n_cols} cell value(s) for this row but got {len(cells)}.",
            {
                "expected_cells": n_cols,
                "provided_cells": len(cells),
                "row_texts": _row_texts(table_elem, row_index),
            },
        )

    # --- Nested-table guard ----------------------------------------------
    for c_idx, cell in enumerate(table_cells):
        if any("table" in elem for elem in cell.get("content", [])):
            raise _make_error(
                ErrorCode.INVALID_INPUT,
                "cell contains a nested table",
                {"table_index": table_index, "row_index": row_index, "cell_index": c_idx},
            )

    # --- Before-state + style capture ------------------------------------
    row_before = _row_texts(table_elem, row_index)
    captured_styles: list[dict[str, bool]] = []
    for cell in table_cells:
        style: dict[str, bool] = {}
        content = cell.get("content", [])
        if content:
            elements = content[0].get("paragraph", {}).get("elements", [])
            if elements and "textRun" in elements[0]:
                text_style = elements[0]["textRun"].get("textStyle", {})
                for key in _STYLE_ALLOWLIST:
                    if text_style.get(key):
                        style[key] = True
        captured_styles.append(style)

    # --- Build requests, highest cell index first -------------------------
    requests: list[dict[str, Any]] = []
    for c in range(n_cols - 1, -1, -1):
        cell = table_cells[c]
        content = cell.get("content", [])
        if not content:
            continue  # defensive: malformed cell with no paragraphs
        cell_start = content[0].get("startIndex", 0)
        cell_end = content[-1].get("endIndex", 0)
        delete_end = cell_end - 1  # never delete the cell's terminal newline
        if delete_end > cell_start:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {"startIndex": cell_start, "endIndex": delete_end}
                    }
                }
            )
        replacement = cells[c]
        if replacement:
            requests.append(
                {"insertText": {"location": {"index": cell_start}, "text": replacement}}
            )
            truthy_fields = [k for k in _STYLE_ALLOWLIST if captured_styles[c].get(k)]
            if truthy_fields:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": cell_start,
                                "endIndex": cell_start + _utf16_len(replacement),
                            },
                            "textStyle": {k: True for k in truthy_fields},
                            "fields": ",".join(truthy_fields),
                        }
                    }
                )

    _stamp_tab_id(requests, tab_id)
    tab_start, tab_end = _tab_extent(body)
    _simulate_or_raise(requests, tab_start=tab_start, tab_end=tab_end)

    # --- Dry run ------------------------------------------------------------
    if dry_run:
        return {
            "applied": False,
            "dry_run": True,
            "table_index": table_index,
            "row_index": row_index,
            "row_before": row_before,
            "row_after_preview": cells,
            "planned_requests": len(requests),
            "revision_before": revision_before,
            "revision_after": "",
            "audit_logged": False,
        }

    # --- batchUpdate ----------------------------------------------------------
    body_payload: dict[str, Any] = {
        "requests": requests,
        "writeControl": {"requiredRevisionId": revision_before},
    }
    try:
        service.documents().batchUpdate(documentId=doc_id, body=body_payload).execute(num_retries=3)
    except Exception as exc:
        translated = _translate_http_error(exc, doc_id)
        raise translated from exc

    # --- Post-read -----------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")
    post_body = _find_tab_body(post_doc, tab_id) or {}

    post_tables = _top_level_tables(post_body)
    if table_index >= len(post_tables):
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool="replace_table_row",
            message="Post-write re-read no longer shows the target table.",
            evidence={
                "applied": False,
                "table_index": table_index,
                "row_index": row_index,
                "revision_before": revision_before,
                "revision_after": revision_after,
            },
        )
    post_table = post_tables[table_index]
    post_rows = post_table.get("table", {}).get("tableRows", [])
    if row_index >= len(post_rows):
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool="replace_table_row",
            message="Post-write re-read no longer shows the target row.",
            evidence={
                "applied": False,
                "table_index": table_index,
                "row_index": row_index,
                "revision_before": revision_before,
                "revision_after": revision_after,
            },
        )
    row_after = _row_texts(post_table, row_index)

    # --- Assemble evidence -----------------------------------------------
    evidence = assemble_table_row_evidence(
        row_before=row_before,
        requested_cells=cells,
        row_after=row_after,
        table_index=table_index,
        row_index=row_index,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
    )
    if not evidence["cells_match"]:
        evidence["applied"] = False
        audit_ok, audit_reason = append_audit(
            doc=doc_id, tab=tab_id, tool="replace_table_row", evidence=evidence
        )
        evidence["audit_logged"] = audit_ok
        if not audit_ok:
            evidence["audit_log_reason"] = audit_reason
        raise _make_error(
            ErrorCode.VERIFICATION_FAILED,
            "Post-write verification failed: written row does not match requested cells.",
            {"row_after": row_after, "requested_cells": cells, "evidence": evidence},
        )

    audit_ok, audit_reason = append_audit(
        doc=doc_id, tab=tab_id, tool="replace_table_row", evidence=evidence
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


def execute_insert_table(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    anchor: str,
    rows: list[list[str]],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verified insert-table pipeline.

    Inserts a new table after the paragraph containing anchor. Cells are
    plain strings — no markdown compilation. The Docs API inserts the table's
    own leading newline, so no separate newline request is issued (pinned by
    TestTableGeometryProbe in the live suite).
    """
    # --- Pre-read --------------------------------------------------------
    pre_doc = fetch_document(service, doc_id)
    revision_before = pre_doc.get("revisionId", "")

    body = _find_tab_body(pre_doc, tab_id)
    if body is None:
        available = _available_tab_ids(pre_doc)
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            f"Tab {tab_id!r} not found in document {doc_id!r}.",
            {"available_tabs": available},
        )

    # --- Validate rows -----------------------------------------------------
    row_lengths = [len(r) for r in rows]
    if not rows or any(length == 0 for length in row_lengths):
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "rows must be a non-empty list of non-empty rows",
            {"row_lengths": row_lengths},
        )
    if len(set(row_lengths)) > 1:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "all rows must have the same number of cells",
            {"row_lengths": row_lengths},
        )
    for row in rows:
        for cell in row:
            if not isinstance(cell, str):
                raise _make_error(
                    ErrorCode.INVALID_INPUT,
                    "table cells must be plain strings",
                    {"row_lengths": row_lengths},
                )

    n_rows = len(rows)
    n_cols = row_lengths[0]

    # --- Locate anchor -------------------------------------------------------
    tab_json = {"body": body}
    try:
        locate_result = locate(anchor, tab_json, expected_matches=1)
    except VerifyError as exc:
        orig_diag = exc.envelope.diagnostics
        candidates = []
        nm = orig_diag.get("near_miss")
        if nm:
            candidates.append(nm)
        raise _make_error(
            ErrorCode.QUOTE_NOT_FOUND,
            f"Anchor not found in tab {tab_id!r}: {exc.envelope.message}",
            {
                "anchor": anchor,
                "tab_id": tab_id,
                "candidates": candidates,
                "ladder_report": orig_diag.get("ladder_report", []),
            },
        ) from exc

    span_start = locate_result.spans[0][0]
    _assert_anchor_not_in_table(body, span_start)

    anchor_para_end = _find_paragraph_end(body, span_start)
    tab_start, tab_end = _tab_extent(body)
    insert_at = min(anchor_para_end, tab_end - 1)

    # --- Build requests --------------------------------------------------
    requests: list[dict[str, Any]] = [
        {"insertTable": {"rows": n_rows, "columns": n_cols, "location": {"index": insert_at}}}
    ]
    table_start = insert_at + 1
    for r in range(n_rows - 1, -1, -1):
        for c in range(n_cols - 1, -1, -1):
            text = rows[r][c]
            if text:
                cell_index = _table_cell_index(table_start, n_cols, r, c)
                requests.append({"insertText": {"location": {"index": cell_index}, "text": text}})

    _stamp_tab_id(requests, tab_id)
    _simulate_or_raise(requests, tab_start=tab_start, tab_end=tab_end)

    # --- Dry run -------------------------------------------------------------
    if dry_run:
        return {
            "applied": False,
            "dry_run": True,
            "anchor_span": {"start": locate_result.spans[0][0], "end": locate_result.spans[0][1]},
            "insert_at": insert_at,
            "rows": n_rows,
            "columns": n_cols,
            "planned_requests": len(requests),
            "revision_before": revision_before,
            "revision_after": "",
            "audit_logged": False,
        }

    # --- batchUpdate ----------------------------------------------------------
    body_payload: dict[str, Any] = {
        "requests": requests,
        "writeControl": {"requiredRevisionId": revision_before},
    }
    try:
        service.documents().batchUpdate(documentId=doc_id, body=body_payload).execute(num_retries=3)
    except Exception as exc:
        translated = _translate_http_error(exc, doc_id)
        raise translated from exc

    # --- Post-read -----------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")
    post_body = _find_tab_body(post_doc, tab_id) or {}

    found_table = _find_table_at(post_body, table_start)
    evidence = assemble_insert_table_evidence(
        found_table=found_table,
        expected_rows=n_rows,
        expected_columns=n_cols,
        expected_first_row=rows[0],
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
    )
    if not evidence["table_confirmed"]:
        evidence["applied"] = False
        audit_ok, audit_reason = append_audit(
            doc=doc_id, tab=tab_id, tool="insert_table", evidence=evidence
        )
        evidence["audit_logged"] = audit_ok
        if not audit_ok:
            evidence["audit_log_reason"] = audit_reason
        raise _make_error(
            ErrorCode.VERIFICATION_FAILED,
            "Post-write verification failed: inserted table not confirmed at the expected location.",
            {
                "expected_table_start": table_start,
                "expected_rows": n_rows,
                "expected_columns": n_cols,
                "evidence": evidence,
            },
        )

    audit_ok, audit_reason = append_audit(
        doc=doc_id, tab=tab_id, tool="insert_table", evidence=evidence
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence
