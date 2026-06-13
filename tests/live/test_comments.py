"""§4 Comments and suggestions.

Read-only checks (list_open_items, get_comment_thread) run against the canonical
fixture, which carries the seeded comment threads and suggestions. Mutating
checks (add / reply / resolve) run against a disposable copy and create their
own throwaway comments, since files.copy does not carry comments forward.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.live

# Seeded on the canonical fixture (issue #1).
SEEDED_THREAD_WITH_REPLY = "AAAB9jbwLrg"  # curly-quotes comment, has one reply
SEEDED_SUGGESTION_IDS = {"suggest.ibkkr0jufgxx", "suggest.9ta3rfsy7fng"}


def _err(result) -> str:  # type: ignore[no-untyped-def]
    return str(result.content)


# ---------------------------------------------------------------------------
# list_open_items — comments AND suggestions in one call
# ---------------------------------------------------------------------------


class TestListOpenItems:
    async def test_returns_comments_and_suggestions_together(self, client, canonical_doc_id):
        data = (await client.call_tool("list_open_items", {"doc_id": canonical_doc_id})).data

        # Open comments present (the two seeded threads).
        assert len(data["open_comments"]) >= 2
        assert all(c["resolved"] is False for c in data["open_comments"])

        # Suggested edits present in the SAME response — the gap the incumbent
        # required a second access path for.
        suggestion_ids = {s["suggestion_id"] for s in data["pending_suggestions"]}
        assert SEEDED_SUGGESTION_IDS <= suggestion_ids


# ---------------------------------------------------------------------------
# get_comment_thread — full reply chain
# ---------------------------------------------------------------------------


class TestGetCommentThread:
    async def test_full_reply_chain(self, client, canonical_doc_id):
        data = (
            await client.call_tool(
                "get_comment_thread",
                {"doc_id": canonical_doc_id, "comment_id": SEEDED_THREAD_WITH_REPLY},
            )
        ).data
        assert data["comment_id"] == SEEDED_THREAD_WITH_REPLY
        assert data["reply_count"] >= 1
        assert data["replies"]
        assert data["replies"][0]["content"]
        assert data["quoted_text"]  # the anchored quote


# ---------------------------------------------------------------------------
# add_anchored_comment — validated against a quote; QUOTE_NOT_FOUND otherwise
# ---------------------------------------------------------------------------


class TestAddAnchoredComment:
    async def test_creates_quote_validated_comment(self, client, scratch_doc):
        r = await client.call_tool(
            "add_anchored_comment",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "quote": "Duplicate sentence test:",
                "body": "Acceptance: anchored comment created by the live suite.",
            },
        )
        d = r.data
        assert d["applied"] is True
        assert d["comment_id"]
        assert d["resolved"] is False
        # The quote is embedded as the comment's quoted content (doc-level
        # rendering per the #1 anchoring spike).
        assert "Duplicate sentence test:" in d["quoted_text"]

    async def test_missing_quote_is_quote_not_found(self, client, canonical_doc_id):
        r = await client.call_tool(
            "add_anchored_comment",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.0",
                "quote": "this exact phrase is definitely not present anywhere zzz",
                "body": "should not be created",
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "QUOTE_NOT_FOUND" in content
        assert "candidates" in content  # nearest candidate anchors offered


# ---------------------------------------------------------------------------
# reply_to_comment — reply appears on re-query
# ---------------------------------------------------------------------------


class TestReplyToComment:
    async def test_reply_appears_on_requery(self, client, scratch_doc):
        created = (
            await client.call_tool(
                "add_anchored_comment",
                {
                    "doc_id": scratch_doc.doc_id,
                    "tab_id": scratch_doc.primary_tab,
                    "quote": "Duplicate sentence test:",
                    "body": "Parent comment.",
                },
            )
        ).data
        cid = created["comment_id"]

        r = await client.call_tool(
            "reply_to_comment",
            {
                "doc_id": scratch_doc.doc_id,
                "comment_id": cid,
                "body": "A reply from the live suite.",
            },
        )
        assert r.data["applied"] is True
        assert r.data["reply_count"] >= 1

        # Independent re-query confirms the reply landed.
        thread = (
            await client.call_tool(
                "get_comment_thread",
                {"doc_id": scratch_doc.doc_id, "comment_id": cid},
            )
        ).data
        assert any("A reply from the live suite." in rep["content"] for rep in thread["replies"])


# ---------------------------------------------------------------------------
# resolve_comment — the incumbent-bug regression + COMMENT_STILL_OPEN
# ---------------------------------------------------------------------------


class TestResolveComment:
    async def test_resolve_actually_closes_the_comment(self, client, scratch_doc):
        """Resolve, re-query, confirm the comment is genuinely closed."""
        created = (
            await client.call_tool(
                "add_anchored_comment",
                {
                    "doc_id": scratch_doc.doc_id,
                    "tab_id": scratch_doc.primary_tab,
                    "quote": "Duplicate sentence test:",
                    "body": "To be resolved.",
                },
            )
        ).data
        cid = created["comment_id"]

        r = await client.call_tool(
            "resolve_comment",
            {"doc_id": scratch_doc.doc_id, "comment_id": cid},
        )
        assert r.data["applied"] is True
        assert r.data["resolved"] is True

        # Independent re-query proves it is actually closed (not a false success).
        thread = (
            await client.call_tool(
                "get_comment_thread",
                {"doc_id": scratch_doc.doc_id, "comment_id": cid},
            )
        ).data
        assert thread["resolved"] is True
        # Closed via a resolve-action reply, not a (silently-ignored) field write.
        assert any(rep["action"] == "resolve" for rep in thread["replies"])

    async def test_comment_still_open_failure_path(self, client, scratch_doc):
        """If the resolve action silently no-ops, the re-query catches it.

        Simulates the incumbent's behaviour (comments.update is ignored) by
        stubbing the resolve call to do nothing; the tool's real re-query then
        observes the comment is still open and refuses to report success.
        """
        created = (
            await client.call_tool(
                "add_anchored_comment",
                {
                    "doc_id": scratch_doc.doc_id,
                    "tab_id": scratch_doc.primary_tab,
                    "quote": "Duplicate sentence test:",
                    "body": "Resolve will be sabotaged.",
                },
            )
        ).data
        cid = created["comment_id"]

        with patch(
            "verified_googledocs_mcp.comments.resolve_comment_api",
            lambda drive_service, doc_id, comment_id: None,  # silent no-op
        ):
            r = await client.call_tool(
                "resolve_comment",
                {"doc_id": scratch_doc.doc_id, "comment_id": cid},
                raise_on_error=False,
            )
        assert r.is_error
        content = _err(r)
        assert "COMMENT_STILL_OPEN" in content
        assert "post_state" in content  # post-state evidence included
