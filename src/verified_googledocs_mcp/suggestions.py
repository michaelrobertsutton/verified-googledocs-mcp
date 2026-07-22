"""Suggested-edit extraction from Docs API document JSON.

Extract pending suggested edits (insertions, deletions, and style changes)
from a document dict returned by the Docs API with
``suggestionsViewMode=SUGGESTIONS_INLINE``.  The caller is responsible for
fetching the document with that view mode; without it the suggestion-related
fields are absent and this module returns empty results.

Integration point: a later pass inside the ``list_open_items`` tool merges
the results of this module with comment results obtained from the Drive API.
Comments are doc-level (Drive anchors are opaque); suggestions are
per-tab and tab-attributable, which is why the two access paths are kept
separate.

Only the standard library is used — no new dependencies are introduced.
"""

from __future__ import annotations

from typing import Any

from .docs import _available_tab_ids, _find_tab_body, fetch_document_inline
from .verify import ErrorCode, _make_error

# Suggestion kinds that shift text indices. A suggested insertion or deletion
# changes where every subsequent character sits in SUGGESTIONS_INLINE space
# relative to PREVIEW_WITHOUT_SUGGESTIONS space (issue #56); a style-only
# suggestion (suggestedTextStyleChanges / suggestedParagraphStyleChanges,
# reported as kind="style" by extract_suggestions) does not move anything and
# is intentionally allowed through the write guard below.
_INDEX_AFFECTING_KINDS = frozenset({"insertion", "deletion"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_suggestions(
    document_json: dict[str, Any],
    tab_id: str,
) -> list[dict[str, Any]]:
    """Return pending suggested edits for one tab.

    Parameters
    ----------
    document_json:
        A Docs API ``Document`` response dict.  Must have been fetched with
        ``includeTabsContent=True`` and ``suggestionsViewMode=SUGGESTIONS_INLINE``
        so that suggestion-related fields are populated.
    tab_id:
        The target tab.  Pass ``"_body"`` for tabless legacy documents (those
        without a ``tabs`` key); the document's top-level ``body`` is used.

    Returns
    -------
    list of dicts, one per (suggestion_id, kind) pair, with keys:

    - ``suggestion_id`` (str): the suggestion identifier
    - ``kind`` (str): ``"insertion"``, ``"deletion"``, or ``"style"``
    - ``text`` (str): the inserted or deleted text; empty string for style-only changes
    - ``anchor_context`` (str): the full text of the paragraph that contains
      the suggestion, assembled from all runs, so the change is locatable
    - ``tab_id`` (str): the tab this suggestion belongs to

    Raises
    ------
    ValueError
        If ``tab_id`` is not found in the document.  The error message lists
        available tab IDs, matching the convention used in ``docs.py``.
    """
    body = _find_tab_body(document_json, tab_id)
    if body is None:
        available = _available_tab_ids(document_json)
        raise ValueError(f"Tab '{tab_id}' not found. Available tabs: {available}")

    return _collect_suggestions(body, tab_id)


def assert_no_pending_suggestions(
    *,
    service: Any,
    doc_id: str,
    tab_id: str,
    expected_revision: str,
) -> None:
    """Refuse to proceed if the target tab has pending index-affecting suggestions.

    Root cause (issue #56): every mutation pipeline pre-reads with
    ``suggestionsViewMode=PREVIEW_WITHOUT_SUGGESTIONS`` (see ``fetch_document``'s
    docstring — needed so a pending suggestion can't defeat the locator's
    match-count guard from issue #28). But ``batchUpdate`` always mutates the
    document's real index space, which is ``SUGGESTIONS_INLINE``. When the
    target tab has a pending suggested insertion or deletion, those two index
    spaces diverge by the net length of the suggested content, so every index
    a pipeline computes from the PREVIEW read is wrong by that amount — the
    write lands at the wrong offset (often mid-word) and corrupts the
    document. This guard is the pre-write safety valve: refuse instead of
    corrupting.

    Only insertion/deletion suggestions are index-affecting; style-only
    suggestions (bold/italic/paragraph-style suggestions) do not move indices
    and are intentionally allowed through — blocking on those would needlessly
    refuse writes to documents under active style review.

    The INLINE read is attempted at ``expected_revision`` — the revision the
    caller's PREVIEW pre-read captured, and the same revision `batchUpdate`
    will pin via ``requiredRevisionId`` — since checking at a *different*
    revision would validate the wrong snapshot. It is retried once on a
    mismatch, purely as a best-effort nicety.

    If it still disagrees after that, this check is silently skipped (no
    raise) rather than blocking the write, for a specific reason: this
    guard's actual job is narrower than "detect any revision drift." A
    pending suggestion can sit at a perfectly *stable* revision indefinitely
    (nothing needs to move for the PREVIEW-vs-INLINE corruption to occur —
    PREVIEW hides content INLINE reveals, at the very same revision), which
    is exactly the case this guard exists to catch, and it does — reliably,
    since two back-to-back reads of an unchanging document always agree.
    A revision *mismatch* between the two reads means something else
    entirely: the document is being edited right now, by someone else, in
    the narrow window between the caller's PREVIEW read and this INLINE
    read. In that case the pipeline's own upcoming ``batchUpdate`` call —
    which pins ``requiredRevisionId=expected_revision`` — already rejects
    the write with the API's real REVISION_CONFLICT if the revision has
    genuinely moved on, independent of anything this guard does. Raising a
    second, less-specific error here instead would only shadow that more
    accurate, existing signal, without adding any real safety (a moved
    revision is caught either way).

    Raises
    ------
    VerifyError(SUGGESTIONS_PRESENT)
        The target tab has one or more pending insertion/deletion suggestions.
    VerifyError(TAB_NOT_FOUND)
        ``tab_id`` is not present in the INLINE-mode document (should not
        happen if the caller's own PREVIEW pre-read already found the tab,
        short of the document being restructured between reads).
    """
    inline_doc: dict[str, Any] = {}
    matched_revision = False
    for _attempt in range(2):
        inline_doc = fetch_document_inline(service, doc_id)
        if inline_doc.get("revisionId", "") == expected_revision:
            matched_revision = True
            break

    if not matched_revision:
        # The document is changing concurrently; defer to the write's own
        # requiredRevisionId check rather than raising here (see docstring).
        return

    try:
        found = extract_suggestions(inline_doc, tab_id)
    except ValueError as exc:
        raise _make_error(
            ErrorCode.TAB_NOT_FOUND,
            str(exc),
            {"doc_id": doc_id, "tab_id": tab_id},
        ) from exc

    index_affecting = [s for s in found if s["kind"] in _INDEX_AFFECTING_KINDS]
    if index_affecting:
        raise _make_error(
            ErrorCode.SUGGESTIONS_PRESENT,
            (
                f"Tab {tab_id!r} has {len(index_affecting)} pending suggested "
                "insertion(s)/deletion(s). Writing now would compute indices against "
                "the wrong index space and can corrupt the document (issue #56). "
                "Accept or reject the pending suggestions in the Docs UI first, then retry."
            ),
            {
                "doc_id": doc_id,
                "tab_id": tab_id,
                "suggestion_count": len(index_affecting),
                "suggestion_ids": [s["suggestion_id"] for s in index_affecting],
            },
        )


# ---------------------------------------------------------------------------
# Suggestion collection
# ---------------------------------------------------------------------------


def _collect_suggestions(
    body: dict[str, Any],
    tab_id: str,
) -> list[dict[str, Any]]:
    """Walk body content recursively and collect suggestion entries."""
    # Use a dict keyed by (suggestion_id, kind) to accumulate text across
    # multiple runs that share the same suggestion ID.  A suggested
    # replacement is represented as one deletion entry and one insertion entry
    # sharing the same suggestion_id — the human/integration pass sees them as
    # a pair.
    accumulator: dict[tuple[str, str], dict[str, Any]] = {}

    for content_elem in body.get("content", []):
        if "paragraph" in content_elem:
            _process_paragraph(content_elem["paragraph"], tab_id, accumulator)
        elif "table" in content_elem:
            _process_table(content_elem["table"], tab_id, accumulator)

    return list(accumulator.values())


def _process_paragraph(
    para: dict[str, Any],
    tab_id: str,
    accumulator: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Extract suggestions from one paragraph and its elements."""
    # Build the full text of the paragraph for anchor context.
    anchor_context = _paragraph_full_text(para)

    for elem in para.get("elements", []):
        # Text runs carry suggestedInsertionIds / suggestedDeletionIds
        # (arrays) and suggestedTextStyleChanges (map keyed by suggestion id).
        text_run = elem.get("textRun", {})
        if text_run:
            run_text = text_run.get("content", "")

            for sid in text_run.get("suggestedInsertionIds", []):
                _accumulate(
                    accumulator,
                    sid,
                    "insertion",
                    run_text,
                    anchor_context,
                    tab_id,
                )

            for sid in text_run.get("suggestedDeletionIds", []):
                _accumulate(
                    accumulator,
                    sid,
                    "deletion",
                    run_text,
                    anchor_context,
                    tab_id,
                )

            for sid in text_run.get("suggestedTextStyleChanges", {}).keys():
                # Style-only changes carry no text content of their own.
                _accumulate(
                    accumulator,
                    sid,
                    "style",
                    run_text,
                    anchor_context,
                    tab_id,
                )

    # Paragraph-level style suggestions live on the paragraph itself.
    for sid in para.get("suggestedParagraphStyleChanges", {}).keys():
        _accumulate(
            accumulator,
            sid,
            "style",
            "",
            anchor_context,
            tab_id,
        )


def _process_table(
    table: dict[str, Any],
    tab_id: str,
    accumulator: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """Recurse into table cells to find suggestions there too."""
    for row in table.get("tableRows", []):
        for cell in row.get("tableCells", []):
            for content_elem in cell.get("content", []):
                if "paragraph" in content_elem:
                    _process_paragraph(content_elem["paragraph"], tab_id, accumulator)
                elif "table" in content_elem:
                    _process_table(content_elem["table"], tab_id, accumulator)


def _accumulate(
    accumulator: dict[tuple[str, str], dict[str, Any]],
    sid: str,
    kind: str,
    text: str,
    anchor_context: str,
    tab_id: str,
) -> None:
    key = (sid, kind)
    if key not in accumulator:
        accumulator[key] = {
            "suggestion_id": sid,
            "kind": kind,
            "text": text,
            "anchor_context": anchor_context,
            "tab_id": tab_id,
        }
    else:
        # Append text for multi-run suggestions (same id spans several runs).
        accumulator[key]["text"] += text


def _paragraph_full_text(para: dict[str, Any]) -> str:
    """Return the full plain text of a paragraph by joining all run contents."""
    parts: list[str] = []
    for elem in para.get("elements", []):
        text_run = elem.get("textRun", {})
        if text_run:
            parts.append(text_run.get("content", ""))
    return "".join(parts).rstrip("\n")
