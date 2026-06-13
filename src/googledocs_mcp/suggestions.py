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

from .docs import _available_tab_ids, _find_tab_body


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
        raise ValueError(
            f"Tab '{tab_id}' not found. Available tabs: {available}"
        )

    return _collect_suggestions(body, tab_id)


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
                    _process_paragraph(
                        content_elem["paragraph"], tab_id, accumulator
                    )
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
