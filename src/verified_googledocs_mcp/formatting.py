"""Verified format_text pipeline: apply character styling (bold/italic/underline)
to a text-matched span via updateTextStyle only — no content mutation.

  validate → pre-read (R1) → locate → no-op check → [dry_run] →
  batchUpdate(requiredRevisionId=R1, updateTextStyle only) → post-read (R2) →
  post-write locate + style check → evidence → audit

format_text lives in its own module rather than mutations.py because
markdown_mutations.py already imports ``_translate_http_error`` from
mutations.py; mutations.py importing back from markdown_mutations.py
(``_stamp_tab_id`` / ``_tab_extent`` / ``_simulate_or_raise``) would create an
import cycle. tables.py solved the identical problem the same way — as a peer
module importing from both. API calls live here; verify.py stays pure.
"""

from __future__ import annotations

from typing import Any

from .docs import _available_tab_ids, _find_tab_body, fetch_document
from .markdown_mutations import (
    _raise_post_write_verification_failure,
    _simulate_or_raise,
    _stamp_tab_id,
    _tab_extent,
)
from .mutations import _translate_http_error
from .suggestions import assert_no_pending_suggestions
from .verify import (
    _STYLE_ALLOWLIST,
    ErrorCode,
    LocateResult,
    VerifyError,
    _collect_style_runs,
    _make_error,
    _style_matches,
    append_audit,
    assemble_format_text_evidence,
    locate,
)


def _validate_style(style: Any) -> dict[str, bool]:
    """Validate the ``style`` argument, raising INVALID_INPUT on any problem.

    Requires a non-empty dict, keys drawn from ``_STYLE_ALLOWLIST``, and every
    value an *exact* bool — ``type(v) is bool``, not ``isinstance``, since
    ``isinstance(True, int)`` is also true and would silently accept ``1``/``0``
    as if they were ``True``/``False``.
    """
    if not isinstance(style, dict):
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            "style must be an object mapping bold/italic/underline to true/false",
            {"style": repr(style)},
        )
    if not style:
        raise _make_error(ErrorCode.INVALID_INPUT, "style must not be empty")

    unknown = sorted(set(style) - set(_STYLE_ALLOWLIST))
    if unknown:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"unknown style key(s): {unknown}; allowed: {list(_STYLE_ALLOWLIST)}",
            {"unknown_keys": unknown},
        )

    non_bool = {k: repr(v) for k, v in style.items() if type(v) is not bool}
    if non_bool:
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"style values must be true/false booleans, got: {non_bool}",
            {"invalid_values": non_bool},
        )

    return style


def _build_format_requests(
    spans: list[tuple[int, int]],
    style: dict[str, bool],
    tab_id: str,
) -> list[dict[str, Any]]:
    """Build updateTextStyle-only requests, one per span.

    Carries every requested field's value verbatim — including ``False`` — in
    both ``textStyle`` and ``fields``, so ``{"bold": false}`` actually clears
    bold. This is deliberately unlike tables.py's replace_table_row, whose
    ``truthy_fields`` filter is correct THERE because it is carrying over
    EXISTING style onto new text; here the caller is asking for an explicit
    value, so a filtered-out ``False`` would silently fail to un-bold.
    """
    fields = [k for k in _STYLE_ALLOWLIST if k in style]
    requests: list[dict[str, Any]] = [
        {
            "updateTextStyle": {
                "range": {"startIndex": s, "endIndex": e},
                "textStyle": {k: style[k] for k in fields},
                "fields": ",".join(fields),
            }
        }
        for s, e in spans
    ]
    _stamp_tab_id(requests, tab_id)
    return requests


def _assert_structural_purity(requests: list[dict[str, Any]]) -> None:
    """Assert every compiled request is updateTextStyle-only.

    Defense in depth for this tool's core promise (no ``insertTable`` /
    ``deleteContentRange`` / merge ops): a bug in ``_build_format_requests``
    that ever emitted a structural request would corrupt content instead of
    merely mis-styling it, so this asserts rather than silently trusting the
    builder. ``_stamp_tab_id`` mutates each request's nested ``range`` dict in
    place; it never adds a sibling top-level key, so a clean request is always
    exactly ``{"updateTextStyle": {...}}``.
    """
    for req in requests:
        assert set(req.keys()) == {"updateTextStyle"}, (
            f"format_text compiled a non-style request: {req!r}"
        )


def execute_format_text(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    find: str,
    style: dict[str, bool],
    expected_matches: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Full verified-write pipeline for format_text.

    Compiles ONLY updateTextStyle requests over the matched span(s) — no
    content mutation, so it is safe inside merged-cell tables by construction
    (see ``_assert_structural_purity``; the offline index simulator only
    bounds-checks indices, it knows nothing about merge geometry).

    Raises VerifyError on any verification failure. Raises other exceptions
    for unexpected API errors. Returns the evidence dict (same shape
    regardless of dry_run or the no-op path).
    """
    style = _validate_style(style)

    if not find:
        raise _make_error(ErrorCode.INVALID_INPUT, "find must not be empty")

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

    # --- Suggestion guard (issue #56) ---------------------------------------
    # Style-only suggestions overlapping the target span are a pre-existing,
    # accepted risk shared by every mutating tool here (suggestions.py's
    # _INDEX_AFFECTING_KINDS deliberately excludes them) — format_text does
    # not special-case it. This guard only blocks index-affecting suggestions.
    assert_no_pending_suggestions(
        service=service, doc_id=doc_id, tab_id=tab_id, expected_revision=revision_before
    )

    pre_tab_json = {"body": body}

    # --- Locate --------------------------------------------------------------
    locate_result: LocateResult = locate(find, pre_tab_json, expected_matches)

    runs_before = _collect_style_runs(pre_tab_json, locate_result.spans)

    # --- Dry run ---------------------------------------------------------------
    if dry_run:
        # Predict runs_after by overlaying the requested fields onto
        # runs_before — the style analogue of assemble_text_edit_evidence's
        # predicted_replacement splice. No write is issued.
        runs_after = [[{**run, **style} for run in span_runs] for span_runs in runs_before]
        evidence = assemble_format_text_evidence(
            style=style,
            match_count=locate_result.match_count,
            rung=locate_result.rung,
            spans=locate_result.spans,
            runs_before=runs_before,
            runs_after=runs_after,
            revision_before=revision_before,
            revision_after="",
            applied=False,
            compiled_request_kinds=[],
            audit_logged=False,
            audit_log_reason="dry_run",
            dry_run=True,
        )
        audit_ok, audit_reason = append_audit(
            doc=doc_id,
            tab=tab_id,
            tool="format_text",
            evidence=evidence,
        )
        evidence["audit_logged"] = audit_ok
        if not audit_ok:
            evidence["audit_log_reason"] = audit_reason
        return evidence

    # --- No-op check ---------------------------------------------------------
    # If every located run already carries every requested field's value,
    # skip batchUpdate and the post-read entirely: an idempotent re-run must
    # not create a new Docs revision, or revision_before != revision_after
    # would make _raise_post_write_verification_failure's document_mutated /
    # needs_manual_restore diagnostics misleading on a call that changed
    # nothing (issue #63's idempotency requirement — this IS the success
    # case, not an error).
    if _style_matches(runs_before, style):
        evidence = assemble_format_text_evidence(
            style=style,
            match_count=locate_result.match_count,
            rung=locate_result.rung,
            spans=locate_result.spans,
            runs_before=runs_before,
            runs_after=runs_before,
            revision_before=revision_before,
            revision_after=revision_before,
            applied=True,
            compiled_request_kinds=[],
            audit_logged=True,
            audit_log_reason="",
        )
        audit_ok, audit_reason = append_audit(
            doc=doc_id,
            tab=tab_id,
            tool="format_text",
            evidence=evidence,
        )
        evidence["audit_logged"] = audit_ok
        if not audit_ok:
            evidence["audit_log_reason"] = audit_reason
        return evidence

    # --- Build + pre-flight ---------------------------------------------------
    requests = _build_format_requests(locate_result.spans, style, tab_id)
    _assert_structural_purity(requests)
    tab_start, tab_end = _tab_extent(body)
    _simulate_or_raise(requests, tab_start=tab_start, tab_end=tab_end)

    # --- batchUpdate with requiredRevisionId ----------------------------------
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

    # --- Post-read -------------------------------------------------------------
    post_doc = fetch_document(service, doc_id)
    revision_after = post_doc.get("revisionId", "")

    post_body = _find_tab_body(post_doc, tab_id)
    post_tab_json: dict[str, Any] = {"body": post_body} if post_body is not None else {"body": {}}

    # --- Post-write verification -----------------------------------------------
    # Re-locate on the post-read rather than reusing the pre-write spans: this
    # both confirms the target text is still byte-identical and stays correct
    # if a concurrent edit elsewhere in the tab shifted indices between the
    # write and this read (updateTextStyle itself shifts nothing, but nothing
    # stops a human from editing the doc in that window). A raw ZERO_MATCH /
    # MATCH_COUNT_MISMATCH must not escape here: the write already landed, so
    # leaking a pre-write-shaped error would hand the caller a misleading
    # invitation to retry (issue #63) instead of the VERIFICATION_FAILED this
    # actually is.
    try:
        post_locate_result = locate(find, post_tab_json, expected_matches)
    except VerifyError as exc:
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool="format_text",
            message=(
                "Post-write verification could not re-locate the matched text after "
                "the style write. The style write likely landed, but this cannot "
                "confirm the target text is unchanged. Note: expected_matches > 1 "
                "guarantees only that all CURRENTLY-present occurrences of `find` "
                "carry the requested style, not that they are the exact occurrences "
                "targeted, if a concurrent edit added or removed a copy of the text. "
                "This is most often caused by a concurrent edit between the write "
                "and the re-read, not by this tool's own write — check Docs version "
                "history before assuming a manual restore is needed."
            ),
            evidence={
                "revision_before": revision_before,
                "revision_after": revision_after,
                "style": style,
            },
            extra_diagnostics={"post_locate_error": exc.envelope.to_dict()},
        )

    runs_after = _collect_style_runs(post_tab_json, post_locate_result.spans)
    if not _style_matches(runs_after, style):
        _raise_post_write_verification_failure(
            doc_id=doc_id,
            tab_id=tab_id,
            tool="format_text",
            message="Post-write verification failed: the requested style was not confirmed on re-read.",
            evidence={
                "revision_before": revision_before,
                "revision_after": revision_after,
                "style": style,
                "runs_after": runs_after,
            },
        )

    evidence = assemble_format_text_evidence(
        style=style,
        match_count=locate_result.match_count,
        rung=locate_result.rung,
        spans=locate_result.spans,
        runs_before=runs_before,
        runs_after=runs_after,
        revision_before=revision_before,
        revision_after=revision_after,
        applied=True,
        compiled_request_kinds=sorted({k for r in requests for k in r}),
        audit_logged=True,
        audit_log_reason="",
    )

    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab=tab_id,
        tool="format_text",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence
