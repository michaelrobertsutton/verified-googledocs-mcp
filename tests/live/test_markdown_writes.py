"""§3 Markdown writes — replace_range_markdown, replace_tab_markdown,
append_markdown, insert_image, plus UNSUPPORTED_MARKDOWN, STALE_RANGE,
IMAGE_SOURCE_UNSUPPORTED.

Each tool's *write* is exercised against a fresh disposable copy and confirmed
by re-reading the document. The structural-verification *evidence* of three of
these tools currently false-negatives or garbles against the live API; those
specific assertions are quarantined as xfail against their follow-up issues
(#36, #37, #38) so the suite stays honestly green and the assertions flip to
passing once the fixes land.

replace_range_markdown / STALE_RANGE need a heading (canonical fixture has none
— gap #31), so they use the heading-seeded scratch copy.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

# A small, stable, publicly fetchable image the Docs API can pull.
IMG_URL = "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png"


def _err(result) -> str:  # type: ignore[no-untyped-def]
    return str(result.content)


async def _read(client, doc_id, tab_id) -> str:  # type: ignore[no-untyped-def]
    r = await client.call_tool(
        "read_document", {"doc_id": doc_id, "tab_id": tab_id, "format": "markdown"}
    )
    return r.data["content"]


# The canonical fixture's HEADING_1 (seeded for #31); inherited by every copy.
HEADING = "Text Hazards"


async def _range_replace(client, s):  # type: ignore[no-untyped-def]
    m = (
        await client.call_tool(
            "find_sections",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "heading": HEADING},
        )
    ).data["matches"][0]
    return await client.call_tool(
        "replace_range_markdown",
        {
            "doc_id": s.doc_id,
            "tab_id": s.primary_tab,
            "start_index": m["start_index"],
            "end_index": m["end_index"],
            "computed_at_revision": m["computed_at_revision"],
            "markdown": "# Renamed Heading\n",
        },
        raise_on_error=False,
    )


# ---------------------------------------------------------------------------
# replace_range_markdown + STALE_RANGE
# ---------------------------------------------------------------------------


class TestReplaceRangeMarkdown:
    async def test_replaces_a_find_sections_range(self, client, scratch_doc):
        r = await _range_replace(client, scratch_doc)
        assert r.data["applied"] is True
        assert r.data["revision_before"] != r.data["revision_after"]
        # The new heading content landed in the document.
        assert "Renamed Heading" in await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)

    @pytest.mark.xfail(
        reason="#43 — replace_range_markdown's evidence re-exports too wide a slice "
        "(approx_end + 100), sweeping in adjacent paragraphs, so structural_match "
        "false-negatives for a range replace not at the tab end. The write itself is correct.",
        strict=False,
    )
    async def test_range_replace_structural_match_evidence(self, client, scratch_doc):
        r = await _range_replace(client, scratch_doc)
        assert r.data["structural_match"] is True

    async def test_stale_range_after_doc_moves_on(self, client, scratch_doc, live_services):
        s = scratch_doc
        m = (
            await client.call_tool(
                "find_sections",
                {"doc_id": s.doc_id, "tab_id": s.primary_tab, "heading": HEADING},
            )
        ).data["matches"][0]

        # Move the document on, invalidating the range's revision stamp.
        docs, _ = live_services
        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1, "tabId": s.primary_tab},
                            "text": "z",
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        r = await client.call_tool(
            "replace_range_markdown",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "start_index": m["start_index"],
                "end_index": m["end_index"],
                "computed_at_revision": m["computed_at_revision"],  # now stale
                "markdown": "# Whatever\n",
            },
            raise_on_error=False,
        )
        assert r.is_error and "STALE_RANGE" in _err(r)


# ---------------------------------------------------------------------------
# replace_tab_markdown — whole-tab replace
# ---------------------------------------------------------------------------


class TestReplaceTabMarkdown:
    MARKDOWN = (
        "# Replaced Tab\n\nFirst synced paragraph.\n\n- one\n- two\n\nSecond synced paragraph.\n"
    )

    async def test_whole_tab_replace_lands_new_content(self, client, scratch_doc):
        r = await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": self.MARKDOWN,
            },
        )
        assert r.data["applied"] is True
        assert r.data["revision_before"] != r.data["revision_after"]

        content = await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)
        assert "Replaced Tab" in content
        assert "First synced paragraph" in content
        assert "Second synced paragraph" in content
        # Old hazard content is gone.
        assert "Duplicate sentence test" not in content

    async def test_whole_tab_replace_structural_match_evidence(self, client, scratch_doc):
        r = await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": self.MARKDOWN,
            },
        )
        assert r.data["structural_match"] is True


# ---------------------------------------------------------------------------
# append_markdown — content lands at the tab end
# ---------------------------------------------------------------------------


class TestAppendMarkdown:
    async def test_append_applies_and_adds_content(self, client, scratch_doc):
        r = await client.call_tool(
            "append_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": "## Appended Section\n\nAPPENDED_MARKER paragraph.\n",
            },
        )
        assert r.data["applied"] is True
        after = (await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)).replace("\\", "")
        assert "APPENDED_MARKER" in after
        # Existing content above is preserved (append, not replace).
        assert "Curly quotes" in after

    async def test_append_does_not_fuse_with_trailing_paragraph(self, client, scratch_doc):
        await client.call_tool(
            "append_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": "## Appended Section\n\nAPPENDED_MARKER paragraph.\n",
            },
        )
        after = (await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)).replace("\\", "")
        # The pre-existing trailing sentence must NOT have been fused into a heading.
        assert "## The quick brown fox" not in after
        assert "[rev-probe]Appended" not in after


# ---------------------------------------------------------------------------
# insert_image — URL succeeds at quote + heading; local path rejected
# ---------------------------------------------------------------------------


class TestInsertImage:
    async def test_url_at_quoted_anchor_applies(self, client, scratch_doc):
        r = await client.call_tool(
            "insert_image",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "anchor": "Duplicate sentence test:",
                "source": IMG_URL,
            },
        )
        assert r.data["applied"] is True
        assert r.data["revision_before"] != r.data["revision_after"]

    async def test_url_at_heading_applies(self, client, scratch_doc):
        r = await client.call_tool(
            "insert_image",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "anchor": HEADING,
                "source": IMG_URL,
            },
        )
        assert r.data["applied"] is True

    async def test_inline_object_confirmed_evidence(self, client, scratch_doc):
        r = await client.call_tool(
            "insert_image",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "anchor": "Duplicate sentence test:",
                "source": IMG_URL,
            },
        )
        assert r.data["inline_object_confirmed"] is True

    async def test_local_path_is_image_source_unsupported(self, client, canonical_doc_id):
        r = await client.call_tool(
            "insert_image",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.0",
                "anchor": "Curly quotes",
                "source": "/tmp/local_image.png",
            },
            raise_on_error=False,
        )
        assert r.is_error and "IMAGE_SOURCE_UNSUPPORTED" in _err(r)


# ---------------------------------------------------------------------------
# UNSUPPORTED_MARKDOWN — out-of-subset construct named
# ---------------------------------------------------------------------------


class TestUnsupportedMarkdown:
    async def test_blockquote_is_unsupported_and_named(self, client, scratch_doc):
        r = await client.call_tool(
            "append_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": "> a blockquote is outside the supported subset\n",
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "UNSUPPORTED_MARKDOWN" in content
        assert "blockquote" in content  # names the offending construct
