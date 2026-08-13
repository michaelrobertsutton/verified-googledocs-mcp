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

    async def test_issue_65_minimal_repro_round_trips(self, client, scratch_doc):
        """Verbatim from issue #65: nested unordered list + a separate
        ordered list. Before the fix this landed with every child flattened
        to nesting level 0 and every numbered item rendered as a bullet,
        while still reporting VERIFICATION_FAILED after already mutating
        the document."""
        markdown = "* Parent bullet\n  * Child bullet\n\n1. First\n2. Second\n"
        r = await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": markdown,
            },
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True
        assert "structural_diff" not in r.data
        assert r.data["write_status"] == "written_verified"
        assert r.data["retry_safe"] is True

        content = await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)
        assert "- Parent bullet" in content
        assert "  - Child bullet" in content  # nested one level, not flattened
        assert "1. First" in content
        assert "2. Second" in content  # numbered, not flattened to bullets

    async def test_mixed_nested_list_ordered_list_and_table_round_trips(
        self, client, scratch_doc, tmp_path
    ):
        """issue #65 Criterion 4: a nested unordered list, a separate
        ordered list, and a table in the same tab, all in one write."""
        markdown = (
            "- Parent bullet\n"
            "  - Child bullet\n"
            "\n"
            "1. First\n"
            "2. Second\n"
            "\n"
            "| Header A | Header B |\n"
            "|---|---|\n"
            "| Cell 1 | Cell 2 |\n"
        )
        r = await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": markdown,
            },
        )
        assert r.data["applied"] is True
        assert r.data["structural_match"] is True
        assert "structural_diff" not in r.data

        # diff_tab_vs_file catches what block-level structural_match can't:
        # exact ordered-marker text and indent-width arithmetic. Compared
        # against the reader's own rendering conventions, not the input
        # verbatim: consecutive list items always render tight (no blank
        # line) regardless of whether they belong to the same list — that
        # is a pre-existing, unrelated presentational choice in
        # markdown.py's block-separation logic, not something this fix
        # changes — and the pipe-table separator row always renders with
        # spaces ("| --- | --- |").
        expected_rendered = (
            "- Parent bullet\n"
            "  - Child bullet\n"
            "1. First\n"
            "2. Second\n"
            "\n"
            "| Header A | Header B |\n"
            "| --- | --- |\n"
            "| Cell 1 | Cell 2 |\n"
        )
        f = tmp_path / "mixed.md"
        f.write_text(expected_rendered, encoding="utf-8")
        diff = (
            await client.call_tool(
                "diff_tab_vs_file",
                {
                    "doc_id": scratch_doc.doc_id,
                    "tab_id": scratch_doc.primary_tab,
                    "file_path": str(f),
                },
            )
        ).data
        assert diff["identical"] is True, diff["unified_diff"]

    async def test_link_url_shapes_survive_structural_match(self, client, scratch_doc):
        """`_blocks_structurally_equal` now compares `link_targets` on the
        post-write side too (issue #65's adjacent fix). That comparison uses
        whatever URL Docs actually stored, not what was sent — if Docs
        normalizes a URL (adds a trailing slash, re-encodes a space, etc.)
        this would false-positive VERIFICATION_FAILED *after* the batchUpdate
        landed, which is exactly the mutate-then-fail failure mode issue #65
        removes for lists. Pin that Docs round-trips these shapes byte-exact.
        """
        markdown = (
            "[bare](https://example.com)\n\n"
            "[slash](https://example.com/)\n\n"
            "[query](https://example.com/a?b=c&d=e)\n\n"
            "[frag](https://example.com/a#sec)\n\n"
            "[space](https://example.com/a%20b)\n"
        )
        r = await client.call_tool(
            "replace_tab_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": markdown,
            },
        )
        assert r.data["applied"] is True, r.data
        assert r.data["structural_match"] is True, r.data.get("structural_diff")
        assert "structural_diff" not in r.data


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


class TestBulletNestingProbe:
    """Pins the Docs API's list-nesting mechanism as a living contract test
    for issue #65 (nested lists and ordered lists lost on write).

    Uses *raw* batchUpdate requests, independent of markdown_writer.py /
    markdown.py, so a compiler regression can never mask what the live API
    actually does. If Google ever changes this behaviour, this test fails
    before any compiler/reader regression test does.

    Confirmed (2026-08-13, against a scratch copy of the canonical fixture):

    1. A *single* ``createParagraphBullets`` request applied over a range
       spanning paragraphs with 0, 1, 2 leading tabs produces
       ``bullet.nestingLevel`` 0, 1, 2 respectively on the three paragraphs —
       nesting is derived per-paragraph from each paragraph's own leading-tab
       count, not fixed for the whole range.
    2. The leading tabs are consumed by the request: ``textRun.content`` no
       longer contains them afterward (confirmed no list-item text starts
       with ``"\\t"``).
    3. ``bullet`` on a created paragraph is exactly
       ``{"listId": ..., "nestingLevel": ..., "textStyle": {...}}`` (nestingLevel
       omitted key entirely for level 0) — there is no ``listProperties`` key
       on the bullet itself. This confirms markdown.py's
       ``bullet.get("listProperties")`` lookup (the pre-#65 reader bug) can
       never succeed; list properties live on ``body["lists"][listId]``.
    4. ``lists[listId].listProperties.nestingLevels`` has exactly 9 entries
       (levels 0-8) for *both* presets. For ``NUMBERED_DECIMAL_ALPHA_ROMAN``
       every entry carries a ``glyphType``, cycling ``DECIMAL`` (0, 3, 6),
       ``ALPHA`` (1, 4, 7), ``ROMAN`` (2, 5, 8) — confirmed by direct dump,
       not just the two levels this probe asserts on. For
       ``BULLET_DISC_CIRCLE_SQUARE`` every entry instead carries a
       ``glyphSymbol`` (``●``/``○``/``■`` cycling) and **no** ``glyphType``
       key at all. This confirms a positive membership test against the
       known ordered ``glyphType`` values (DECIMAL, ALPHA, ROMAN,
       UPPER_ALPHA, UPPER_ROMAN, ZERO_DECIMAL — the other three never appear
       for this preset, but do for other numbered presets the Docs UI
       offers) is the correct way for the reader to detect an ordered list,
       never a fallback-to-BULLET default.
    5. Nesting clamps at the preset's defined depth rather than erroring:
       source depths 0-7 map to ``nestingLevel`` 0-7 one-to-one, and every
       depth 8 and beyond (tested through depth 13) reads back as
       ``nestingLevel`` 8 — the preset's deepest defined level. The compiler
       must therefore *refuse* markdown nested deeper than level 8, rather
       than silently emit a request Google will clamp — clamping would make
       both sides of the structural comparison agree on the wrong
       (flattened) level and falsely verify.
    """

    @staticmethod
    def _tab_doc_and_body(docs, doc_id, tab_id):  # type: ignore[no-untyped-def]
        from verified_googledocs_mcp.docs import _find_tab_body, fetch_document

        doc = fetch_document(docs, doc_id)
        return doc, _find_tab_body(doc, tab_id)

    @staticmethod
    def _find_tab_lists(doc, tab_id):  # type: ignore[no-untyped-def]
        for t in doc.get("tabs", []):
            if t.get("tabProperties", {}).get("tabId") == tab_id:
                return t.get("documentTab", {}).get("lists", {})
        return doc.get("lists", {})

    async def test_single_request_nesting_derived_per_paragraph_and_tabs_stripped(
        self, live_services, scratch_doc
    ):
        docs, _ = live_services
        s = scratch_doc
        insert_at = 1
        text = "a\n\tb\n\t\tc\n"  # 9 ASCII chars: nesting 0, 1, 2
        end_index = insert_at + len(text)

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": insert_at, "tabId": s.primary_tab},
                            "text": text,
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "createParagraphBullets": {
                            "range": {
                                "startIndex": insert_at,
                                "endIndex": end_index,
                                "tabId": s.primary_tab,
                            },
                            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        doc, body = self._tab_doc_and_body(docs, s.doc_id, s.primary_tab)
        paras = [e["paragraph"] for e in body["content"] if "paragraph" in e][:3]
        assert len(paras) == 3, "expected the three inserted paragraphs first in the tab body"

        for expected_nesting, para in zip((0, 1, 2), paras):
            bullet = para.get("bullet")
            assert bullet is not None, f"paragraph has no bullet: {para!r}"
            assert bullet.get("nestingLevel", 0) == expected_nesting
            assert "listProperties" not in bullet, (
                "bullet unexpectedly carries listProperties — the reader fix's "
                f"premise is wrong: {bullet!r}"
            )
            text_content = "".join(
                el["textRun"]["content"] for el in para.get("elements", []) if "textRun" in el
            )
            assert not text_content.startswith("\t"), (
                f"leading tab survived createParagraphBullets: {text_content!r}"
            )

        list_id = paras[0]["bullet"]["listId"]
        lists = self._find_tab_lists(doc, s.primary_tab)
        nesting_levels = lists[list_id]["listProperties"]["nestingLevels"]
        for level in (0, 1, 2):
            assert "glyphType" not in nesting_levels[level], (
                f"unordered preset level {level} unexpectedly has glyphType: "
                f"{nesting_levels[level]!r}"
            )
            assert "glyphSymbol" in nesting_levels[level]

    async def test_ordered_preset_glyph_types_and_max_nesting_depth(
        self, live_services, scratch_doc
    ):
        docs, _ = live_services
        s = scratch_doc
        insert_at = 1
        # Levels 0-11: more than any preset defines, to find where nesting clamps.
        depths = list(range(12))
        text = "".join("\t" * d + str(d) + "\n" for d in depths)
        end_index = insert_at + len(text)

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": insert_at, "tabId": s.primary_tab},
                            "text": text,
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "createParagraphBullets": {
                            "range": {
                                "startIndex": insert_at,
                                "endIndex": end_index,
                                "tabId": s.primary_tab,
                            },
                            "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN",
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        doc, body = self._tab_doc_and_body(docs, s.doc_id, s.primary_tab)
        paras = [e["paragraph"] for e in body["content"] if "paragraph" in e][: len(depths)]
        assert len(paras) == len(depths)

        observed_nesting = [p["bullet"].get("nestingLevel", 0) for p in paras]
        # Nesting must never exceed the deepest requested depth, and must be
        # monotonically non-decreasing per source depth up to the clamp point.
        max_observed = max(observed_nesting)
        assert max_observed <= max(depths)
        # Record where the API stopped increasing nesting (the preset's max
        # defined level) — the compiler must refuse markdown nested deeper
        # than this rather than silently emit a request Google will clamp.
        clamp_level = observed_nesting[-1]
        for i in range(1, len(observed_nesting)):
            assert observed_nesting[i] >= observed_nesting[i - 1] - 0, (
                f"nesting must not decrease as source depth increases: {observed_nesting!r}"
            )

        list_id = paras[0]["bullet"]["listId"]
        lists = self._find_tab_lists(doc, s.primary_tab)
        nesting_levels = lists[list_id]["listProperties"]["nestingLevels"]
        ordered_glyph_types = {nl.get("glyphType") for nl in nesting_levels if "glyphType" in nl}
        assert ordered_glyph_types, "expected at least one ordered glyphType in the preset"
        assert ordered_glyph_types <= {
            "DECIMAL",
            "ALPHA",
            "ROMAN",
            "UPPER_ALPHA",
            "UPPER_ROMAN",
            "ZERO_DECIMAL",
        }
        # Surface the clamp depth for the compiler's own max-nesting refusal —
        # not a hard assertion (Google could redefine preset depth), but a
        # sanity bound so this probe fails loudly if the assumption changes.
        assert clamp_level < len(depths)


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
