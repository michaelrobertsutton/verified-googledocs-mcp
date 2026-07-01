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

    async def test_range_replace_structural_match_evidence(self, client, scratch_doc):
        # #43 fixed: the evidence slice is now bounded to the inserted extent.
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
# Tables — regression coverage for the table-write fix
# ---------------------------------------------------------------------------


class TestTableWrites:
    """Tables previously 400'd on write via both replace_tab_markdown and
    replace_range_markdown ("insertion index must be inside the bounds of an
    existing paragraph"), while dry_run reported false success. See
    markdown_writer.py's _visit_table docstring for the fixed geometry."""

    async def test_table_first_element_after_heading(self, client, scratch_doc):
        markdown = (
            "# Report\n\n"
            "| Metric | Detail |\n"
            "|---|---|\n"
            "| one sentence here. | another sentence. |\n"
        )
        r = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab, "markdown": markdown},
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True

        content = (await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)).replace(
            "\\", ""
        )
        assert "one sentence here." in content
        assert "another sentence." in content

    async def test_table_mid_doc_with_content_before_and_after(self, client, scratch_doc):
        """Exercises the post-table cursor fix: content after the table must
        land after it, not at a stale (pre-fix) index."""
        markdown = (
            "# Report\n\n"
            "Lead-in paragraph before the table.\n\n"
            "| A | B |\n"
            "|---|---|\n"
            "| mid1 | mid2 |\n\n"
            "Trailing paragraph after the table.\n"
        )
        r = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab, "markdown": markdown},
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True

        content = (await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)).replace(
            "\\", ""
        )
        assert "Lead-in paragraph before the table." in content
        assert "mid1" in content and "mid2" in content
        assert "Trailing paragraph after the table." in content
        assert content.index("mid2") < content.index("Trailing paragraph")

    async def test_table_as_last_element(self, client, scratch_doc):
        markdown = "# Report\n\nLead-in paragraph.\n\n| A | B |\n|---|---|\n| last1 | last2 |\n"
        r = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab, "markdown": markdown},
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True
        content = (await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)).replace(
            "\\", ""
        )
        assert "last1" in content and "last2" in content

    async def test_multi_row_table(self, client, scratch_doc):
        markdown = "| A | B |\n|---|---|\n| r0c0 | r0c1 |\n| r1c0 | r1c1 |\n| r2c0 | r2c1 |\n"
        r = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab, "markdown": markdown},
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True
        content = (await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)).replace(
            "\\", ""
        )
        for expected in ("r0c0", "r0c1", "r1c0", "r1c1", "r2c0", "r2c1"):
            assert expected in content

    async def test_multi_sentence_cells(self, client, scratch_doc):
        """The original bug repro shape: a one-row, two-column table with
        multi-sentence cells, as the first body element after a heading."""
        markdown = (
            "| Metric | Detail |\n"
            "|---|---|\n"
            "| This is a first multi-sentence cell. It has two sentences. "
            "| This is a second cell with its own multi-sentence content. Right here. |\n"
        )
        r = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab, "markdown": markdown},
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True
        content = (await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)).replace(
            "\\", ""
        )
        assert "This is a first multi-sentence cell. It has two sentences." in content
        assert "This is a second cell with its own multi-sentence content. Right here." in content

    async def test_styled_and_linked_table_cells(self, client, scratch_doc):
        """Exercises the intra-cell style-span re-anchoring fix: bold/link
        spans must land on the right text after reverse-order cell
        insertion shifts an already-inserted cell's text forward."""
        markdown = (
            "| Left | Right |\n|---|---|\n| **bold cell** | [a link](https://example.com) |\n"
        )
        r = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab, "markdown": markdown},
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True
        content = await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)
        assert "**bold cell**" in content
        assert "[a link](https://example.com)" in content

    async def test_table_round_trips_via_replace_range_markdown(self, client, scratch_doc):
        s = scratch_doc
        m = (
            await client.call_tool(
                "find_sections",
                {"doc_id": s.doc_id, "tab_id": s.primary_tab, "heading": HEADING},
            )
        ).data["matches"][0]
        markdown = "# Renamed Heading\n\n| A | B |\n|---|---|\n| range1 | range2 |\n"
        r = await client.call_tool(
            "replace_range_markdown",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "start_index": m["start_index"],
                "end_index": m["end_index"],
                "computed_at_revision": m["computed_at_revision"],
                "markdown": markdown,
            },
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True
        content = (await _read(client, s.doc_id, s.primary_tab)).replace("\\", "")
        assert "range1" in content and "range2" in content

    async def test_dry_run_and_real_write_agree_for_table_markdown(self, client, scratch_doc):
        """AC #2: dry_run and the real write must return the same verdict for
        table markdown — no false positive."""
        markdown = "| A | B |\n|---|---|\n| parity1 | parity2 |\n"
        s = scratch_doc

        dry = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "markdown": markdown, "dry_run": True},
        )
        assert dry.data["dry_run"] is True
        assert dry.data["applied"] is False  # dry_run never applies

        real = await client.call_tool(
            "replace_tab_markdown",
            {"doc_id": s.doc_id, "tab_id": s.primary_tab, "markdown": markdown},
        )
        # dry_run reported no error (the write would succeed); the real
        # write must actually succeed too — no false positive.
        assert real.data["applied"] is True
        assert real.data["structural_match"] is True


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


class TestTableGeometryProbe:
    """Pins the Docs API's empty-table index layout as a living contract test.

    ``_visit_table`` in markdown_writer.py predicts every cell's insertion
    index from a formula (see its docstring). This test inserts *raw*, empty
    ``insertTable`` requests via the live API and reads back the real
    per-cell start indices, independent of our compiler. If Google ever
    changes this layout, this test fails before any compiler regression
    test does, pointing straight at the geometry rather than a symptom.

    Confirmed layout for an ``insertTable`` at ``location.index = I``:
        T = I + 1                      (leading newline)
        stride = 2 * cols + 1
        row_start(r)          = T + 1 + r * stride
        cell_paragraph(r, c)  = T + 3 + r * stride + 2 * c
        table_end             = T + rows * stride + 2
    """

    @staticmethod
    def _table_element(docs, doc_id, tab_id):  # type: ignore[no-untyped-def]
        from verified_googledocs_mcp.docs import fetch_document

        doc = fetch_document(docs, doc_id)
        for t in doc.get("tabs", []):
            if t.get("tabProperties", {}).get("tabId") == tab_id:
                body = t["documentTab"]["body"]
                return next(el for el in body["content"] if "table" in el)
        raise AssertionError(f"tab {tab_id!r} not found")

    async def test_empty_table_geometry_matches_formula(self, live_services, scratch_doc):
        docs, _ = live_services
        s = scratch_doc
        rows, cols = 2, 3
        insert_at = 1

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertTable": {
                            "rows": rows,
                            "columns": cols,
                            "location": {"index": insert_at, "tabId": s.primary_tab},
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        table_elem = self._table_element(docs, s.doc_id, s.primary_tab)
        table_start = table_elem["startIndex"]
        assert table_start == insert_at + 1

        stride = 2 * cols + 1
        for r_idx, row in enumerate(table_elem["table"]["tableRows"]):
            assert row["startIndex"] == table_start + 1 + r_idx * stride
            for c_idx, cell in enumerate(row["tableCells"]):
                para_start = cell["content"][0]["startIndex"]
                assert para_start == table_start + 3 + r_idx * stride + 2 * c_idx

        assert table_elem["endIndex"] == table_start + rows * stride + 2


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
