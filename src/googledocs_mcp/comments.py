"""Drive API v3 comment operations.

Implements the comment-layer pipeline:

  list     → Drive comments.list (doc-level) + extract_suggestions (per-tab)
  get      → Drive comments.get  (full reply chain)
  create   → Drive comments.create with quotedFileContent
  reply    → Drive replies.create
  resolve  → Drive replies.create(action="resolve") → re-query → verify

NOTE: The Drive API likely renders comments as document-level even when
quotedFileContent is supplied.  The ``add_anchored_comment`` tool validates
the quote via the Docs API locate() before creating the comment, but the
resulting comment may appear UI-unanchored pending the anchoring spike.

API calls live here; verify.py stays pure.
"""

from __future__ import annotations

from typing import Any

from .docs import _available_tab_ids, _find_tab_body, fetch_document
from .verify import (
    ErrorCode,
    VerifyError,
    _make_error,
    append_audit,
    locate,
)


# ---------------------------------------------------------------------------
# Drive service builder
# ---------------------------------------------------------------------------


def build_drive_service(credentials: Any) -> Any:
    """Build and return a Google Drive API v3 service resource."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    return build("drive", "v3", credentials=credentials)


# ---------------------------------------------------------------------------
# Drive comment field sets
# ---------------------------------------------------------------------------

# Fields to request on a single comment (ensures resolved and replies are present).
_COMMENT_FIELDS = (
    "id,content,resolved,quotedFileContent,author,createdTime,modifiedTime,"
    "replies(id,content,action,author,createdTime)"
)

# Fields for list responses (same content per item, wrapped in comments[]).
_LIST_COMMENT_FIELDS = f"comments({_COMMENT_FIELDS}),nextPageToken"


# ---------------------------------------------------------------------------
# Read operations (no mutation, no audit)
# ---------------------------------------------------------------------------


def list_comments(drive_service: Any, doc_id: str) -> list[dict[str, Any]]:
    """Return all open (non-resolved) comments on the document.

    Drive comment anchors are opaque and cannot be attributed to a tab, so
    every comment in the returned list is labeled scope='document'.

    Uses pagination internally; returns the flattened list.
    """
    comments: list[dict[str, Any]] = []
    page_token: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "fileId": doc_id,
            "fields": _LIST_COMMENT_FIELDS,
            "includeDeleted": False,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = drive_service.comments().list(**kwargs).execute(num_retries=3)
        for raw in response.get("comments", []):
            if not raw.get("resolved", False):
                comments.append(_format_comment(raw))

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return comments


def get_comment_thread(drive_service: Any, doc_id: str, comment_id: str) -> dict[str, Any]:
    """Return the full comment thread including all replies.

    Raises VerifyError(INVALID_INPUT) if the comment does not exist on the doc.
    """
    try:
        raw = (
            drive_service.comments()
            .get(fileId=doc_id, commentId=comment_id, fields=_COMMENT_FIELDS)
            .execute(num_retries=3)
        )
    except Exception as exc:
        _maybe_raise_http_not_found(exc, comment_id)
        raise

    return _format_comment(raw)


# ---------------------------------------------------------------------------
# Mutating operations
# ---------------------------------------------------------------------------


def create_comment(
    drive_service: Any,
    doc_id: str,
    quote: str,
    body: str,
) -> dict[str, Any]:
    """Create a comment with quotedFileContent.

    The quote must have already been validated via locate() before this call.
    Whether the comment renders UI-anchored depends on the anchoring spike
    result (see module docstring).
    """
    payload: dict[str, Any] = {
        "content": body,
        "quotedFileContent": {
            "mimeType": "text/plain",
            "value": quote,
        },
    }
    raw = (
        drive_service.comments()
        .create(
            fileId=doc_id,
            body=payload,
            fields=_COMMENT_FIELDS,
        )
        .execute(num_retries=3)
    )
    return _format_comment(raw)


def create_reply(
    drive_service: Any,
    doc_id: str,
    comment_id: str,
    body: str,
) -> dict[str, Any]:
    """Add a reply to an existing comment thread."""
    payload: dict[str, Any] = {"content": body}
    try:
        raw = (
            drive_service.replies()
            .create(
                fileId=doc_id,
                commentId=comment_id,
                body=payload,
                fields="id,content,action,author,createdTime",
            )
            .execute(num_retries=3)
        )
    except Exception as exc:
        _maybe_raise_http_not_found(exc, comment_id)
        raise

    return raw


def resolve_comment_api(
    drive_service: Any,
    doc_id: str,
    comment_id: str,
) -> None:
    """Issue a resolve action via replies.create(action='resolve').

    This is the only mechanism that actually resolves a comment in Drive API v3.
    Using comments.update({resolved: True}) is silently ignored (resolved is
    read-only).  This is the incumbent server's bug.

    Does not re-query; the caller must re-query and verify via assemble_comment_state_evidence.
    """
    payload: dict[str, Any] = {"action": "resolve", "content": ""}
    try:
        (
            drive_service.replies()
            .create(
                fileId=doc_id,
                commentId=comment_id,
                body=payload,
                fields="id,action",
            )
            .execute(num_retries=3)
        )
    except Exception as exc:
        _maybe_raise_http_not_found(exc, comment_id)
        raise


def re_query_comment(
    drive_service: Any,
    doc_id: str,
    comment_id: str,
) -> dict[str, Any]:
    """Re-fetch a comment for post-op state verification."""
    try:
        raw = (
            drive_service.comments()
            .get(fileId=doc_id, commentId=comment_id, fields=_COMMENT_FIELDS)
            .execute(num_retries=3)
        )
    except Exception as exc:
        _maybe_raise_http_not_found(exc, comment_id)
        raise

    return _format_comment(raw)


# ---------------------------------------------------------------------------
# Mutating pipelines (validate → act → re-query → evidence → audit)
# ---------------------------------------------------------------------------


def execute_add_anchored_comment(
    *,
    drive_service: Any,
    docs_service: Any,
    doc_id: str,
    tab_id: str,
    quote: str,
    body: str,
) -> dict[str, Any]:
    """Validated comment creation pipeline.

    Validates that the quote exists in the target tab via locate(), then
    creates the comment.  Returns comment-state evidence.

    Raises VerifyError on QUOTE_NOT_FOUND, INVALID_INPUT.
    """
    # --- Validate inputs ---------------------------------------------------
    if not body.strip():
        raise _make_error(ErrorCode.INVALID_INPUT, "comment body must not be empty")
    if not quote.strip():
        raise _make_error(ErrorCode.INVALID_INPUT, "quote must not be empty")

    # --- Locate quote in tab -----------------------------------------------
    pre_doc = fetch_document(docs_service, doc_id)
    tab_body = _find_tab_body(pre_doc, tab_id)
    if tab_body is None:
        available = _available_tab_ids(pre_doc)
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            f"Tab {tab_id!r} not found in document {doc_id!r}.",
            {"available_tabs": available},
        )

    tab_json = {"body": tab_body}
    try:
        locate(quote, tab_json, expected_matches=1)
    except Exception as exc:
        if isinstance(exc, VerifyError):
            # Re-raise as QUOTE_NOT_FOUND, mapping near-miss into candidates.
            orig_diag = exc.envelope.diagnostics
            candidates = []
            nm = orig_diag.get("near_miss")
            if nm:
                candidates.append(nm)
            raise _make_error(
                ErrorCode.QUOTE_NOT_FOUND,
                f"Quote not found in tab {tab_id!r}: {exc.envelope.message}",
                {
                    "quote": quote,
                    "tab_id": tab_id,
                    "candidates": candidates,
                    "ladder_report": orig_diag.get("ladder_report", []),
                },
            ) from exc
        raise

    # --- Create comment ----------------------------------------------------
    comment_raw = create_comment(drive_service, doc_id, quote, body)

    # --- Assemble evidence -------------------------------------------------
    evidence = assemble_comment_state_evidence(
        comment=comment_raw,
        applied=True,
        audit_logged=True,
    )
    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool="add_anchored_comment",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


def execute_reply_to_comment(
    *,
    drive_service: Any,
    doc_id: str,
    comment_id: str,
    body: str,
) -> dict[str, Any]:
    """Verified reply pipeline.

    Creates the reply, re-queries the thread for post-state, returns evidence.
    """
    if not body.strip():
        raise _make_error(ErrorCode.INVALID_INPUT, "reply body must not be empty")

    # --- Create reply -------------------------------------------------------
    create_reply(drive_service, doc_id, comment_id, body)

    # --- Re-query for post-state -------------------------------------------
    updated = re_query_comment(drive_service, doc_id, comment_id)

    # --- Assemble evidence -------------------------------------------------
    evidence = assemble_comment_state_evidence(
        comment=updated,
        applied=True,
        audit_logged=True,
    )
    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab="doc-level",
        tool="reply_to_comment",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


def execute_resolve_comment(
    *,
    drive_service: Any,
    doc_id: str,
    comment_id: str,
) -> dict[str, Any]:
    """Verified resolve pipeline.

    Issues replies.create(action='resolve'), re-queries, verifies the comment
    is actually resolved.  A comment that remains open after the resolve call
    raises VerifyError(COMMENT_STILL_OPEN) — it is never reported as success.
    """
    # --- Resolve ------------------------------------------------------------
    resolve_comment_api(drive_service, doc_id, comment_id)

    # --- Re-query -----------------------------------------------------------
    post_comment = re_query_comment(drive_service, doc_id, comment_id)

    # --- Verify resolved ----------------------------------------------------
    if not post_comment.get("resolved", False):
        raise _make_error(
            ErrorCode.COMMENT_STILL_OPEN,
            f"Comment {comment_id!r} is still open after resolve attempt.",
            {
                "comment_id": comment_id,
                "post_state": post_comment,
            },
        )

    # --- Assemble evidence -------------------------------------------------
    evidence = assemble_comment_state_evidence(
        comment=post_comment,
        applied=True,
        audit_logged=True,
    )
    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab="doc-level",
        tool="resolve_comment",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence


# ---------------------------------------------------------------------------
# Comment-state evidence helper (pure)
# ---------------------------------------------------------------------------


def assemble_comment_state_evidence(
    *,
    comment: dict[str, Any],
    applied: bool,
    audit_logged: bool,
    audit_log_reason: str = "",
) -> dict[str, Any]:
    """Assemble comment-state evidence dict from a formatted comment dict.

    This is a pure transform (no I/O).  The resolve pipeline calls this after
    re-querying; add_anchored_comment and reply_to_comment call it after
    re-querying their respective post-state.

    Returns a dict with keys:
        applied, comment_id, resolved, reply_count, content,
        quoted_text, author, audit_logged,
        audit_log_reason (only when non-empty)
    """
    evidence: dict[str, Any] = {
        "applied": applied,
        "comment_id": comment.get("comment_id", ""),
        "resolved": comment.get("resolved", False),
        "reply_count": comment.get("reply_count", 0),
        "content": comment.get("content", ""),
        "quoted_text": comment.get("quoted_text", ""),
        "author": comment.get("author", ""),
        "audit_logged": audit_logged,
    }
    if audit_log_reason:
        evidence["audit_log_reason"] = audit_log_reason
    return evidence


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _format_comment(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw Drive comments.get / comments.list item."""
    replies = raw.get("replies", [])
    return {
        "comment_id": raw.get("id", ""),
        "content": raw.get("content", ""),
        "resolved": raw.get("resolved", False),
        "reply_count": len(replies),
        "replies": [
            {
                "reply_id": r.get("id", ""),
                "content": r.get("content", ""),
                "action": r.get("action", ""),
                "author": r.get("author", {}).get("displayName", ""),
                "created_time": r.get("createdTime", ""),
            }
            for r in replies
        ],
        "quoted_text": raw.get("quotedFileContent", {}).get("value", ""),
        "author": raw.get("author", {}).get("displayName", ""),
        "created_time": raw.get("createdTime", ""),
        "modified_time": raw.get("modifiedTime", ""),
        "scope": "document",
    }


def _maybe_raise_http_not_found(exc: Exception, comment_id: str) -> None:
    """If exc is a 404 HttpError, raise VerifyError(INVALID_INPUT)."""
    try:
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
    except ImportError:
        return

    if isinstance(exc, HttpError):
        status = exc.resp.status if exc.resp else 0
        if status == 404:
            raise _make_error(
                ErrorCode.INVALID_INPUT,
                f"Comment {comment_id!r} not found.",
                {"comment_id": comment_id, "http_status": status},
            ) from exc
