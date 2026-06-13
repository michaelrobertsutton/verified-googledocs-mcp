"""Synthetic fixtures for markdown tool tests.

Builds minimal Docs API response dicts for testing the markdown write tools
without any network calls or real credentials.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Primitive builders
# ---------------------------------------------------------------------------


def _text_run(text: str) -> dict[str, Any]:
    return {"textRun": {"content": text}}


def _para(
    text: str,
    start: int,
    end: int | None = None,
    style: str = "NORMAL_TEXT",
) -> dict[str, Any]:
    if end is None:
        end = start + len(text)
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text},
                }
            ],
        },
    }


def _heading_para(level: int, text: str, start: int, end: int | None = None) -> dict[str, Any]:
    return _para(text + "\n", start, end, style=f"HEADING_{level}")


def _inline_image_para(obj_id: str, start: int, end: int) -> dict[str, Any]:
    """A paragraph containing an inline image element."""
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "inlineObjectElement": {"inlineObjectId": obj_id},
                }
            ],
        },
    }


def _table_elem(start: int, end: int) -> dict[str, Any]:
    """A minimal table element."""
    return {
        "startIndex": start,
        "endIndex": end,
        "table": {
            "rows": 2,
            "columns": 2,
            "tableRows": [
                {
                    "tableCells": [
                        {"content": [_para("Header A\n", start + 2, start + 11)]},
                        {"content": [_para("Header B\n", start + 13, start + 22)]},
                    ]
                },
                {
                    "tableCells": [
                        {"content": [_para("Cell 1\n", start + 24, start + 31)]},
                        {"content": [_para("Cell 2\n", start + 33, start + 40)]},
                    ]
                },
            ],
        },
    }


def _chip_para(email: str, start: int, end: int) -> dict[str, Any]:
    """A paragraph containing a person chip."""
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "person": {"personProperties": {"email": email, "name": "User"}},
                }
            ],
        },
    }


def _footnote_para(fn_id: str, start: int, end: int) -> dict[str, Any]:
    """A paragraph containing a footnote reference."""
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {"startIndex": start, "endIndex": start + 5, "textRun": {"content": "Text\n"}},
                {
                    "startIndex": start + 5,
                    "endIndex": end,
                    "footnoteReference": {"footnoteId": fn_id, "footnoteNumber": "1"},
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Document fixtures
# ---------------------------------------------------------------------------


def simple_markdown_doc(text: str = "Hello world", revision: str = "rev-1") -> dict[str, Any]:
    """Single-tab doc with one paragraph."""
    raw = text + "\n"
    end = 1 + len(raw)
    body = {"content": [_para(raw, 1, end)]}
    return {
        "documentId": "doc-test",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": body},
                "childTabs": [],
            }
        ],
    }


def doc_with_heading_and_table(revision: str = "rev-1") -> dict[str, Any]:
    """Doc with a heading paragraph at [1,20) and a table at [20,80)."""
    body = {
        "content": [
            _heading_para(1, "Introduction", 1, 15),
            _table_elem(15, 60),
        ]
    }
    return {
        "documentId": "doc-table",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": body},
                "childTabs": [],
            }
        ],
    }


def doc_with_image(revision: str = "rev-1") -> dict[str, Any]:
    """Doc with a heading and an inline image paragraph."""
    body = {
        "content": [
            _heading_para(1, "My Doc", 1, 9),
            _para("Paragraph with anchor text\n", 9, 36),
            _inline_image_para("img-001", 36, 38),
        ]
    }
    return {
        "documentId": "doc-image",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": body},
                "childTabs": [],
            }
        ],
    }


def doc_with_chip(revision: str = "rev-1") -> dict[str, Any]:
    """Doc with a person chip."""
    body = {
        "content": [
            _heading_para(1, "Team", 1, 7),
            _chip_para("alice@example.com", 7, 10),
        ]
    }
    return {
        "documentId": "doc-chip",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": body},
                "childTabs": [],
            }
        ],
    }


def doc_with_footnote(revision: str = "rev-1") -> dict[str, Any]:
    """Doc with a footnote reference."""
    body = {
        "content": [
            _heading_para(1, "Notes", 1, 8),
            _footnote_para("fn-1", 8, 16),
        ]
    }
    return {
        "documentId": "doc-footnote",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": body},
                "childTabs": [],
            }
        ],
    }


def doc_with_outside_table(revision: str = "rev-1") -> dict[str, Any]:
    """Doc with a paragraph at [1,30) and a table at [30,70) outside any write range."""
    body = {
        "content": [
            _para("Edit me\n", 1, 9),
            _table_elem(30, 70),
        ]
    }
    return {
        "documentId": "doc-outside-table",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": body},
                "childTabs": [],
            }
        ],
    }
