"""Unit tests for the Docs JSON → markdown converter.

All tests use synthetic fixture dicts; no network calls, no credentials.
"""

from __future__ import annotations


from verified_googledocs_mcp.markdown import to_markdown
from tests.unit.fixtures.docs_api import (
    lossy_elements_doc,
    multi_tab_doc,
)


def _tab1_body(doc: dict) -> dict:
    """Extract body dict for tab-1 from multi_tab_doc."""
    for tab in doc["tabs"]:
        if tab["tabProperties"]["tabId"] == "tab-1":
            return tab["documentTab"]["body"]
    raise KeyError("tab-1 not found")


def _tab_body(doc: dict, tab_id: str) -> dict:
    for tab in doc["tabs"]:
        if tab["tabProperties"]["tabId"] == tab_id:
            return tab["documentTab"]["body"]
    raise KeyError(tab_id)


class TestHeadings:
    def test_h1_renders(self) -> None:
        doc = multi_tab_doc()
        md, lossy = to_markdown(_tab1_body(doc))
        assert "# Introduction" in md

    def test_h2_renders(self) -> None:
        doc = multi_tab_doc()
        md, _ = to_markdown(_tab1_body(doc))
        assert "## Methods" in md

    def test_heading_prefix_level_matches(self) -> None:
        body = {
            "content": [
                {
                    "startIndex": 1,
                    "endIndex": 10,
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_3"},
                        "elements": [{"textRun": {"content": "SubSub\n"}}],
                    },
                }
            ]
        }
        md, _ = to_markdown(body)
        assert md.strip().startswith("### SubSub")


class TestInlineStyles:
    def test_bold_rendered(self) -> None:
        doc = multi_tab_doc()
        md, _ = to_markdown(_tab1_body(doc))
        assert "**bold**" in md

    def test_italic_rendered(self) -> None:
        doc = multi_tab_doc()
        md, _ = to_markdown(_tab1_body(doc))
        assert "*italic*" in md

    def test_link_rendered(self) -> None:
        doc = multi_tab_doc()
        for tab in doc["tabs"]:
            if tab["tabProperties"]["tabId"] == "tab-2":
                tab2_body = tab["documentTab"]["body"]
        md, _ = to_markdown(tab2_body)
        assert "[Google](https://google.com)" in md


class TestLists:
    def test_bullet_list_items(self) -> None:
        doc = multi_tab_doc()
        md, _ = to_markdown(_tab1_body(doc))
        assert "- First item" in md
        assert "- Second item" in md


def _list_item_para(text: str, list_id: str = "list-1", level: int = 0) -> dict:
    """A list-item paragraph, matching the live API's real shape (issue #65):
    ``bullet`` carries only ``listId``/``nestingLevel``, never a glyph type —
    ``nestingLevel`` is simply omitted at level 0, confirmed live."""
    bullet: dict = {"listId": list_id}
    if level:
        bullet["nestingLevel"] = level
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "bullet": bullet,
            "elements": [{"textRun": {"content": text + "\n"}}],
        }
    }


def _ordered_lists_map(list_id: str = "list-1", depth: int = 3) -> dict:
    """A lists map for the NUMBERED_DECIMAL_ALPHA_ROMAN preset, matching the
    live cycle confirmed by TestBulletNestingProbe: DECIMAL/ALPHA/ROMAN."""
    cycle = ("DECIMAL", "ALPHA", "ROMAN")
    return {
        list_id: {
            "listProperties": {"nestingLevels": [{"glyphType": cycle[i % 3]} for i in range(depth)]}
        }
    }


def _unordered_lists_map(list_id: str = "list-1", depth: int = 3) -> dict:
    """A lists map for the BULLET_DISC_CIRCLE_SQUARE preset — glyphSymbol,
    never glyphType, matching the live cycle confirmed by TestBulletNestingProbe."""
    cycle = ("●", "○", "■")  # ● ○ ■
    return {
        list_id: {
            "listProperties": {
                "nestingLevels": [{"glyphSymbol": cycle[i % 3]} for i in range(depth)]
            }
        }
    }


class TestOrderedLists:
    """Reader-side fix for issue #65: ordered vs unordered and nesting are
    read from the tab's ``lists`` map, never from ``bullet`` itself."""

    def test_ordered_list_items_render_with_number_markers(self) -> None:
        body = {"content": [_list_item_para("First"), _list_item_para("Second")]}
        md, _ = to_markdown(body, lists=_ordered_lists_map())
        assert md == "1. First\n2. Second\n"

    def test_missing_lists_map_falls_back_to_unordered(self) -> None:
        # No lists= passed at all — the pre-#65 behaviour, preserved as the
        # safe default rather than guessing.
        body = {"content": [_list_item_para("Item")]}
        md, _ = to_markdown(body)
        assert md == "- Item\n"

    def test_level_beyond_defined_nesting_levels_falls_back_to_unordered(self) -> None:
        body = {"content": [_list_item_para("Deep", level=5)]}
        lists = _ordered_lists_map(depth=2)  # only levels 0-1 defined
        md, _ = to_markdown(body, lists=lists)
        assert md.strip().endswith("- Deep")

    def test_different_list_id_at_same_level_starts_fresh_counter(self) -> None:
        body = {
            "content": [
                _list_item_para("A1", list_id="list-1", level=0),
                _list_item_para("A2", list_id="list-1", level=0),
                _list_item_para("B1", list_id="list-2", level=0),
            ]
        }
        lists = {**_ordered_lists_map("list-1"), **_ordered_lists_map("list-2")}
        md, _ = to_markdown(body, lists=lists)
        assert md == "1. A1\n2. A2\n1. B1\n"

    def test_ordered_nested_indent_matches_parent_marker_width(self) -> None:
        # After "1. " (3 chars), the child must indent by exactly 3 spaces —
        # not a fixed "  " * nesting — or it would not re-parse as nested.
        body = {
            "content": [
                _list_item_para("Parent", level=0),
                _list_item_para("Child", level=1),
            ]
        }
        md, _ = to_markdown(body, lists=_ordered_lists_map())
        assert md == "1. Parent\n   1. Child\n"

    def test_unordered_nested_indent_matches_parent_marker_width(self) -> None:
        # After "- " (2 chars), the child indents by exactly 2 spaces.
        body = {
            "content": [
                _list_item_para("Parent", level=0),
                _list_item_para("Child", level=1),
            ]
        }
        md, _ = to_markdown(body, lists=_unordered_lists_map())
        assert md == "- Parent\n  - Child\n"

    def test_ordered_list_counter_resumes_at_same_level_after_a_deeper_item(self) -> None:
        body = {
            "content": [
                _list_item_para("First", level=0),
                _list_item_para("Sub A", level=1),
                _list_item_para("Second", level=0),
            ]
        }
        md, _ = to_markdown(body, lists=_ordered_lists_map())
        assert md == "1. First\n   1. Sub A\n2. Second\n"

    def test_ordered_list_child_counter_restarts_under_a_new_parent(self) -> None:
        # Each parent's children are their own group and restart at 1 — this
        # is what makes diff_tab_vs_file agree with a hand-written source
        # file, which would restart nested numbering the same way.
        body = {
            "content": [
                _list_item_para("First", level=0),
                _list_item_para("Sub A", level=1),
                _list_item_para("Sub B", level=1),
                _list_item_para("Second", level=0),
                _list_item_para("Sub C", level=1),
            ]
        }
        md, _ = to_markdown(body, lists=_ordered_lists_map())
        assert md == ("1. First\n   1. Sub A\n   2. Sub B\n2. Second\n   1. Sub C\n")


def _normal_para(text: str) -> dict:
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{"textRun": {"content": text + "\n"}}],
        }
    }


def _bullet_para(text: str) -> dict:
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "bullet": {"listId": "list-1"},
            "elements": [{"textRun": {"content": text + "\n"}}],
        }
    }


class TestBlockSeparation:
    """Regression for #36: block-level elements need a blank line between them."""

    def test_consecutive_paragraphs_separated_by_blank_line(self) -> None:
        # A single newline between two paragraphs is a soft break that re-parses
        # as one paragraph; they must be separated by a blank line instead.
        body = {"content": [_normal_para("First paragraph"), _normal_para("Second paragraph")]}
        md, _ = to_markdown(body)
        assert md == "First paragraph\n\nSecond paragraph\n"

    def test_paragraph_after_heading_separated_by_blank_line(self) -> None:
        body = {
            "content": [
                {
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "elements": [{"textRun": {"content": "Title\n"}}],
                    }
                },
                _normal_para("Body text"),
            ]
        }
        md, _ = to_markdown(body)
        assert md == "# Title\n\nBody text\n"

    def test_consecutive_list_items_stay_tight(self) -> None:
        # Items within one list must NOT gain blank lines between them.
        body = {"content": [_bullet_para("First item"), _bullet_para("Second item")]}
        md, _ = to_markdown(body)
        assert md == "- First item\n- Second item\n"


class TestTables:
    def test_table_renders_pipe_format(self) -> None:
        doc = multi_tab_doc()
        md, _ = to_markdown(_tab1_body(doc))
        assert "| Header A |" in md
        assert "| Cell 1 |" in md

    def test_table_has_separator_row(self) -> None:
        doc = multi_tab_doc()
        md, _ = to_markdown(_tab1_body(doc))
        assert "| --- |" in md or "|---|" in md or "| ---" in md


class TestLossyElements:
    def test_inline_image_placeholder(self) -> None:
        doc = lossy_elements_doc()
        body = _tab_body(doc, "tab-main")
        md, lossy = to_markdown(body)
        assert "[image:obj-abc123]" in md
        kinds = [e.kind for e in lossy]
        assert "image" in kinds

    def test_person_chip_placeholder(self) -> None:
        doc = lossy_elements_doc()
        body = _tab_body(doc, "tab-main")
        md, lossy = to_markdown(body)
        assert "[chip:person:alice@example.com]" in md
        assert any(e.kind == "chip" for e in lossy)

    def test_footnote_placeholder(self) -> None:
        doc = lossy_elements_doc()
        body = _tab_body(doc, "tab-main")
        md, lossy = to_markdown(body)
        assert "[footnote:fn-1]" in md
        assert any(e.kind == "footnote" for e in lossy)

    def test_lossy_elements_list_populated(self) -> None:
        doc = lossy_elements_doc()
        body = _tab_body(doc, "tab-main")
        _, lossy = to_markdown(body)
        # image + chip + footnote
        assert len(lossy) >= 3

    def test_no_lossy_elements_for_plain_text(self) -> None:
        body = {
            "content": [
                {
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        "elements": [{"textRun": {"content": "Hello world.\n"}}],
                    }
                }
            ]
        }
        _, lossy = to_markdown(body)
        assert lossy == []


class TestEmptyDoc:
    def test_empty_body(self) -> None:
        md, lossy = to_markdown({"content": []})
        assert md == ""
        assert lossy == []
