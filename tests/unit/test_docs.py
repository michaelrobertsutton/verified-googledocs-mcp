"""Unit tests for docs.py — pure transforms over Docs API response dicts.

No network calls, no credentials.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from verified_googledocs_mcp.docs import (
    IMPLICIT_TAB_ID,
    fetch_document,
    find_sections_in,
    list_tabs_from,
    read_tab,
)
from tests.unit.fixtures.docs_api import (
    multi_tab_doc,
    nested_tabs_doc,
    tabless_doc,
)


class TestListTabs:
    def test_multi_tab_returns_all_tabs(self) -> None:
        doc = multi_tab_doc()
        tabs = list_tabs_from(doc)
        assert len(tabs) == 2
        ids = {t.tab_id for t in tabs}
        assert ids == {"tab-1", "tab-2"}

    def test_tab_titles_returned(self) -> None:
        doc = multi_tab_doc()
        tabs = list_tabs_from(doc)
        titles = {t.title for t in tabs}
        assert "Tab One" in titles
        assert "Tab Two" in titles

    def test_tabless_doc_returns_implicit_tab(self) -> None:
        doc = tabless_doc()
        tabs = list_tabs_from(doc)
        assert len(tabs) == 1
        assert tabs[0].tab_id == IMPLICIT_TAB_ID

    def test_tabless_doc_implicit_tab_title(self) -> None:
        doc = tabless_doc()
        tabs = list_tabs_from(doc)
        assert tabs[0].title == "Body"

    def test_nested_tabs_top_level_count(self) -> None:
        doc = nested_tabs_doc()
        tabs = list_tabs_from(doc)
        # Only top-level tabs returned at the top of the list; child tabs are
        # in child_tabs attribute.
        assert len(tabs) == 1
        assert tabs[0].tab_id == "parent-tab"
        assert len(tabs[0].child_tabs) == 1
        assert tabs[0].child_tabs[0].tab_id == "child-tab"

    def test_as_dict_includes_child_tabs(self) -> None:
        doc = nested_tabs_doc()
        tabs = list_tabs_from(doc)
        d = tabs[0].as_dict()
        assert "child_tabs" in d
        assert d["child_tabs"][0]["tab_id"] == "child-tab"

    def test_as_dict_no_child_tabs_key_when_empty(self) -> None:
        doc = multi_tab_doc()
        tabs = list_tabs_from(doc)
        d = tabs[0].as_dict()
        assert "child_tabs" not in d


class TestReadTab:
    def test_read_tab_markdown_format(self) -> None:
        doc = multi_tab_doc()
        result = read_tab(doc, "doc-multi-tab", "tab-1", format="markdown")
        assert result.format == "markdown"
        assert isinstance(result.content, str)
        assert "Introduction" in result.content

    def test_read_tab_structured_format(self) -> None:
        doc = multi_tab_doc()
        result = read_tab(doc, "doc-multi-tab", "tab-1", format="structured")
        assert result.format == "structured"
        assert isinstance(result.content, dict)
        assert "paragraphs" in result.content

    def test_read_tab_revision_id_stamped(self) -> None:
        doc = multi_tab_doc()
        result = read_tab(doc, "doc-multi-tab", "tab-1")
        assert result.revision_id == "rev-001"

    def test_read_tab_unknown_tab_raises(self) -> None:
        doc = multi_tab_doc()
        with pytest.raises(ValueError, match="tab-999"):
            read_tab(doc, "doc-multi-tab", "tab-999")

    def test_read_tab_unknown_tab_lists_available(self) -> None:
        doc = multi_tab_doc()
        with pytest.raises(ValueError, match="tab-1"):
            read_tab(doc, "doc-multi-tab", "nonexistent-tab")

    def test_read_tabless_doc_implicit_tab(self) -> None:
        doc = tabless_doc()
        result = read_tab(doc, "doc-tabless", IMPLICIT_TAB_ID)
        assert "Legacy Document" in result.content

    def test_read_tabless_doc_wrong_tab_raises(self) -> None:
        doc = tabless_doc()
        with pytest.raises(ValueError):
            read_tab(doc, "doc-tabless", "tab-1")

    def test_read_tab_lossy_elements_in_result(self) -> None:
        from tests.unit.fixtures.docs_api import lossy_elements_doc

        doc = lossy_elements_doc()
        result = read_tab(doc, "doc-lossy", "tab-main", format="markdown")
        assert len(result.lossy_elements) >= 3

    def test_nested_child_tab_readable(self) -> None:
        doc = nested_tabs_doc()
        result = read_tab(doc, "doc-nested", "child-tab")
        assert "Child Tab" in result.content

    def test_read_tab_default_format_is_markdown(self) -> None:
        doc = multi_tab_doc()
        result = read_tab(doc, "doc-multi-tab", "tab-1")
        assert result.format == "markdown"


class TestFindSections:
    def test_find_by_exact_heading(self) -> None:
        doc = multi_tab_doc()
        matches = find_sections_in(doc, "Introduction", "tab-1")
        assert len(matches) == 1
        assert matches[0].matched_text == "Introduction"

    def test_find_case_insensitive(self) -> None:
        doc = multi_tab_doc()
        matches = find_sections_in(doc, "introduction", "tab-1")
        assert len(matches) == 1

    def test_find_substring_match(self) -> None:
        doc = multi_tab_doc()
        matches = find_sections_in(doc, "intro", "tab-1")
        assert len(matches) == 1

    def test_find_no_match_returns_empty(self) -> None:
        doc = multi_tab_doc()
        matches = find_sections_in(doc, "Nonexistent Section", "tab-1")
        assert matches == []

    def test_find_returns_revision_stamp(self) -> None:
        doc = multi_tab_doc()
        matches = find_sections_in(doc, "Introduction", "tab-1")
        assert matches[0].computed_at_revision == "rev-001"

    def test_find_returns_section_span_to_next_heading(self) -> None:
        # #49: the range must span the whole section (heading through body),
        # ending at the start of the next heading — not just the heading line.
        # "Introduction" starts at 1; the next heading ("Methods") starts at 64.
        doc = multi_tab_doc()
        matches = find_sections_in(doc, "Introduction", "tab-1")
        m = matches[0]
        assert m.start_index == 1
        assert m.end_index == 64  # start of "Methods", not the heading's own end (16)

    def test_find_last_heading_spans_to_tab_end(self) -> None:
        # #49: a section with no following heading runs to the end of the tab
        # body. "Methods" is the last heading in tab-1; the tab body ends at 130.
        doc = multi_tab_doc()
        matches = find_sections_in(doc, "Methods", "tab-1")
        m = matches[0]
        assert m.start_index == 64
        assert m.end_index == 130

    def test_find_multiple_headings_matched(self) -> None:
        # Both "Introduction" and "Methods" headings should be found with
        # a broad query.
        doc = multi_tab_doc()
        matches_intro = find_sections_in(doc, "Introduction", "tab-1")
        matches_methods = find_sections_in(doc, "Methods", "tab-1")
        assert len(matches_intro) == 1
        assert len(matches_methods) == 1

    def test_find_scoped_to_tab(self) -> None:
        # "Results" exists only in tab-2, not tab-1.
        doc = multi_tab_doc()
        in_tab1 = find_sections_in(doc, "Results", "tab-1")
        in_tab2 = find_sections_in(doc, "Results", "tab-2")
        assert in_tab1 == []
        assert len(in_tab2) == 1

    def test_find_unknown_tab_raises(self) -> None:
        doc = multi_tab_doc()
        with pytest.raises(ValueError):
            find_sections_in(doc, "Intro", "nonexistent-tab")

    def test_find_in_tabless_doc(self) -> None:
        doc = tabless_doc()
        matches = find_sections_in(doc, "Legacy", IMPLICIT_TAB_ID)
        assert len(matches) == 1
        assert "Legacy Document" in matches[0].matched_text

    def test_find_in_nested_child_tab(self) -> None:
        doc = nested_tabs_doc()
        matches = find_sections_in(doc, "Child Tab", "child-tab")
        assert len(matches) == 1


class TestFetchDocument:
    """fetch_document is the I/O edge; verify the Docs API call shape.

    The locator and every index computation run over this response, so the
    document must be fetched WITHOUT inline suggestions. If suggestionsViewMode
    is unset it resolves to DEFAULT_FOR_CURRENT_ACCESS (SUGGESTIONS_INLINE for an
    editor), which lets a pending suggestion collapse a duplicate sentence into a
    single match and defeats replace_text's match-count guard (issue #28).
    """

    def _service_returning(self, doc: dict) -> tuple[MagicMock, MagicMock]:
        service = MagicMock()
        get_call = service.documents.return_value.get
        get_call.return_value.execute.return_value = doc
        return service, get_call

    def test_pins_preview_without_suggestions(self) -> None:
        service, get_call = self._service_returning({"documentId": "doc-1"})
        fetch_document(service, "doc-1")
        get_call.assert_called_once_with(
            documentId="doc-1",
            includeTabsContent=True,
            suggestionsViewMode="PREVIEW_WITHOUT_SUGGESTIONS",
        )

    def test_returns_execute_result(self) -> None:
        service, _ = self._service_returning({"documentId": "doc-1", "revisionId": "r1"})
        assert fetch_document(service, "doc-1") == {"documentId": "doc-1", "revisionId": "r1"}
