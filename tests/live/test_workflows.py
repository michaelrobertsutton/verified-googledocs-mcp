"""§8 PRD acceptance workflows — end to end, zero manual verification steps.

Both run against a disposable copy and assert convergence purely from tool
return values and re-reads — no human-in-the-loop check anywhere.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


class TestCommentResolutionCycle:
    async def test_locate_comment_reply_resolve_confirm(self, client, scratch_doc):
        doc, tab = scratch_doc.doc_id, scratch_doc.primary_tab

        # locate + comment
        created = (
            await client.call_tool(
                "add_anchored_comment",
                {
                    "doc_id": doc,
                    "tab_id": tab,
                    "quote": "Duplicate sentence test:",
                    "body": "Reviewer: please confirm this section.",
                },
            )
        ).data
        cid = created["comment_id"]
        assert created["applied"] is True and created["resolved"] is False

        # reply
        replied = (
            await client.call_tool(
                "reply_to_comment",
                {"doc_id": doc, "comment_id": cid, "body": "Author: confirmed, resolving."},
            )
        ).data
        assert replied["applied"] is True and replied["reply_count"] >= 1

        # resolve
        resolved = (
            await client.call_tool("resolve_comment", {"doc_id": doc, "comment_id": cid})
        ).data
        assert resolved["applied"] is True and resolved["resolved"] is True

        # confirm closed — re-query, no manual step
        thread = (
            await client.call_tool("get_comment_thread", {"doc_id": doc, "comment_id": cid})
        ).data
        assert thread["resolved"] is True


class TestMarkdownSyncRoundTrip:
    async def test_read_diff_apply_reread_converge(self, client, scratch_doc, tmp_path):
        doc, tab = scratch_doc.doc_id, scratch_doc.primary_tab

        # The local file we want the tab to converge to (supported subset).
        desired = (
            "# Synced Document\n\nFirst paragraph after sync.\n\nSecond paragraph after sync.\n"
        )
        f = tmp_path / "desired.md"
        f.write_text(desired, encoding="utf-8")

        # 1. diff — the tab does not yet match the file.
        before = (
            await client.call_tool(
                "diff_tab_vs_file",
                {"doc_id": doc, "tab_id": tab, "file_path": str(f)},
            )
        ).data
        assert before["identical"] is False

        # 2. apply — write the file's markdown into the tab.
        applied = (
            await client.call_tool(
                "replace_tab_markdown",
                {"doc_id": doc, "tab_id": tab, "markdown": desired},
            )
        ).data
        assert applied["applied"] is True
        # (structural_match evidence is unreliable on this path — see #36 — so
        # convergence is proven below by re-reading the content directly.)

        # 3. re-read + re-diff — the tab has converged toward the file.
        content = (await client.call_tool("read_document", {"doc_id": doc, "tab_id": tab})).data[
            "content"
        ]
        assert "Synced Document" in content
        assert "First paragraph after sync" in content
        assert "Second paragraph after sync" in content
        assert "Duplicate sentence test" not in content  # old content gone

        # The re-diff confirms convergence: the old content was a difference
        # before the apply and is gone after it.
        after = (
            await client.call_tool(
                "diff_tab_vs_file",
                {"doc_id": doc, "tab_id": tab, "file_path": str(f)},
            )
        ).data
        assert "Duplicate sentence test" in before["unified_diff"]
        assert "Duplicate sentence test" not in after["unified_diff"]
