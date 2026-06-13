"""Mutating Docs API operations.

Implements the verified-write pipeline for every tool that modifies a document:

  validate → pre-read (R1) → locate → [dry_run] → batchUpdate(requiredRevisionId=R1)
  → post-read (R2) → evidence → audit

API calls live here; verify.py stays pure.
"""

from __future__ import annotations

from typing import Any

from .docs import _available_tab_ids, _find_tab_body, fetch_document
from .verify import (
    ErrorCode,
    LocateResult,
    _make_error,
    append_audit,
    assemble_text_edit_evidence,
    locate,
)


# ---------------------------------------------------------------------------
# replace_text implementation
# ---------------------------------------------------------------------------


def _build_batch_requests(
    spans: list[tuple[int, int]],
    replace: str,
    tab_id: str,
) -> list[dict[str, Any]]:
    """Build deleteContentRange + insertText request pairs in DESCENDING span order.

    Descending order is load-bearing: applying edits from the end of the
    document toward the start ensures earlier-span indices are never shifted
    by a preceding write within the same batchUpdate call.
    """
    requests: list[dict[str, Any]] = []
    for api_start, api_end in sorted(spans, reverse=True):
        requests.append(
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": api_start,
                        "endIndex": api_end,
                        "tabId": tab_id,
                    }
                }
            }
        )
        requests.append(
            {
                "insertText": {
                    "location": {
                        "index": api_start,
                        "tabId": tab_id,
                    },
                    "text": replace,
                }
            }
        )
    return requests


def _translate_http_error(exc: Exception, doc_id: str) -> Exception:
    """Translate a googleapiclient HttpError to a VerifyError when appropriate.

    A 409 or a 400 whose message mentions "revision" signals a concurrent edit
    (requiredRevisionId rejected).  All other HTTP errors propagate as-is.
    """
    try:
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
    except ImportError:
        return exc

    if not isinstance(exc, HttpError):
        return exc

    status = exc.resp.status if exc.resp else 0
    reason = str(exc).lower()
    if status == 409 or (status == 400 and "revision" in reason):
        return _make_error(
            ErrorCode.REVISION_CONFLICT,
            f"Document {doc_id!r} was modified concurrently; re-read and retry.",
            {"http_status": status, "api_message": str(exc)},
        )
    return exc


def execute_replace_text(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    find: str,
    replace: str,
    expected_matches: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Full verified-write pipeline for replace_text.

    Raises VerifyError on any verification failure.
    Raises other exceptions for unexpected API errors.

    Returns the evidence dict (same shape regardless of dry_run).
    """
    # --- Validate inputs ---------------------------------------------------
    if not find:
        raise _make_error(ErrorCode.INVALID_INPUT, "find must not be empty")
    if find == replace:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "find and replace are identical; no change would be made",
            {"find": find},
        )

    # --- Pre-read ----------------------------------------------------------
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
    pre_tab_json = {"body": body}

    # --- Locate ------------------------------------------------------------
    locate_result: LocateResult = locate(find, pre_tab_json, expected_matches)

    # --- Dry run -----------------------------------------------------------
    if dry_run:
        # No write is issued; the "after" excerpt is the predicted diff,
        # computed by splicing `replace` into the pre-read at the located spans.
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=pre_tab_json,
            post_tab_json=pre_tab_json,  # unused when predicted_replacement is set
            revision_before=revision_before,
            revision_after="",  # unknown; write not issued
            applied=False,
            audit_logged=False,
            audit_log_reason="dry_run",
            predicted_replacement=replace,
        )
        # Record the dry-run in the audit log (best-effort; applied=False).
        audit_ok, audit_reason = append_audit(
            doc=doc_id,
            tab=tab_id,
            tool="replace_text",
            evidence=evidence,
        )
        evidence["audit_logged"] = audit_ok
        if not audit_ok:
            evidence["audit_log_reason"] = audit_reason
        return evidence

    # --- batchUpdate with requiredRevisionId -------------------------------
    requests = _build_batch_requests(locate_result.spans, replace, tab_id)
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

    # --- Post-read ---------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")

    post_body = _find_tab_body(post_doc, tab_id)
    post_tab_json: dict[str, Any] = {"body": post_body} if post_body is not None else {"body": {}}

    # --- Assemble evidence -------------------------------------------------
    evidence = assemble_text_edit_evidence(
        locate_result=locate_result,
        pre_tab_json=pre_tab_json,
        post_tab_json=post_tab_json,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        audit_logged=True,  # provisional; corrected from the append result below
        audit_log_reason="",
    )

    # One audit line per mutation, written from the full evidence (best-effort;
    # an append failure is recorded in the evidence, never raised).
    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool="replace_text",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence
