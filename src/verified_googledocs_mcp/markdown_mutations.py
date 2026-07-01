"""Verified markdown write pipelines.

Implements the verified-write pipeline for every markdown-mutating tool:

  validate → pre-read (R1) → [structural guardrail] → compile → [dry_run]
  → batchUpdate(requiredRevisionId=R1) → post-read (R2)
  → assemble evidence → audit

API calls live here; verify.py stays pure.

Live-API caveats (needs fixture session with real credentials):
- insertTable cell-index formula in markdown_writer.py is unverified against
  the live API.
- createParagraphBullets nesting mechanism in markdown_writer.py is unverified.
- Whole-tab and range deletes hit the Docs "cannot delete the segment's
  trailing newline" constraint — the delete range must stop at end-1.
- append_markdown inserts before the final newline (at end-1), not the raw end.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .docs import IMPLICIT_TAB_ID, _available_tab_ids, _find_tab_body, fetch_document
from .index_sim import IndexSimulationError, compiled_requests_growth, simulate_requests
from .markdown import to_markdown
from .markdown_writer import UnsupportedMarkdown, compile_markdown
from .mutations import _translate_http_error
from .verify import (
    ErrorCode,
    LocateResult,
    VerifyError,
    _make_error,
    append_audit,
    assemble_range_markdown_evidence,
    assemble_structural_evidence,
    locate,
)


# ---------------------------------------------------------------------------
# Structural inventory helpers
# ---------------------------------------------------------------------------


def _iter_body_elements(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structural elements, including paragraphs nested inside tables."""
    elements: list[dict[str, Any]] = []

    def walk(content: list[dict[str, Any]]) -> None:
        for elem in content:
            elements.append(elem)
            table = elem.get("table")
            if not table:
                continue
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    walk(cell.get("content", []))

    walk(body.get("content", []))
    return elements


def _count_structural_elements(body: dict[str, Any]) -> dict[str, int]:
    """Count tables, inline images, chips, and footnotes in a Docs body dict.

    Returns a dict with keys: tables, images, chips, footnotes.
    Used for the guardrail inventory and blast-radius check.
    """
    counts: dict[str, int] = {"tables": 0, "images": 0, "chips": 0, "footnotes": 0}
    for elem in _iter_body_elements(body):
        if "table" in elem:
            counts["tables"] += 1
        if "paragraph" in elem:
            para = elem["paragraph"]
            for inline in para.get("elements", []):
                if "inlineObjectElement" in inline:
                    counts["images"] += 1
                if "person" in inline or "richLink" in inline:
                    counts["chips"] += 1
                if "footnoteReference" in inline:
                    counts["footnotes"] += 1
    return counts


def _count_structural_elements_in_range(
    body: dict[str, Any],
    start_index: int,
    end_index: int,
) -> dict[str, int]:
    """Count structural elements whose span overlaps [start_index, end_index).

    Used by the guardrail to check what would be overwritten.
    """
    counts: dict[str, int] = {"tables": 0, "images": 0, "chips": 0, "footnotes": 0}
    for elem in _iter_body_elements(body):
        elem_start = elem.get("startIndex", 0)
        elem_end = elem.get("endIndex", 0)
        if elem_start >= end_index or elem_end <= start_index:
            continue
        if "table" in elem:
            counts["tables"] += 1
        if "paragraph" in elem:
            para = elem["paragraph"]
            for inline in para.get("elements", []):
                if "inlineObjectElement" in inline:
                    counts["images"] += 1
                if "person" in inline or "richLink" in inline:
                    counts["chips"] += 1
                if "footnoteReference" in inline:
                    counts["footnotes"] += 1
    return counts


def _count_structural_elements_outside_range(
    body: dict[str, Any],
    start_index: int,
    end_index: int,
) -> dict[str, int]:
    """Count structural elements fully outside [start_index, end_index).

    Used for the blast-radius check.
    """
    counts: dict[str, int] = {"tables": 0, "images": 0, "chips": 0, "footnotes": 0}
    for elem in _iter_body_elements(body):
        elem_start = elem.get("startIndex", 0)
        elem_end = elem.get("endIndex", 0)
        if elem_end > start_index and elem_start < end_index:
            continue
        if "table" in elem:
            counts["tables"] += 1
        if "paragraph" in elem:
            para = elem["paragraph"]
            for inline in para.get("elements", []):
                if "inlineObjectElement" in inline:
                    counts["images"] += 1
                if "person" in inline or "richLink" in inline:
                    counts["chips"] += 1
                if "footnoteReference" in inline:
                    counts["footnotes"] += 1
    return counts


def _structural_total(counts: dict[str, int]) -> int:
    return sum(counts.values())


def _raise_post_write_verification_failure(
    *,
    doc_id: str,
    tab_id: str,
    tool: str,
    message: str,
    evidence: dict[str, Any],
) -> None:
    """Audit post-write verification evidence, then raise a typed failure."""
    evidence["applied"] = False
    evidence["verification_failed"] = True
    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool=tool,
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason
    raise _make_error(
        ErrorCode.VERIFICATION_FAILED,
        message,
        {"evidence": evidence},
    )


def _fail_if_range_verification_failed(
    *,
    doc_id: str,
    tab_id: str,
    tool: str,
    evidence: dict[str, Any],
) -> None:
    if evidence.get("structural_match") is False:
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool=tool,
            message="Post-write markdown verification failed.",
            evidence=evidence,
        )
    if evidence.get("applied") is False and evidence.get("input_blocks", 0) > 0:
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool=tool,
            message="Post-write re-read did not confirm that the markdown write landed.",
            evidence=evidence,
        )


# ---------------------------------------------------------------------------
# Markdown write pipelines
# ---------------------------------------------------------------------------


def _simulate_or_raise(
    requests: list[dict[str, Any]],
    *,
    tab_start: int,
    tab_end: int,
) -> None:
    """Run the offline index simulator over an assembled request list.

    Raises a typed INDEX_SIMULATION_FAILED error on any invalid index,
    instead of letting an invalid batch reach the live API as a raw 400.
    Called identically from dry_run and the real write's pre-flight (on the
    exact same assembled requests list), so the two paths can never disagree
    about whether a write is index-valid.
    """
    try:
        simulate_requests(requests, tab_start=tab_start, tab_end=tab_end)
    except IndexSimulationError as exc:
        raise _make_error(
            ErrorCode.INDEX_SIMULATION_FAILED,
            str(exc),
            {"request_index": exc.request_index, "request": exc.request},
        ) from exc


def _stamp_tab_id(node: Any, tab_id: str) -> None:
    """Recursively stamp tab_id onto every location/range in a request tree (#48).

    compile_markdown emits ``location``/``range`` objects with only an ``index``
    (or start/end), no ``tabId``. Without a ``tabId`` the Docs API resolves the
    index against the document's *first* tab segment, so a write aimed at a
    secondary tab errors (index past the first tab's end) or lands in the wrong
    tab — leaving the target empty. Stamping the target ``tabId`` onto every
    location/range scopes the whole batch to the intended tab. ``setdefault``
    preserves any tabId already set on hand-built requests.

    Tabless documents use the implicit "_body" tab; their body segment is
    addressed without a tabId, so stamping is skipped.
    """
    if not tab_id or tab_id == IMPLICIT_TAB_ID:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("location", "range") and isinstance(value, dict):
                value.setdefault("tabId", tab_id)
            _stamp_tab_id(value, tab_id)
    elif isinstance(node, list):
        for item in node:
            _stamp_tab_id(item, tab_id)


def _tab_has_content(body: dict[str, Any]) -> bool:
    """True if the tab body holds any real content (text, table, or inline object).

    A body containing only an empty paragraph (just a trailing newline) counts
    as empty. Mirrors what read_document would render as a non-empty tab.
    """
    for elem in body.get("content", []):
        if "table" in elem:
            return True
        para = elem.get("paragraph")
        if not para:
            continue  # sectionBreak, tableOfContents, etc.
        for inline in para.get("elements", []):
            if "textRun" in inline:
                if inline["textRun"].get("content", "").strip():
                    return True
            elif any(k in inline for k in ("inlineObjectElement", "person", "footnoteReference")):
                return True
    return False


def _flag_unconfirmed_write(evidence: dict[str, Any], post_body: dict[str, Any]) -> dict[str, Any]:
    """Downgrade a false success to applied=False when nothing landed (issue #48).

    A markdown write that compiled non-empty content but whose post-write re-read
    leaves the targeted tab empty did not land — e.g. a write the API accepted
    but that wrote no content into a secondary tab. The verified-write contract
    forbids reporting applied=True for a write that left no trace, so flip
    applied to False and say why.

    The signal is the *whole post tab body* being empty, not the windowed
    post_blocks count: an evidence window that simply doesn't line up with where
    content landed must not be mistaken for a failed write.
    """
    if evidence.get("input_blocks", 0) > 0 and not _tab_has_content(post_body):
        evidence["applied"] = False
        diffs = evidence.setdefault("structural_diff", [])
        diffs.append(
            "Write not confirmed: the targeted tab is still empty after the write, so "
            "the content did not land. Reported as applied=false rather than a false success."
        )
    return evidence


def execute_replace_range_markdown(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    start_index: int,
    end_index: int,
    computed_at_revision: str,
    markdown: str,
    allow_structural_loss: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verified replace-range-markdown pipeline.

    Validates that computed_at_revision matches the current document revision,
    compiles the markdown, applies the structural guardrail (refusing if the
    range contains structural elements not accounted for in the input), then
    deletes the range and inserts the compiled content under
    writeControl.requiredRevisionId.

    Raises VerifyError on any verification failure.

    STALE_RANGE: computed_at_revision doesn't match current revision.
    INVALID_INPUT: markdown cannot be compiled or structural guardrail refuses.
    UNSUPPORTED_MARKDOWN: markdown contains unsupported constructs.
    TAB_NOT_FOUND: tab_id not in document.
    REVISION_CONFLICT: document changed mid-call.
    """
    # --- Pre-read ------------------------------------------------------------
    pre_doc = fetch_document(service, doc_id)
    revision_before = pre_doc.get("revisionId", "")

    # --- Stale range check ---------------------------------------------------
    if computed_at_revision and revision_before != computed_at_revision:
        raise _make_error(
            ErrorCode.STALE_RANGE,
            (
                f"Range was computed at revision {computed_at_revision!r} but the document "
                f"is now at {revision_before!r}. Re-run find_sections to get a fresh range."
            ),
            {
                "computed_at_revision": computed_at_revision,
                "current_revision": revision_before,
                "doc_id": doc_id,
            },
        )

    body = _find_tab_body(pre_doc, tab_id)
    if body is None:
        available = _available_tab_ids(pre_doc)
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            f"Tab {tab_id!r} not found in document {doc_id!r}.",
            {"available_tabs": available},
        )

    # --- Compile markdown ----------------------------------------------------
    try:
        compiled_requests = compile_markdown(markdown, start_index=start_index)
    except UnsupportedMarkdown as exc:
        raise _make_error(
            ErrorCode.UNSUPPORTED_MARKDOWN,
            str(exc),
            {"construct": exc.construct, "source_map": exc.source_map},
        ) from exc

    # --- Structural guardrail ------------------------------------------------
    if not allow_structural_loss:
        range_counts = _count_structural_elements_in_range(body, start_index, end_index)
        if _structural_total(range_counts) > 0:
            input_blocks = _parse_markdown_blocks_for_guardrail(markdown)
            input_tables = sum(1 for b in input_blocks if b["type"] == "table")
            if range_counts["tables"] > input_tables or (
                range_counts["images"] + range_counts["chips"] + range_counts["footnotes"] > 0
            ):
                raise _make_error(
                    ErrorCode.INVALID_INPUT,
                    (
                        "Range contains structural elements that would be silently lost. "
                        "Pass allow_structural_loss=true to proceed, or update the markdown "
                        "to account for these elements."
                    ),
                    {
                        "structural_elements_in_range": range_counts,
                        "input_tables": input_tables,
                        "allow_structural_loss": False,
                    },
                )

    # --- Blast-radius baseline -----------------------------------------------
    outside_before = _count_structural_elements_outside_range(body, start_index, end_index)

    # --- Assemble the full batch (shared by dry_run and the real write) ------
    # NOTE: Docs API cannot delete the final trailing newline of a tab body.
    # We delete to end_index-1 to avoid that constraint. (Live-API caveat:
    # needs verification in a fixture session with real credentials.)
    delete_end = max(start_index, end_index - 1)
    requests: list[dict[str, Any]] = [
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": start_index,
                    "endIndex": delete_end,
                    "tabId": tab_id,
                }
            }
        }
    ] + compiled_requests
    _stamp_tab_id(requests, tab_id)

    # --- Index-bounds simulation (makes dry_run authoritative) ---------------
    # Runs on the exact assembled list that would be sent, so dry_run and the
    # real write can never disagree about whether it's index-valid.
    tab_start, tab_end = _tab_extent(body)
    _simulate_or_raise(requests, tab_start=tab_start, tab_end=tab_end)

    # --- Dry run -------------------------------------------------------------
    if dry_run:
        evidence: dict[str, Any] = {
            "applied": False,
            "revision_before": revision_before,
            "revision_after": "",
            "dry_run": True,
            "planned_requests": len(compiled_requests),
            "structural_elements_in_range": _count_structural_elements_in_range(
                body, start_index, end_index
            ),
            "audit_logged": False,
        }
        return evidence

    # --- batchUpdate: delete range then insert compiled content ---------------
    body_payload: dict[str, Any] = {
        "requests": requests,
        "writeControl": {"requiredRevisionId": revision_before},
    }

    try:
        service.documents().batchUpdate(
            documentId=doc_id,
            body=body_payload,
        ).execute(num_retries=3)
    except Exception as exc:
        translated = _translate_http_error(exc, doc_id)
        raise translated from exc

    # --- Post-read -----------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")
    post_body = _find_tab_body(post_doc, tab_id) or {}

    # --- Blast-radius check --------------------------------------------------
    outside_after = _count_structural_elements_outside_range(post_body, start_index, end_index)
    if outside_before != outside_after:
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool="replace_range_markdown",
            message="Post-write blast-radius verification failed.",
            evidence={
                "applied": False,
                "revision_before": revision_before,
                "revision_after": revision_after,
                "outside_before": outside_before,
                "outside_after": outside_after,
                "blast_radius_violation": True,
            },
        )

    # --- Assemble evidence ---------------------------------------------------
    # Bound the re-export slice to exactly the inserted content so adjacent
    # paragraphs beyond the write are not swept in and cause spurious mismatches.
    # Uses compiled_requests_growth (UTF-16, and it counts a table's structural
    # markers too) rather than a Python-len() sum of insertText, which
    # undercounts for astral emoji and for any table (whose row/cell/table-end
    # markers never appear in an insertText at all).
    exact_end = start_index + compiled_requests_growth(compiled_requests)
    evidence = assemble_range_markdown_evidence(
        input_markdown=markdown,
        post_body=post_body,
        start_index=start_index,
        end_index=exact_end,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
    )
    evidence = _flag_unconfirmed_write(evidence, post_body)
    _fail_if_range_verification_failed(
        doc_id=doc_id,
        tab_id=tab_id,
        tool="replace_range_markdown",
        evidence=evidence,
    )

    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool="replace_range_markdown",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


def execute_replace_tab_markdown(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    markdown: str,
    allow_structural_loss: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verified whole-tab replace markdown pipeline.

    Replaces the entire tab body with the compiled markdown.
    Refuses without a tab_id (TAB_NOT_FOUND if unknown).

    UNSUPPORTED_MARKDOWN: markdown contains unsupported constructs.
    TAB_NOT_FOUND: tab_id not in document.
    REVISION_CONFLICT: document changed mid-call.
    """
    if not tab_id:
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            "tab_id is required for replace_tab_markdown.",
            {},
        )

    # --- Pre-read ------------------------------------------------------------
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

    tab_start, tab_end = _tab_extent(body)

    # --- Compile markdown ----------------------------------------------------
    try:
        compiled_requests = compile_markdown(markdown, start_index=tab_start)
    except UnsupportedMarkdown as exc:
        raise _make_error(
            ErrorCode.UNSUPPORTED_MARKDOWN,
            str(exc),
            {"construct": exc.construct, "source_map": exc.source_map},
        ) from exc

    # --- Structural guardrail ------------------------------------------------
    if not allow_structural_loss:
        all_counts = _count_structural_elements(body)
        if _structural_total(all_counts) > 0:
            input_blocks = _parse_markdown_blocks_for_guardrail(markdown)
            input_tables = sum(1 for b in input_blocks if b["type"] == "table")
            if all_counts["tables"] > input_tables or (
                all_counts["images"] + all_counts["chips"] + all_counts["footnotes"] > 0
            ):
                raise _make_error(
                    ErrorCode.INVALID_INPUT,
                    (
                        "Tab contains structural elements that would be silently lost. "
                        "Pass allow_structural_loss=true to proceed."
                    ),
                    {
                        "structural_elements_in_tab": all_counts,
                        "input_tables": input_tables,
                        "allow_structural_loss": False,
                    },
                )

    # --- Assemble the full batch (shared by dry_run and the real write) ------
    # NOTE: Cannot delete the trailing newline of the tab body (end-1).
    # (Live-API caveat: needs verification in a fixture session.)
    delete_end = max(tab_start, tab_end - 1)
    requests: list[dict[str, Any]] = []
    if delete_end > tab_start:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": tab_start,
                        "endIndex": delete_end,
                        "tabId": tab_id,
                    }
                }
            }
        )
    requests.extend(compiled_requests)
    _stamp_tab_id(requests, tab_id)

    # --- Index-bounds simulation (makes dry_run authoritative) ---------------
    # Runs on the exact assembled list that would be sent, so dry_run and the
    # real write can never disagree about whether it's index-valid.
    _simulate_or_raise(requests, tab_start=tab_start, tab_end=tab_end)

    # --- Dry run -------------------------------------------------------------
    if dry_run:
        evidence: dict[str, Any] = {
            "applied": False,
            "revision_before": revision_before,
            "revision_after": "",
            "dry_run": True,
            "planned_requests": len(compiled_requests),
            "tab_extent": {"start": tab_start, "end": tab_end},
            "audit_logged": False,
        }
        return evidence

    # --- batchUpdate: delete tab body then insert compiled content -----------
    body_payload: dict[str, Any] = {
        "requests": requests,
        "writeControl": {"requiredRevisionId": revision_before},
    }

    try:
        service.documents().batchUpdate(
            documentId=doc_id,
            body=body_payload,
        ).execute(num_retries=3)
    except Exception as exc:
        translated = _translate_http_error(exc, doc_id)
        raise translated from exc

    # --- Post-read -----------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")
    post_body = _find_tab_body(post_doc, tab_id) or {}

    # --- Assemble evidence ---------------------------------------------------
    post_tab_start, post_tab_end = _tab_extent(post_body)
    evidence = assemble_range_markdown_evidence(
        input_markdown=markdown,
        post_body=post_body,
        start_index=post_tab_start,
        end_index=post_tab_end,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
    )
    evidence = _flag_unconfirmed_write(evidence, post_body)
    _fail_if_range_verification_failed(
        doc_id=doc_id,
        tab_id=tab_id,
        tool="replace_tab_markdown",
        evidence=evidence,
    )

    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool="replace_tab_markdown",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


def execute_append_markdown(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    markdown: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verified append markdown pipeline.

    Appends compiled markdown at the end of the tab body (before the final
    trailing newline — see live-API caveat in module docstring).

    UNSUPPORTED_MARKDOWN: markdown contains unsupported constructs.
    TAB_NOT_FOUND: tab_id not in document.
    REVISION_CONFLICT: document changed mid-call.
    """
    # --- Pre-read ------------------------------------------------------------
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

    tab_start, tab_end = _tab_extent(body)
    # Append inserts before the final newline to avoid the trailing-newline constraint.
    insert_at = max(1, tab_end - 1)
    # Fix #37: open a fresh paragraph by inserting a newline first, then place
    # compiled markdown after it. Mirrors the pattern in execute_insert_image.
    content_start = insert_at + 1

    # --- Compile markdown ----------------------------------------------------
    try:
        compiled_requests = compile_markdown(markdown, start_index=content_start)
    except UnsupportedMarkdown as exc:
        raise _make_error(
            ErrorCode.UNSUPPORTED_MARKDOWN,
            str(exc),
            {"construct": exc.construct, "source_map": exc.source_map},
        ) from exc

    # Build the leading newline request and prepend it so the appended content
    # starts in a fresh paragraph rather than fusing with the trailing one (#37).
    newline_request: dict[str, Any] = {
        "insertText": {
            "location": {"index": insert_at, "tabId": tab_id},
            "text": "\n",
        }
    }
    requests: list[dict[str, Any]] = [newline_request, *compiled_requests]
    _stamp_tab_id(requests, tab_id)

    # --- Index-bounds simulation (makes dry_run authoritative) ---------------
    # Runs on the exact assembled list that would be sent, so dry_run and the
    # real write can never disagree about whether it's index-valid.
    _simulate_or_raise(requests, tab_start=tab_start, tab_end=tab_end)

    # --- Dry run -------------------------------------------------------------
    if dry_run:
        evidence: dict[str, Any] = {
            "applied": False,
            "revision_before": revision_before,
            "revision_after": "",
            "dry_run": True,
            "planned_requests": len(requests),
            "insert_at": insert_at,
            "audit_logged": False,
        }
        return evidence

    # --- batchUpdate ---------------------------------------------------------
    body_payload: dict[str, Any] = {
        "requests": requests,
        "writeControl": {"requiredRevisionId": revision_before},
    }

    try:
        service.documents().batchUpdate(
            documentId=doc_id,
            body=body_payload,
        ).execute(num_retries=3)
    except Exception as exc:
        translated = _translate_http_error(exc, doc_id)
        raise translated from exc

    # --- Post-read -----------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")
    post_body = _find_tab_body(post_doc, tab_id) or {}

    # --- Assemble evidence ---------------------------------------------------
    # Use content_start (not insert_at) so the evidence window excludes the
    # original trailing paragraph and covers only the newly appended content.
    # compiled_requests_growth (UTF-16, counts table structural markers too)
    # rather than a Python-len() sum of insertText avoids undercounting for
    # astral emoji or any appended table; +50 remains a defensive margin.
    approx_end = content_start + compiled_requests_growth(compiled_requests) + 50
    evidence = assemble_range_markdown_evidence(
        input_markdown=markdown,
        post_body=post_body,
        start_index=content_start,
        end_index=approx_end,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
    )
    evidence = _flag_unconfirmed_write(evidence, post_body)
    _fail_if_range_verification_failed(
        doc_id=doc_id,
        tab_id=tab_id,
        tool="append_markdown",
        evidence=evidence,
    )

    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool="append_markdown",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


def execute_insert_image(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    anchor: str,
    source: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Verified insert-image pipeline.

    Locates the anchor text via locate(), inserts the image as an inline object
    in a new paragraph after the anchor's paragraph.

    source must be a publicly fetchable URL; local paths raise IMAGE_SOURCE_UNSUPPORTED.

    QUOTE_NOT_FOUND: anchor not found in tab (with candidates).
    IMAGE_SOURCE_UNSUPPORTED: source is a local file path.
    TAB_NOT_FOUND: tab_id not in document.
    REVISION_CONFLICT: document changed mid-call.
    """
    # --- Validate source -----------------------------------------------------
    _validate_image_source(source)

    # --- Pre-read ------------------------------------------------------------
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

    tab_json = {"body": body}

    # --- Locate anchor -------------------------------------------------------
    try:
        locate_result: LocateResult = locate(anchor, tab_json, expected_matches=1)
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

    # Find the paragraph end index for the anchor span.
    anchor_api_start = locate_result.spans[0][0]
    anchor_para_end = _find_paragraph_end(body, anchor_api_start)
    insert_at = anchor_para_end

    # --- Dry run -------------------------------------------------------------
    if dry_run:
        evidence: dict[str, Any] = {
            "applied": False,
            "revision_before": revision_before,
            "revision_after": "",
            "dry_run": True,
            "anchor_span": {
                "start": locate_result.spans[0][0],
                "end": locate_result.spans[0][1],
            },
            "insert_at": insert_at,
            "audit_logged": False,
        }
        return evidence

    # --- batchUpdate: insert paragraph + image -------------------------------
    # Insert a newline to create a new paragraph, then insert the image there.
    requests: list[dict[str, Any]] = [
        {
            "insertText": {
                "location": {"index": insert_at, "tabId": tab_id},
                "text": "\n",
            }
        },
        {
            "insertInlineImage": {
                "location": {"index": insert_at + 1, "tabId": tab_id},
                "uri": source,
                "objectSize": {
                    "height": {"magnitude": 200, "unit": "PT"},
                    "width": {"magnitude": 300, "unit": "PT"},
                },
            }
        },
    ]

    body_payload: dict[str, Any] = {
        "requests": requests,
        "writeControl": {"requiredRevisionId": revision_before},
    }

    try:
        service.documents().batchUpdate(
            documentId=doc_id,
            body=body_payload,
        ).execute(num_retries=3)
    except Exception as exc:
        translated = _translate_http_error(exc, doc_id)
        raise translated from exc

    # --- Post-read -----------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")
    post_body = _find_tab_body(post_doc, tab_id) or {}

    # --- Assemble evidence ---------------------------------------------------
    evidence = assemble_structural_evidence(
        post_body=post_body,
        anchor_paragraph_start=anchor_api_start,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
    )
    if not evidence.get("inline_object_confirmed", False):
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool="insert_image",
            message="Post-write image verification failed.",
            evidence=evidence,
        )

    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool="insert_image",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


# ---------------------------------------------------------------------------
# diff_tab_vs_file (read-only, no mutation)
# ---------------------------------------------------------------------------

_ALLOWED_FILE_ROOTS_ENV = "VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS"
_MAX_DIFF_FILE_BYTES_ENV = "VERIFIED_GOOGLEDOCS_MCP_MAX_DIFF_FILE_BYTES"
_DEFAULT_MAX_DIFF_FILE_BYTES = 1_000_000

# Well-known credential/secrets locations, relative to the user's home
# directory. Denylisted unconditionally -- regardless of the configured
# allowed roots -- because the risk this guards against is a document's own
# content tricking an agent into reading credentials (prompt injection), not
# an operator deliberately misconfiguring VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS.
# Widening the default allowed root to the home directory (below) makes this
# denylist load-bearing: it is what keeps that default from exposing
# ~/.ssh, ~/.aws, etc. to a doc-driven diff.
_SENSITIVE_HOME_RELATIVE_DENYLIST = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".git-credentials",
    ".config/gh",
    ".docker/config.json",
    ".npmrc",
)


def _allowed_file_roots() -> list[Path]:
    """Directories diff_tab_vs_file may read a file from.

    Defaults to the user's home directory, not the server process's working
    directory: MCP clients commonly register this server pinned to one repo
    (e.g. `--directory /path/to/GoogleDocs-MCP`), while a diff target usually
    lives in whichever *other* repo/project the caller is actually working
    in. Scoping to cwd by default made every cross-repo diff fail unless the
    operator pre-configured this env var. Home still excludes system paths
    and other users' home directories, and _is_denylisted_sensitive_path
    below unconditionally blocks well-known credential locations under it —
    set this env var explicitly to narrow (or further widen) the allowed
    roots themselves.
    """
    raw = os.environ.get(_ALLOWED_FILE_ROOTS_ENV)
    root_values = [p for p in raw.split(os.pathsep) if p] if raw is not None else [str(Path.home())]
    return [Path(value).expanduser().resolve(strict=False) for value in root_values]


def _is_denylisted_sensitive_path(resolved: Path) -> bool:
    """True if *resolved* is (or is inside) a well-known credential location
    under the user's home directory — see _SENSITIVE_HOME_RELATIVE_DENYLIST.
    """
    home = Path.home().resolve(strict=False)
    for entry in _SENSITIVE_HOME_RELATIVE_DENYLIST:
        denied = (home / entry).resolve(strict=False)
        if resolved == denied or resolved.is_relative_to(denied):
            return True
    return False


def _max_diff_file_bytes() -> int:
    raw = os.environ.get(_MAX_DIFF_FILE_BYTES_ENV)
    if raw is None:
        return _DEFAULT_MAX_DIFF_FILE_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"{_MAX_DIFF_FILE_BYTES_ENV} must be an integer byte count.",
            {"env_var": _MAX_DIFF_FILE_BYTES_ENV, "value": raw},
        ) from exc
    if value < 1:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"{_MAX_DIFF_FILE_BYTES_ENV} must be greater than zero.",
            {"env_var": _MAX_DIFF_FILE_BYTES_ENV, "value": raw},
        )
    return value


def _resolve_allowed_diff_file(file_path: str) -> Path:
    requested = Path(file_path).expanduser()
    try:
        resolved = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"File not found: {file_path!r}",
            {"file_path": file_path},
        ) from exc

    if _is_denylisted_sensitive_path(resolved):
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            (
                "File path resolves to a well-known credential/secrets location "
                "and is never allowed by diff_tab_vs_file, regardless of "
                f"{_ALLOWED_FILE_ROOTS_ENV}."
            ),
            {"file_path": file_path, "resolved_path": str(resolved)},
        )

    allowed_roots = _allowed_file_roots()
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            (
                "File path is outside the allowed roots for diff_tab_vs_file. "
                "By default this is the user's home directory — the file is "
                "outside that (or the caller has narrowed it), so set "
                f"{_ALLOWED_FILE_ROOTS_ENV} on the server process to a "
                f"{os.pathsep!r}-separated list of directories to widen it (e.g. in "
                "the MCP server's env config), then restart the server."
            ),
            {
                "file_path": file_path,
                "resolved_path": str(resolved),
                "allowed_roots": [str(root) for root in allowed_roots],
                "env_var": _ALLOWED_FILE_ROOTS_ENV,
            },
        )

    if not resolved.is_file():
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"Path is not a regular file: {file_path!r}",
            {"file_path": file_path, "resolved_path": str(resolved)},
        )

    max_bytes = _max_diff_file_bytes()
    size = resolved.stat().st_size
    if size > max_bytes:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "File is too large for diff_tab_vs_file.",
            {"file_path": file_path, "size_bytes": size, "max_bytes": max_bytes},
        )

    return resolved


def execute_diff_tab_vs_file(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    file_path: str,
) -> dict[str, Any]:
    """Export the tab as markdown and diff against a local file.

    Reads the local file directly (the server is local). Returns a structured
    diff with tagged hunks and a unified diff string.

    This is a READ tool — no mutation, no audit, no evidence envelope required.
    """
    # --- Fetch document and export tab as markdown ---------------------------
    doc = fetch_document(service, doc_id)
    body = _find_tab_body(doc, tab_id)
    if body is None:
        available = _available_tab_ids(doc)
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            f"Tab {tab_id!r} not found in document {doc_id!r}.",
            {"available_tabs": available},
        )

    tab_markdown, _ = to_markdown(body)
    revision_id = doc.get("revisionId", "")

    # --- Read the local file -------------------------------------------------
    file = _resolve_allowed_diff_file(file_path)
    file_content = file.read_text(encoding="utf-8")

    # --- Structured diff -----------------------------------------------------
    tab_lines = tab_markdown.splitlines(keepends=True)
    file_lines = file_content.splitlines(keepends=True)

    matcher = difflib.SequenceMatcher(None, tab_lines, file_lines)
    hunks: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        hunks.append(
            {
                "tag": tag,
                "tab_lines": tab_lines[i1:i2],
                "file_lines": file_lines[j1:j2],
                "tab_range": [i1 + 1, i2],
                "file_range": [j1 + 1, j2],
            }
        )

    unified = list(
        difflib.unified_diff(
            tab_lines,
            file_lines,
            fromfile=f"doc:{doc_id}/{tab_id}",
            tofile=file_path,
        )
    )

    return {
        "doc_id": doc_id,
        "tab_id": tab_id,
        "file_path": file_path,
        "revision_id": revision_id,
        "identical": tab_markdown == file_content,
        "hunks": hunks,
        "unified_diff": "".join(unified),
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_markdown_blocks_for_guardrail(markdown: str) -> list[dict[str, Any]]:
    """Light block parse to count tables and structural types for the guardrail."""
    from .verify import _parse_markdown_blocks

    return _parse_markdown_blocks(markdown)


def _tab_extent(body: dict[str, Any]) -> tuple[int, int]:
    """Return (start_index, end_index) for the tab body.

    Scans all structural elements to find the overall extent.
    Defaults to (1, 1) for an empty body.
    """
    content = body.get("content", [])
    if not content:
        return 1, 1
    starts = [elem.get("startIndex", 1) for elem in content if "startIndex" in elem]
    ends = [elem.get("endIndex", 1) for elem in content if "endIndex" in elem]
    if not starts or not ends:
        return 1, 1
    return min(starts), max(ends)


def _find_paragraph_end(body: dict[str, Any], api_index: int) -> int:
    """Find the endIndex of the paragraph containing the given API index.

    Returns the endIndex of the matching paragraph, or api_index + 1 if not found.
    """
    for elem in body.get("content", []):
        if "paragraph" not in elem:
            continue
        start = elem.get("startIndex", 0)
        end = elem.get("endIndex", 0)
        if start <= api_index < end:
            return end
    return api_index + 1


def _validate_image_source(source: str) -> None:
    """Raise IMAGE_SOURCE_UNSUPPORTED if source is a local file path or empty."""
    if not source:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "image source must not be empty",
            {},
        )
    parsed = urlparse(source)
    is_url = parsed.scheme in ("http", "https", "ftp", "ftps")
    if not is_url:
        raise _make_error(
            ErrorCode.IMAGE_SOURCE_UNSUPPORTED,
            (
                f"Image source {source!r} appears to be a local file path. "
                "The Docs API requires a publicly fetchable URL. "
                "Upload the file to a publicly accessible location first."
            ),
            {"source": source, "scheme": parsed.scheme},
        )
