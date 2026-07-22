"""§ Issue #56 suggestion guard — live acceptance.

Root cause: every mutation pipeline pre-reads with
suggestionsViewMode=PREVIEW_WITHOUT_SUGGESTIONS (needed so a pending
suggestion can't defeat the locator's match-count guard from issue #28), but
batchUpdate always mutates the document's real index space, which is
SUGGESTIONS_INLINE. When the target tab has a pending suggested insertion or
deletion, those two index spaces diverge, so every index a pipeline computes
is wrong — the write lands at the wrong offset and corrupts the document.
assert_no_pending_suggestions (suggestions.py) is the pre-write guard: refuse
instead of corrupting.

Seeding a real live suggestion requires either the Docs UI's "Suggesting"
mode or a viewer/commenter-role account — the REST API cannot create one for
an editor-role account (this is why the canonical fixture's own suggestions
were seeded manually via the UI, see tests/live/test_comments.py). Absent
that manual step, this suite proves the guard's live wiring the same way
tests/live/test_comments.py::TestResolveComment::test_comment_still_open_failure_path
proves a hard-to-arrange API state: everything is real (a genuine disposable
scratch_doc, the real FastMCP client, the real pipeline, real revision
capture) except the ONE API response that can't be arranged any other way —
here, the SUGGESTIONS_INLINE read — which is patched to report a pending
insertion in the target tab. This proves the guard fires end-to-end against
real documents and that no batchUpdate reaches the API when it does; the
control test at the bottom proves the same tools still succeed, unpatched,
on the same scratch doc.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest

from verified_googledocs_mcp.docs import _find_tab_body, fetch_document

pytestmark = pytest.mark.live


def _inline_doc_with_suggestion(preview_doc: dict[str, Any], tab_id: str) -> dict[str, Any]:
    """Deep-copy a real PREVIEW_WITHOUT_SUGGESTIONS doc and mark its first
    text run in *tab_id* as a pending suggested insertion, keeping the same
    revisionId (so the guard's same-revision check passes)."""
    doc = copy.deepcopy(preview_doc)
    body = _find_tab_body(doc, tab_id)
    assert body is not None, f"tab {tab_id!r} not found in live scratch doc"
    for elem in body.get("content", []):
        para = elem.get("paragraph")
        if not para:
            continue
        for inline in para.get("elements", []):
            text_run = inline.get("textRun")
            if text_run and text_run.get("content", "").strip():
                text_run["suggestedInsertionIds"] = ["suggest.live-guard-test"]
                return doc
    raise AssertionError("no non-empty text run found to mark as a suggestion")


class TestSuggestionGuardBlocksLiveWrites:
    async def test_insert_table_blocked_and_revision_unchanged(
        self, client, scratch_doc, live_services
    ) -> None:
        docs_service, _ = live_services
        s = scratch_doc
        pre_doc = fetch_document(docs_service, s.doc_id)
        revision_before = pre_doc.get("revisionId", "")
        inline_with_suggestion = _inline_doc_with_suggestion(pre_doc, s.primary_tab)

        with patch(
            "verified_googledocs_mcp.suggestions.fetch_document_inline",
            lambda _service, _doc_id: inline_with_suggestion,
        ):
            for dry_run in (True, False):
                r = await client.call_tool(
                    "insert_table",
                    {
                        "doc_id": s.doc_id,
                        "tab_id": s.primary_tab,
                        "anchor": "Duplicate sentence test:",
                        "rows": [["Role", "Allocation"], ["Solutions Architect", "50%"]],
                        "dry_run": dry_run,
                    },
                    raise_on_error=False,
                )
                assert r.is_error, f"dry_run={dry_run} should have been refused"
                assert "SUGGESTIONS_PRESENT" in str(r.content)

        # Independent live re-read: the document was never mutated.
        post_doc = fetch_document(docs_service, s.doc_id)
        assert post_doc.get("revisionId", "") == revision_before

    async def test_replace_table_row_blocked_and_revision_unchanged(
        self, client, scratch_doc, live_services
    ) -> None:
        docs_service, _ = live_services
        s = scratch_doc
        # Seed a real table first (unpatched) so the row-target is valid.
        await client.call_tool(
            "insert_table",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "anchor": "Duplicate sentence test:",
                "rows": [["Role", "Allocation"], ["Solutions Architect", "50%"]],
            },
        )
        pre_doc = fetch_document(docs_service, s.doc_id)
        revision_before = pre_doc.get("revisionId", "")
        inline_with_suggestion = _inline_doc_with_suggestion(pre_doc, s.primary_tab)

        with patch(
            "verified_googledocs_mcp.suggestions.fetch_document_inline",
            lambda _service, _doc_id: inline_with_suggestion,
        ):
            for dry_run in (True, False):
                r = await client.call_tool(
                    "replace_table_row",
                    {
                        "doc_id": s.doc_id,
                        "tab_id": s.primary_tab,
                        "table_index": 0,
                        "row_index": 1,
                        "cells": ["Principal Architect", "60%"],
                        "dry_run": dry_run,
                    },
                    raise_on_error=False,
                )
                assert r.is_error, f"dry_run={dry_run} should have been refused"
                assert "SUGGESTIONS_PRESENT" in str(r.content)

        post_doc = fetch_document(docs_service, s.doc_id)
        assert post_doc.get("revisionId", "") == revision_before

    async def test_replace_tab_markdown_blocked_and_revision_unchanged(
        self, client, scratch_doc, live_services
    ) -> None:
        docs_service, _ = live_services
        s = scratch_doc
        pre_doc = fetch_document(docs_service, s.doc_id)
        revision_before = pre_doc.get("revisionId", "")
        inline_with_suggestion = _inline_doc_with_suggestion(pre_doc, s.primary_tab)

        with patch(
            "verified_googledocs_mcp.suggestions.fetch_document_inline",
            lambda _service, _doc_id: inline_with_suggestion,
        ):
            for dry_run in (True, False):
                r = await client.call_tool(
                    "replace_tab_markdown",
                    {
                        "doc_id": s.doc_id,
                        "tab_id": s.primary_tab,
                        "markdown": "# Replaced\n\nNew content.\n",
                        "allow_structural_loss": True,
                        "dry_run": dry_run,
                    },
                    raise_on_error=False,
                )
                assert r.is_error, f"dry_run={dry_run} should have been refused"
                assert "SUGGESTIONS_PRESENT" in str(r.content)

        post_doc = fetch_document(docs_service, s.doc_id)
        assert post_doc.get("revisionId", "") == revision_before

    async def test_replace_text_blocked_and_revision_unchanged(
        self, client, scratch_doc, live_services
    ) -> None:
        docs_service, _ = live_services
        s = scratch_doc
        pre_doc = fetch_document(docs_service, s.doc_id)
        revision_before = pre_doc.get("revisionId", "")
        inline_with_suggestion = _inline_doc_with_suggestion(pre_doc, s.primary_tab)

        with patch(
            "verified_googledocs_mcp.suggestions.fetch_document_inline",
            lambda _service, _doc_id: inline_with_suggestion,
        ):
            r = await client.call_tool(
                "replace_text",
                {
                    "doc_id": s.doc_id,
                    "tab_id": s.primary_tab,
                    "find": "Duplicate sentence test:",
                    "replace": "Replaced heading:",
                },
                raise_on_error=False,
            )
            assert r.is_error
            assert "SUGGESTIONS_PRESENT" in str(r.content)

        post_doc = fetch_document(docs_service, s.doc_id)
        assert post_doc.get("revisionId", "") == revision_before


class TestControlNoSuggestionsStillSucceeds:
    async def test_insert_table_dry_run_succeeds_on_the_unpatched_scratch_doc(
        self, client, scratch_doc
    ) -> None:
        """The guard is a no-op in the common case — same scratch doc, no patch."""
        s = scratch_doc
        r = await client.call_tool(
            "insert_table",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "anchor": "Duplicate sentence test:",
                "rows": [["Role", "Allocation"], ["Solutions Architect", "50%"]],
                "dry_run": True,
            },
        )
        assert not r.is_error
        assert r.data["applied"] is False  # dry_run
        assert "insert_at" in r.data
