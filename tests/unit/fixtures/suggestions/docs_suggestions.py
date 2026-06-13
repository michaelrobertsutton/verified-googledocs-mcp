"""Synthetic Docs API fixtures for suggestion extraction tests.

All shapes match the Docs REST API v1 document structure with
suggestionsViewMode=SUGGESTIONS_INLINE applied.

Reference:
  https://developers.google.com/docs/api/reference/rest/v1/documents#TextRun
  https://developers.google.com/docs/api/reference/rest/v1/documents#SuggestedTextStyle

Key suggestion fields on TextRun:
  suggestedInsertionIds   list[str]  — non-empty means this run is a pending insertion
  suggestedDeletionIds    list[str]  — non-empty means this run is a pending deletion
  suggestedTextStyleChanges  dict[str, SuggestedTextStyle]  — keyed by suggestion id

Key suggestion fields on Paragraph:
  suggestedParagraphStyleChanges  dict[str, SuggestedParagraphStyle]  — keyed by suggestion id
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Primitive builders (minimal — only what suggestion tests need)
# ---------------------------------------------------------------------------

def _text_run(
    text: str,
    insertion_ids: list[str] | None = None,
    deletion_ids: list[str] | None = None,
    style_suggestion_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a textRun element, optionally with suggestion ids."""
    tr: dict[str, Any] = {"content": text}
    if insertion_ids:
        tr["suggestedInsertionIds"] = insertion_ids
    if deletion_ids:
        tr["suggestedDeletionIds"] = deletion_ids
    if style_suggestion_ids:
        tr["suggestedTextStyleChanges"] = {sid: {} for sid in style_suggestion_ids}
    return {"textRun": tr}


def _paragraph(
    elements: list[dict[str, Any]],
    para_style_suggestion_ids: list[str] | None = None,
) -> dict[str, Any]:
    para: dict[str, Any] = {
        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        "elements": elements,
    }
    if para_style_suggestion_ids:
        para["suggestedParagraphStyleChanges"] = {
            sid: {} for sid in para_style_suggestion_ids
        }
    return {"paragraph": para}


def _body(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"content": content}


def _tab(tab_id: str, title: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "tabProperties": {"tabId": tab_id, "title": title, "index": 0},
        "documentTab": {"body": body},
        "childTabs": [],
    }


# ---------------------------------------------------------------------------
# Fixture: document with a single suggested insertion
# ---------------------------------------------------------------------------

def doc_with_insertion() -> dict[str, Any]:
    """One tab, one paragraph with a pending suggested insertion.

    Suggestion id: "ins-001"
    Inserted text: "new text"
    Surrounding paragraph text: "Before new text after"
    """
    body = _body([
        _paragraph([
            _text_run("Before "),
            _text_run("new text", insertion_ids=["ins-001"]),
            _text_run(" after"),
        ])
    ])
    return {
        "documentId": "doc-insertion",
        "revisionId": "rev-ins",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }


# ---------------------------------------------------------------------------
# Fixture: document with a single suggested deletion
# ---------------------------------------------------------------------------

def doc_with_deletion() -> dict[str, Any]:
    """One tab, one paragraph with a pending suggested deletion.

    Suggestion id: "del-001"
    Deleted text: "old text"
    Surrounding paragraph text: "Keep old text here"
    """
    body = _body([
        _paragraph([
            _text_run("Keep "),
            _text_run("old text", deletion_ids=["del-001"]),
            _text_run(" here"),
        ])
    ])
    return {
        "documentId": "doc-deletion",
        "revisionId": "rev-del",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }


# ---------------------------------------------------------------------------
# Fixture: document with a suggested replacement (same id, deletion + insertion)
# ---------------------------------------------------------------------------

def doc_with_replacement() -> dict[str, Any]:
    """One tab, one paragraph with a suggested replacement.

    A replacement is modeled as one run with the old text (suggestedDeletionIds)
    and one run with the new text (suggestedInsertionIds), sharing the same id.

    Suggestion id: "rep-001"
    Deleted text: "wrong"
    Inserted text: "right"
    """
    body = _body([
        _paragraph([
            _text_run("Say "),
            _text_run("wrong", deletion_ids=["rep-001"]),
            _text_run("right", insertion_ids=["rep-001"]),
            _text_run(" always"),
        ])
    ])
    return {
        "documentId": "doc-replacement",
        "revisionId": "rev-rep",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }


# ---------------------------------------------------------------------------
# Fixture: document with a suggested style change
# ---------------------------------------------------------------------------

def doc_with_style_suggestion() -> dict[str, Any]:
    """One tab, one paragraph where a run has a suggested text-style change.

    Suggestion id: "sty-001"
    The run text is "styled word" — no text addition or removal.
    """
    body = _body([
        _paragraph([
            _text_run("Before "),
            _text_run("styled word", style_suggestion_ids=["sty-001"]),
            _text_run(" after"),
        ])
    ])
    return {
        "documentId": "doc-style",
        "revisionId": "rev-sty",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }


# ---------------------------------------------------------------------------
# Fixture: document with a suggested paragraph-style change
# ---------------------------------------------------------------------------

def doc_with_para_style_suggestion() -> dict[str, Any]:
    """One tab, one paragraph with a suggested paragraph style change.

    Suggestion id: "psty-001" on the paragraph itself.
    """
    body = _body([
        _paragraph(
            [_text_run("Heading candidate text")],
            para_style_suggestion_ids=["psty-001"],
        )
    ])
    return {
        "documentId": "doc-para-style",
        "revisionId": "rev-psty",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }


# ---------------------------------------------------------------------------
# Fixture: tab with no suggestions
# ---------------------------------------------------------------------------

def doc_with_no_suggestions() -> dict[str, Any]:
    """Two tabs; neither has suggestions.  Both should return empty lists."""
    body_a = _body([_paragraph([_text_run("Plain text in tab A")])])
    body_b = _body([_paragraph([_text_run("Plain text in tab B")])])
    return {
        "documentId": "doc-no-suggestions",
        "revisionId": "rev-none",
        "tabs": [
            _tab("tab-a", "Tab A", body_a),
            _tab("tab-b", "Tab B", body_b),
        ],
    }


# ---------------------------------------------------------------------------
# Fixture: tabless document with a suggestion
# ---------------------------------------------------------------------------

def tabless_doc_with_suggestion() -> dict[str, Any]:
    """A tabless (legacy) document — no 'tabs' key — with a pending insertion.

    Should be accessible via tab_id="_body".

    Suggestion id: "ins-legacy"
    Inserted text: "legacy insert"
    """
    body = _body([
        _paragraph([
            _text_run("Start "),
            _text_run("legacy insert", insertion_ids=["ins-legacy"]),
            _text_run(" end"),
        ])
    ])
    return {
        "documentId": "doc-tabless-suggestion",
        "revisionId": "rev-tl",
        "body": body,
    }


# ---------------------------------------------------------------------------
# Fixture: document mixing suggestion with normal text
# ---------------------------------------------------------------------------

def doc_mixed_suggestion_and_normal() -> dict[str, Any]:
    """One tab with two paragraphs: one with a suggestion, one without.

    Only the paragraph with the suggestion should contribute results.

    Suggestion id: "mix-001"
    Inserted text: "extra"
    """
    body = _body([
        _paragraph([_text_run("Normal paragraph, no suggestion here")]),
        _paragraph([
            _text_run("Before "),
            _text_run("extra", insertion_ids=["mix-001"]),
            _text_run(" after"),
        ]),
    ])
    return {
        "documentId": "doc-mixed",
        "revisionId": "rev-mix",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }


# ---------------------------------------------------------------------------
# Fixture: suggestion inside a table cell
# ---------------------------------------------------------------------------

def doc_with_table_cell_suggestion() -> dict[str, Any]:
    """One tab with a table whose cell contains a pending suggested deletion.

    Suggestion id: "tbl-del-001"
    Deleted text: "cell text"
    """
    cell_para = _paragraph([
        _text_run("cell text", deletion_ids=["tbl-del-001"]),
    ])
    table = {
        "table": {
            "rows": 1,
            "columns": 1,
            "tableRows": [
                {
                    "tableCells": [
                        {"content": [cell_para]}
                    ]
                }
            ],
        }
    }
    body = _body([table])
    return {
        "documentId": "doc-table-suggestion",
        "revisionId": "rev-tbl",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }


# ---------------------------------------------------------------------------
# Fixture: multi-run insertion (same suggestion id spans two runs)
# ---------------------------------------------------------------------------

def doc_with_multirun_insertion() -> dict[str, Any]:
    """One tab, one paragraph where a single suggestion id spans two consecutive runs.

    The two runs should have their text concatenated in the output.

    Suggestion id: "mri-001"
    Full inserted text: "first part second part"
    """
    body = _body([
        _paragraph([
            _text_run("first part", insertion_ids=["mri-001"]),
            _text_run(" second part", insertion_ids=["mri-001"]),
        ])
    ])
    return {
        "documentId": "doc-multirun",
        "revisionId": "rev-mri",
        "tabs": [_tab("tab-a", "Tab A", body)],
    }
