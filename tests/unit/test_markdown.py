"""Unit tests for the Docs JSON → markdown converter.

All tests use synthetic fixture dicts; no network calls, no credentials.
"""

from __future__ import annotations


from googledocs_mcp.markdown import to_markdown
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
