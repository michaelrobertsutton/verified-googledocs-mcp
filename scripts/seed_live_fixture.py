#!/usr/bin/env python
"""Seed a new canonical fixture doc for the live acceptance suite.

Creates a Google Doc carrying every piece of substrate tests/live pins
(see docs/fixture-session.md): the tab-1 hazard prose with a "Text Hazards"
HEADING_1 at range [1, 14), a second "Unicode Hazards" tab with the UTF-16
hazard set, a "Nested Tab" child of t.0, and two open comment threads (one
with a reply). Prints the ids to copy into tests/live/conftest.py and
tests/live/test_comments.py.

The one thing this script cannot seed is the two pending suggested edits:
creating suggestions requires the Docs UI (Suggesting mode) unless enrolled
in the Workspace Developer Preview Program. Make one suggested insertion and
one suggested deletion by hand, then update SEEDED_SUGGESTION_IDS.

Prerequisites:
    - OAuth credentials + cached token (run `verified-googledocs-mcp auth`).

Usage:
    uv run python scripts/seed_live_fixture.py
"""

from __future__ import annotations

from typing import Any

from verified_googledocs_mcp.auth import get_credentials
from verified_googledocs_mcp.comments import build_drive_service
from verified_googledocs_mcp.docs import build_docs_service

# Tab 1: every normalization-ladder and match-guard hazard the suite probes.
# Invisible characters are written as escapes so they survive editors:
#   = NBSP, ­ = soft hyphen. The duplicated fox sentence must appear
# exactly twice, the second time in the same paragraph as [rev-probe], so the
# "dog.\nThe quick" boundary triggers STRUCTURAL_BOUNDARY.
TAB1_TEXT = (
    "Text Hazards\n"
    "\n"
    "Curly quotes: “Hello, world!” and ‘single’ quotes.\n"
    "Non-breaking space: before after\n"
    "Soft hyphen: super­seded\n"
    "\n"
    "Duplicate sentence test:\n"
    "The quick brown fox jumps over the lazy dog.\n"
    "The quick brown fox jumps over the lazy dog. [rev-probe]"
)

# "Text Hazards" occupies [1, 13); its paragraph (with newline) is [1, 14).
HEADING_RANGE = {"startIndex": 1, "endIndex": 14}

# Tab 2: the UTF-16 hazard chain. Every width>1 character sits UPSTREAM of
# "(Hebrew shalom)", the phrase TestUtf16Hazards edits — that ordering is the
# probe. The first "café" is decomposed (e + ́ combining acute), the
# second precomposed (é); they must remain different code-point sequences.
TAB2_TEXT = (
    "Unicode Hazards\n"
    "\n"
    "Emoji: \U0001f389\n"
    "ZWJ emoji sequence: \U0001f468‍\U0001f469‍\U0001f467 "
    "(family: man+ZWJ+woman+ZWJ+girl)\n"
    "Combining marks (decomposed): café (e + U+0301 combining acute accent)\n"
    "Combining marks (precomposed): café (U+00E9 precomposed)\n"
    "RTL phrase: שָׁלוֹם (Hebrew shalom)\n"
    "Arabic: مَرْحَبًا (Arabic marhaba)"
)


def _batch(docs: Any, doc_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
    return (
        docs.documents()
        .batchUpdate(documentId=doc_id, body={"requests": requests})
        .execute(num_retries=3)
    )


def main() -> None:
    creds = get_credentials()
    docs = build_docs_service(creds)
    drive = build_drive_service(creds)

    doc = (
        docs.documents()
        .create(body={"title": "verified-gdocs-mcp canonical fixture"})
        .execute(num_retries=3)
    )
    doc_id = doc["documentId"]

    # Tab 1: hazard prose, then promote the first paragraph to HEADING_1.
    _batch(
        docs,
        doc_id,
        [
            {"insertText": {"location": {"index": 1, "tabId": "t.0"}, "text": TAB1_TEXT}},
            {
                "updateParagraphStyle": {
                    "range": {**HEADING_RANGE, "tabId": "t.0"},
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "fields": "namedStyleType",
                }
            },
        ],
    )

    # Tab 2 (top-level) and the nested child of t.0.
    reply = _batch(
        docs, doc_id, [{"addDocumentTab": {"tabProperties": {"title": "Unicode Hazards"}}}]
    )
    tab2_id = reply["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
    _batch(
        docs,
        doc_id,
        [{"insertText": {"location": {"index": 1, "tabId": tab2_id}, "text": TAB2_TEXT}}],
    )
    reply = _batch(
        docs,
        doc_id,
        [{"addDocumentTab": {"tabProperties": {"title": "Nested Tab", "parentTabId": "t.0"}}}],
    )
    nested_id = reply["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

    # Two open comment threads; the first carries a reply.
    thread = (
        drive.comments()
        .create(
            fileId=doc_id,
            body={
                "content": "Please confirm the punctuation here.",
                "quotedFileContent": {"value": "“Hello, world!”"},
            },
            fields="id",
        )
        .execute(num_retries=3)
    )
    drive.replies().create(
        fileId=doc_id,
        commentId=thread["id"],
        body={"content": "Confirmed — looks correct."},
        fields="id",
    ).execute(num_retries=3)
    drive.comments().create(
        fileId=doc_id,
        body={
            "content": "Flagging this section for review.",
            "quotedFileContent": {"value": "Duplicate sentence test:"},
        },
        fields="id",
    ).execute(num_retries=3)

    print(f"fixture doc: https://docs.google.com/document/d/{doc_id}/edit")
    print()
    print("Update tests/live/conftest.py:")
    print(f'  DEFAULT_DOC_ID = "{doc_id}"')
    print(f'  CANONICAL_TAB2_ID = "{tab2_id}"')
    print(f'  CANONICAL_NESTED_TAB_ID = "{nested_id}"')
    print("Update tests/live/test_comments.py:")
    print(f'  SEEDED_THREAD_WITH_REPLY = "{thread["id"]}"')
    print()
    print("Manual step (Docs UI, Suggesting mode): make one suggested insertion")
    print("and one suggested deletion, then update SEEDED_SUGGESTION_IDS with the")
    print("ids reported by list_open_items.")


if __name__ == "__main__":
    main()
