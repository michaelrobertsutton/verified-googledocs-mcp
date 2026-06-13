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
from typing import Any
from urllib.parse import urlparse

from .docs import _available_tab_ids, _find_tab_body, fetch_document
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


def _count_structural_elements(body: dict[str, Any]) -> dict[str, int]:
    """Count tables, inline images, chips, and footnotes in a Docs body dict.

    Returns a dict with keys: tables, images, chips, footnotes.
    Used for the guardrail inventory and blast-radius check.
    """
    counts: dict[str, int] = {"tables": 0, "images": 0, "chips": 0, "footnotes": 0}
    for elem in body.get("content", []):
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
    for elem in body.get("content", []):
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
    for elem in body.get("content", []):
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


# ---------------------------------------------------------------------------
# Markdown write pipelines
# ---------------------------------------------------------------------------


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
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "Structural elements outside the edited range changed (blast-radius violation).",
            {
                "outside_before": outside_before,
                "outside_after": outside_after,
                "blast_radius_violation": True,
            },
        )

    # --- Assemble evidence ---------------------------------------------------
    # Approximate post-write range end for re-export slicing.
    approx_end = (
        start_index
        + sum(
            len(r.get("insertText", {}).get("text", ""))
            for r in compiled_requests
            if "insertText" in r
        )
        + 100
    )
    evidence = assemble_range_markdown_evidence(
        input_markdown=markdown,
        post_body=post_body,
        start_index=start_index,
        end_index=max(approx_end, end_index),
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
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

    _, tab_end = _tab_extent(body)
    # Append inserts before the final newline to avoid the trailing-newline constraint.
    insert_at = max(1, tab_end - 1)

    # --- Compile markdown ----------------------------------------------------
    try:
        compiled_requests = compile_markdown(markdown, start_index=insert_at)
    except UnsupportedMarkdown as exc:
        raise _make_error(
            ErrorCode.UNSUPPORTED_MARKDOWN,
            str(exc),
            {"construct": exc.construct, "source_map": exc.source_map},
        ) from exc

    # --- Dry run -------------------------------------------------------------
    if dry_run:
        evidence: dict[str, Any] = {
            "applied": False,
            "revision_before": revision_before,
            "revision_after": "",
            "dry_run": True,
            "planned_requests": len(compiled_requests),
            "insert_at": insert_at,
            "audit_logged": False,
        }
        return evidence

    # --- batchUpdate ---------------------------------------------------------
    body_payload: dict[str, Any] = {
        "requests": compiled_requests,
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
    approx_end = (
        insert_at
        + sum(
            len(r.get("insertText", {}).get("text", ""))
            for r in compiled_requests
            if "insertText" in r
        )
        + 50
    )
    evidence = assemble_range_markdown_evidence(
        input_markdown=markdown,
        post_body=post_body,
        start_index=insert_at,
        end_index=approx_end,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,
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
    from pathlib import Path

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
    file = Path(file_path)
    if not file.exists():
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"File not found: {file_path!r}",
            {"file_path": file_path},
        )

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
