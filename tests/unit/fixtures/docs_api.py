"""Synthetic Docs API response fixtures.

All fixtures are built from the Docs REST API documented shape — no network
calls and no real credentials are required. Tests import these dicts directly.

Shape reference:
  https://developers.google.com/docs/api/reference/rest/v1/documents#Document
  https://developers.google.com/docs/api/reference/rest/v1/documents#Tab

Key fields used:
  document.tabs[].tabProperties.{tabId, title, index}
  document.tabs[].documentTab.body.content[]
  document.tabs[].childTabs[]
  document.body  (tabless docs — no .tabs key)
  document.revisionId
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Primitive builders
# ---------------------------------------------------------------------------


def _text_run(text: str, bold: bool = False, italic: bool = False, url: str = "") -> dict[str, Any]:
    ts: dict[str, Any] = {}
    if bold:
        ts["bold"] = True
    if italic:
        ts["italic"] = True
    if url:
        ts["link"] = {"url": url}
    elem: dict[str, Any] = {
        "textRun": {
            "content": text,
        }
    }
    if ts:
        elem["textRun"]["textStyle"] = ts
    return elem


def _paragraph(
    elements: list[dict[str, Any]],
    style: str = "NORMAL_TEXT",
    start: int = 0,
    end: int = 0,
    bullet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    para: dict[str, Any] = {
        "paragraphStyle": {"namedStyleType": style},
        "elements": elements,
    }
    if bullet is not None:
        para["bullet"] = bullet
    return {"startIndex": start, "endIndex": end, "paragraph": para}


def _heading(level: int, text: str, start: int = 0, end: int = 0) -> dict[str, Any]:
    return _paragraph(
        [_text_run(text + "\n")],
        style=f"HEADING_{level}",
        start=start,
        end=end,
    )


def _normal(text: str, start: int = 0, end: int = 0) -> dict[str, Any]:
    return _paragraph([_text_run(text + "\n")], start=start, end=end)


def _bullet_item(text: str, nesting: int = 0) -> dict[str, Any]:
    return _paragraph(
        [_text_run(text + "\n")],
        bullet={"nestingLevel": nesting},
    )


def _table_row(cells: list[str]) -> dict[str, Any]:
    return {"tableCells": [{"content": [_paragraph([_text_run(c + "\n")])]} for c in cells]}


def _table(rows: list[list[str]]) -> dict[str, Any]:
    return {
        "table": {
            "rows": len(rows),
            "columns": len(rows[0]) if rows else 0,
            "tableRows": [_table_row(r) for r in rows],
        }
    }


# ---------------------------------------------------------------------------
# Multi-tab document fixture
# ---------------------------------------------------------------------------


def multi_tab_doc() -> dict[str, Any]:
    """A two-tab document with headings, bold/italic text, a list, and a table.

    Tab IDs: "tab-1", "tab-2"
    revisionId: "rev-001"
    """
    tab1_body = {
        "content": [
            _heading(1, "Introduction", start=1, end=16),
            _normal("Plain paragraph here.", start=16, end=38),
            _paragraph(
                [
                    _text_run("This is "),
                    _text_run("bold", bold=True),
                    _text_run(" and "),
                    _text_run("italic", italic=True),
                    _text_run(" text.\n"),
                ],
                start=38,
                end=64,
            ),
            _heading(2, "Methods", start=64, end=73),
            _bullet_item("First item"),
            _bullet_item("Second item"),
            {
                "startIndex": 95,
                "endIndex": 130,
                **_table([["Header A", "Header B"], ["Cell 1", "Cell 2"]]),
            },
        ]
    }

    tab2_body = {
        "content": [
            _heading(1, "Results", start=1, end=10),
            _normal("Some results here.", start=10, end=29),
            _heading(2, "Discussion", start=29, end=41),
            _normal(
                "Link example: see ",
                start=41,
                end=60,
            ),
            _paragraph(
                [
                    _text_run("see "),
                    _text_run("Google", url="https://google.com"),
                    _text_run(" for more.\n"),
                ],
                start=60,
                end=82,
            ),
        ]
    }

    return {
        "documentId": "doc-multi-tab",
        "revisionId": "rev-001",
        "title": "Multi-tab test document",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab One", "index": 0},
                "documentTab": {"body": tab1_body},
                "childTabs": [],
            },
            {
                "tabProperties": {"tabId": "tab-2", "title": "Tab Two", "index": 1},
                "documentTab": {"body": tab2_body},
                "childTabs": [],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Tabless (legacy) document fixture
# ---------------------------------------------------------------------------


def tabless_doc() -> dict[str, Any]:
    """A document without any 'tabs' key — a legacy / pre-tabs document.

    Should be treated as a single implicit tab with id "_body".
    revisionId: "rev-legacy"
    """
    body = {
        "content": [
            _heading(1, "Legacy Document", start=1, end=18),
            _normal("This document has no tab metadata.", start=18, end=52),
        ]
    }
    return {
        "documentId": "doc-tabless",
        "revisionId": "rev-legacy",
        "title": "Legacy document",
        "body": body,
    }


# ---------------------------------------------------------------------------
# Lossy-elements fixture
# ---------------------------------------------------------------------------


def lossy_elements_doc() -> dict[str, Any]:
    """A one-tab document containing an inline image, a person chip, and a footnote.

    These elements cannot be expressed in markdown and must appear as
    placeholder tokens in the read_document output.
    """
    body = {
        "content": [
            _heading(1, "Lossy Elements", start=1, end=17),
            {
                "startIndex": 17,
                "endIndex": 40,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [
                        _text_run("Before image "),
                        {"inlineObjectElement": {"inlineObjectId": "obj-abc123"}},
                        _text_run(" after image.\n"),
                    ],
                },
            },
            {
                "startIndex": 40,
                "endIndex": 65,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [
                        _text_run("Mention: "),
                        {
                            "person": {
                                "personProperties": {
                                    "email": "alice@example.com",
                                    "name": "Alice",
                                }
                            }
                        },
                        _text_run(" end.\n"),
                    ],
                },
            },
            {
                "startIndex": 65,
                "endIndex": 90,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [
                        _text_run("Text with footnote"),
                        {"footnoteReference": {"footnoteId": "fn-1", "footnoteNumber": "1"}},
                        _text_run(".\n"),
                    ],
                },
            },
        ]
    }
    return {
        "documentId": "doc-lossy",
        "revisionId": "rev-lossy",
        "title": "Lossy elements document",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-main", "title": "Main", "index": 0},
                "documentTab": {"body": body},
                "childTabs": [],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Nested tabs fixture
# ---------------------------------------------------------------------------


def nested_tabs_doc() -> dict[str, Any]:
    """A document with a parent tab containing a child tab."""
    parent_body = {"content": [_heading(1, "Parent Tab", start=1, end=13)]}
    child_body = {"content": [_heading(1, "Child Tab", start=1, end=12)]}
    return {
        "documentId": "doc-nested",
        "revisionId": "rev-nested",
        "title": "Nested tabs",
        "tabs": [
            {
                "tabProperties": {"tabId": "parent-tab", "title": "Parent", "index": 0},
                "documentTab": {"body": parent_body},
                "childTabs": [
                    {
                        "tabProperties": {
                            "tabId": "child-tab",
                            "title": "Child",
                            "index": 0,
                        },
                        "documentTab": {"body": child_body},
                        "childTabs": [],
                    }
                ],
            }
        ],
    }
