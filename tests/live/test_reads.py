"""§1 Reads and structure — read_document, list_tabs, find_sections.

All read-only against the canonical fixture, which now carries a HEADING_1
("Text Hazards") and a nested tab "Nested Tab" (seeded for #31).
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.live


def _norm(s: str) -> str:
    """Strip markdown backslash-escapes, NBSP, and soft hyphens; collapse ws.

    Lets structured-run text (raw document text) be compared against the
    markdown rendering of the same span, which escapes punctuation and keeps
    NBSP/soft-hyphen code points.
    """
    s = re.sub(r"\\(.)", r"\1", s)  # markdown escape: \- -> -, \! -> !
    s = s.replace(" ", " ").replace("­", "")  # NBSP -> space, soft hyphen -> gone
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# read_document
# ---------------------------------------------------------------------------


class TestReadDocument:
    async def test_markdown_mode_returns_hazard_text(self, client, canonical_doc_id):
        result = await client.call_tool(
            "read_document",
            {"doc_id": canonical_doc_id, "tab_id": "t.0", "format": "markdown"},
        )
        data = result.data
        assert data["format"] == "markdown"
        assert data["revision_id"]
        # Curly quotes preserved verbatim (not normalised away on read).
        assert "“Hello, world" in data["content"]
        assert "Duplicate sentence test" in data["content"]

    async def test_structured_mode_runs_line_up_with_visible_text(self, client, canonical_doc_id):
        """Structured spans should line up with the markdown text on the same tab."""
        md = (
            await client.call_tool(
                "read_document",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "format": "markdown"},
            )
        ).data["content"]

        structured = (
            await client.call_tool(
                "read_document",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "format": "structured"},
            )
        ).data

        paragraphs = structured["content"]["paragraphs"]
        assert paragraphs, "structured read returned no paragraphs"

        md_norm = _norm(md)
        last_start = -1
        for para in paragraphs:
            # Paragraph spans advance monotonically through the document.
            assert para["start"] >= last_start
            last_start = para["start"]
            for run in para["runs"]:
                assert run["end"] >= run["start"]
                visible = _norm(run["text"])
                if visible:
                    # The text of each structured span is present in the visible
                    # markdown — i.e. the spans line up with what the reader sees.
                    assert visible in md_norm, f"run text {visible!r} not in rendered markdown"

    async def test_markdown_and_structured_share_revision(self, client, canonical_doc_id):
        a = (
            await client.call_tool(
                "read_document",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "format": "markdown"},
            )
        ).data
        b = (
            await client.call_tool(
                "read_document",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "format": "structured"},
            )
        ).data
        assert a["revision_id"] == b["revision_id"]


# ---------------------------------------------------------------------------
# list_tabs
# ---------------------------------------------------------------------------


class TestListTabs:
    async def test_ids_and_titles_match_real_document(self, client, canonical_doc_id):
        data = (await client.call_tool("list_tabs", {"doc_id": canonical_doc_id})).data
        by_id = {t["tab_id"]: t for t in data["tabs"]}
        assert "t.0" in by_id
        assert "t.a53r2f94k2pt" in by_id
        assert by_id["t.a53r2f94k2pt"]["title"] == "Unicode Hazards"
        # Tabs carry an index reflecting document order.
        assert by_id["t.0"]["index"] == 0

    async def test_nested_tab_is_reported(self, client, canonical_doc_id):
        # t.0 has a child tab "Nested Tab" (t.22v4eg81pdjk) — seeded for #31.
        data = (await client.call_tool("list_tabs", {"doc_id": canonical_doc_id})).data
        by_id = {t["tab_id"]: t for t in data["tabs"]}
        children = by_id["t.0"].get("child_tabs", [])
        nested = {c["tab_id"]: c for c in children}
        assert "t.22v4eg81pdjk" in nested
        assert nested["t.22v4eg81pdjk"]["title"] == "Nested Tab"


# ---------------------------------------------------------------------------
# find_sections  (against the canonical "Text Hazards" HEADING_1 — #31)
# ---------------------------------------------------------------------------


class TestFindSections:
    async def test_heading_resolved_to_range_stamped_with_revision(self, client, canonical_doc_id):
        # "Text Hazards" is a HEADING_1 in t.0 (seeded for #31).
        result = (
            await client.call_tool(
                "find_sections",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "heading": "Text Hazards"},
            )
        ).data

        matches = result["matches"]
        assert matches, "Text Hazards heading not found in t.0"
        m = matches[0]
        assert "Text Hazards" in m["matched_text"]
        assert (m["start_index"], m["end_index"]) == (1, 14)
        # Stamped with a live revisionId — present and non-empty (it changes on
        # every edit, so we don't assert a fixed value).
        assert m["computed_at_revision"]
