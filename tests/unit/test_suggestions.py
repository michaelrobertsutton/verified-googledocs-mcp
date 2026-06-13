"""Unit tests for suggestions.py — extract_suggestions over synthetic doc JSON.

No network calls, no credentials.  All fixtures are synthetic dicts that
match the Docs API shape with suggestionsViewMode=SUGGESTIONS_INLINE applied.
"""

from __future__ import annotations

import pytest

from verified_googledocs_mcp.suggestions import extract_suggestions
from tests.unit.fixtures.suggestions.docs_suggestions import (
    doc_mixed_suggestion_and_normal,
    doc_with_deletion,
    doc_with_insertion,
    doc_with_multirun_insertion,
    doc_with_no_suggestions,
    doc_with_para_style_suggestion,
    doc_with_replacement,
    doc_with_style_suggestion,
    doc_with_table_cell_suggestion,
    tabless_doc_with_suggestion,
)


class TestInsertionExtraction:
    def test_returns_one_entry(self) -> None:
        doc = doc_with_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 1

    def test_insertion_kind(self) -> None:
        doc = doc_with_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["kind"] == "insertion"

    def test_insertion_id(self) -> None:
        doc = doc_with_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["suggestion_id"] == "ins-001"

    def test_insertion_text(self) -> None:
        doc = doc_with_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["text"] == "new text"

    def test_insertion_anchor_contains_surrounding_text(self) -> None:
        doc = doc_with_insertion()
        results = extract_suggestions(doc, "tab-a")
        context = results[0]["anchor_context"]
        assert "Before" in context
        assert "after" in context

    def test_insertion_tab_id_recorded(self) -> None:
        doc = doc_with_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["tab_id"] == "tab-a"


class TestDeletionExtraction:
    def test_returns_one_entry(self) -> None:
        doc = doc_with_deletion()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 1

    def test_deletion_kind(self) -> None:
        doc = doc_with_deletion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["kind"] == "deletion"

    def test_deletion_id(self) -> None:
        doc = doc_with_deletion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["suggestion_id"] == "del-001"

    def test_deletion_text(self) -> None:
        doc = doc_with_deletion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["text"] == "old text"

    def test_deletion_anchor_contains_surrounding_text(self) -> None:
        doc = doc_with_deletion()
        results = extract_suggestions(doc, "tab-a")
        context = results[0]["anchor_context"]
        assert "Keep" in context
        assert "here" in context


class TestReplacementExtraction:
    """A replacement is one deletion + one insertion sharing the same id."""

    def test_returns_two_entries(self) -> None:
        doc = doc_with_replacement()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 2

    def test_same_suggestion_id(self) -> None:
        doc = doc_with_replacement()
        results = extract_suggestions(doc, "tab-a")
        ids = {r["suggestion_id"] for r in results}
        assert ids == {"rep-001"}

    def test_both_kinds_present(self) -> None:
        doc = doc_with_replacement()
        results = extract_suggestions(doc, "tab-a")
        kinds = {r["kind"] for r in results}
        assert kinds == {"insertion", "deletion"}

    def test_deletion_text(self) -> None:
        doc = doc_with_replacement()
        results = extract_suggestions(doc, "tab-a")
        deletion = next(r for r in results if r["kind"] == "deletion")
        assert deletion["text"] == "wrong"

    def test_insertion_text(self) -> None:
        doc = doc_with_replacement()
        results = extract_suggestions(doc, "tab-a")
        insertion = next(r for r in results if r["kind"] == "insertion")
        assert insertion["text"] == "right"


class TestStyleSuggestionExtraction:
    def test_text_style_returns_one_entry(self) -> None:
        doc = doc_with_style_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 1

    def test_text_style_kind(self) -> None:
        doc = doc_with_style_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["kind"] == "style"

    def test_text_style_id(self) -> None:
        doc = doc_with_style_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["suggestion_id"] == "sty-001"

    def test_para_style_returns_one_entry(self) -> None:
        doc = doc_with_para_style_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 1

    def test_para_style_kind(self) -> None:
        doc = doc_with_para_style_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["kind"] == "style"

    def test_para_style_id(self) -> None:
        doc = doc_with_para_style_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["suggestion_id"] == "psty-001"


class TestTabWithNoSuggestions:
    def test_tab_a_returns_empty(self) -> None:
        doc = doc_with_no_suggestions()
        results = extract_suggestions(doc, "tab-a")
        assert results == []

    def test_tab_b_returns_empty(self) -> None:
        doc = doc_with_no_suggestions()
        results = extract_suggestions(doc, "tab-b")
        assert results == []

    def test_unknown_tab_raises_value_error(self) -> None:
        doc = doc_with_no_suggestions()
        with pytest.raises(ValueError, match="Tab 'missing-tab' not found"):
            extract_suggestions(doc, "missing-tab")

    def test_unknown_tab_error_lists_available(self) -> None:
        doc = doc_with_no_suggestions()
        with pytest.raises(ValueError) as exc_info:
            extract_suggestions(doc, "missing-tab")
        msg = str(exc_info.value)
        assert "tab-a" in msg
        assert "tab-b" in msg


class TestTablessDoc:
    def test_implicit_tab_returns_results(self) -> None:
        doc = tabless_doc_with_suggestion()
        results = extract_suggestions(doc, "_body")
        assert len(results) == 1

    def test_implicit_tab_insertion_id(self) -> None:
        doc = tabless_doc_with_suggestion()
        results = extract_suggestions(doc, "_body")
        assert results[0]["suggestion_id"] == "ins-legacy"

    def test_implicit_tab_insertion_text(self) -> None:
        doc = tabless_doc_with_suggestion()
        results = extract_suggestions(doc, "_body")
        assert results[0]["text"] == "legacy insert"

    def test_tabless_wrong_tab_id_raises(self) -> None:
        doc = tabless_doc_with_suggestion()
        with pytest.raises(ValueError, match="Tab 'tab-x' not found"):
            extract_suggestions(doc, "tab-x")

    def test_tabless_wrong_tab_error_lists_body(self) -> None:
        doc = tabless_doc_with_suggestion()
        with pytest.raises(ValueError) as exc_info:
            extract_suggestions(doc, "tab-x")
        assert "_body" in str(exc_info.value)


class TestMixedContent:
    def test_only_suggestion_paragraph_contributes(self) -> None:
        doc = doc_mixed_suggestion_and_normal()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 1

    def test_mixed_insertion_id(self) -> None:
        doc = doc_mixed_suggestion_and_normal()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["suggestion_id"] == "mix-001"

    def test_mixed_anchor_context_from_suggestion_paragraph(self) -> None:
        doc = doc_mixed_suggestion_and_normal()
        results = extract_suggestions(doc, "tab-a")
        # The anchor context must come from the paragraph containing the suggestion.
        context = results[0]["anchor_context"]
        assert "extra" in context
        # Normal paragraph text should not bleed into this context.
        assert "Normal paragraph" not in context


class TestTableCellSuggestion:
    def test_table_cell_deletion_found(self) -> None:
        doc = doc_with_table_cell_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 1

    def test_table_cell_deletion_id(self) -> None:
        doc = doc_with_table_cell_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["suggestion_id"] == "tbl-del-001"

    def test_table_cell_deletion_kind(self) -> None:
        doc = doc_with_table_cell_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["kind"] == "deletion"

    def test_table_cell_deletion_text(self) -> None:
        doc = doc_with_table_cell_suggestion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["text"] == "cell text"


class TestMultiRunSuggestion:
    def test_multirun_returns_one_entry(self) -> None:
        doc = doc_with_multirun_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert len(results) == 1

    def test_multirun_text_concatenated(self) -> None:
        doc = doc_with_multirun_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["text"] == "first part second part"

    def test_multirun_id(self) -> None:
        doc = doc_with_multirun_insertion()
        results = extract_suggestions(doc, "tab-a")
        assert results[0]["suggestion_id"] == "mri-001"
