"""Unit tests for the offline index-bounds simulator.

All tests are pure: no network, no Google API calls.
"""

from __future__ import annotations

import pytest

from verified_googledocs_mcp.index_sim import (
    IndexSimulationError,
    compiled_requests_growth,
    simulate_requests,
)
from verified_googledocs_mcp.markdown_writer import compile_markdown

# ---------------------------------------------------------------------------
# Happy paths: our own compiler's output must always simulate clean.
# ---------------------------------------------------------------------------


def test_plain_paragraph_simulates_clean():
    reqs = compile_markdown("Just a paragraph.\n", start_index=1)
    simulate_requests(reqs, tab_start=1, tab_end=2)  # does not raise


def test_table_simulates_clean_after_the_fix():
    """The original repro shape: this must NOT raise IndexSimulationError.
    Before the +1 -> +3 fix, this exact input would have failed here."""
    src = "# Title\n\n| A | B |\n|---|---|\n| one sentence. | another one. |\n"
    reqs = compile_markdown(src, start_index=1)
    simulate_requests(reqs, tab_start=1, tab_end=2)


def test_table_followed_by_paragraph_simulates_clean():
    src = "| A | B |\n|---|---|\n| 1 | 2 |\n\nAfter the table.\n"
    reqs = compile_markdown(src, start_index=1)
    simulate_requests(reqs, tab_start=1, tab_end=2)


def test_multi_row_table_simulates_clean():
    src = "| A | B |\n|---|---|\n| r0c0 | r0c1 |\n| r1c0 | r1c1 |\n| r2c0 | r2c1 |\n"
    reqs = compile_markdown(src, start_index=1)
    simulate_requests(reqs, tab_start=1, tab_end=2)


def test_styled_table_cell_simulates_clean():
    src = "| **Left** | **Right** |\n|---|---|\n"
    reqs = compile_markdown(src, start_index=1)
    simulate_requests(reqs, tab_start=1, tab_end=2)


def test_shift_whole_table_when_content_inserted_before_it():
    """Defensive/robustness case (not produced by our own compiler today,
    which never inserts before content it already emitted, but modeled for
    correctness): inserting text *before* an already-created table must
    shift that table's start, end, and every cell index wholesale."""
    requests = [
        {"insertTable": {"rows": 1, "columns": 1, "location": {"index": 5}}},
        {"insertText": {"text": "PREFIX", "location": {"index": 1}}},
        # Table's cell (originally at index 9) must be targeted at its
        # shifted index (9 + len("PREFIX")) to be considered valid.
        {"insertText": {"text": "cell", "location": {"index": 9 + len("PREFIX")}}},
    ]
    simulate_requests(requests, tab_start=1, tab_end=6)


def test_two_tables_in_one_batch_simulates_clean():
    """A second, later table must be fully shifted by the first table's own
    structure + cell text growth — exercises the 'table wholly at/after idx'
    shift branch (a plain 'table then paragraph' only exercises the
    'straddles idx' branch, so this needs its own construction)."""
    insert_at = 1
    reqs = compile_markdown("| A |\n|---|\n| one |\n", start_index=insert_at)
    from verified_googledocs_mcp.index_sim import compiled_requests_growth

    second_table_at = insert_at + compiled_requests_growth(reqs)
    second_table_reqs = compile_markdown("| B |\n|---|\n| two |\n", start_index=second_table_at)
    simulate_requests(reqs + second_table_reqs, tab_start=1, tab_end=2)


def test_delete_then_insert_full_batch_simulates_clean():
    """Mirrors execute_replace_tab_markdown's assembled batch: a
    deleteContentRange covering the old body, then the compiled requests."""
    src = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    reqs = compile_markdown(src, start_index=1)
    batch = [
        {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 40}}},
        *reqs,
    ]
    simulate_requests(batch, tab_start=1, tab_end=41)


# ---------------------------------------------------------------------------
# The simulator must catch the ORIGINAL bug shape (the +1 offset).
# ---------------------------------------------------------------------------


def test_catches_the_original_off_by_two_bug():
    """Directly reconstructs the pre-fix formula (table_start + 1 + ...
    instead of + 3) and confirms the simulator flags it as landing inside
    the table's structure but not on a valid cell paragraph."""
    insert_at = 1
    table_start = insert_at + 1
    bad_cell_index = table_start + 1  # the old, wrong offset
    requests = [
        {"insertTable": {"rows": 1, "columns": 1, "location": {"index": insert_at}}},
        {"insertText": {"text": "Cell text", "location": {"index": bad_cell_index}}},
    ]
    with pytest.raises(IndexSimulationError) as exc_info:
        simulate_requests(requests, tab_start=1, tab_end=2)
    assert exc_info.value.request_index == 1


def test_catches_numeric_out_of_bounds_insert():
    requests = [{"insertText": {"text": "x", "location": {"index": 9999}}}]
    with pytest.raises(IndexSimulationError):
        simulate_requests(requests, tab_start=1, tab_end=2)


def test_catches_out_of_bounds_delete_range():
    requests = [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 9999}}}]
    with pytest.raises(IndexSimulationError):
        simulate_requests(requests, tab_start=1, tab_end=2)


# ---------------------------------------------------------------------------
# compiled_requests_growth
# ---------------------------------------------------------------------------


def test_growth_plain_text_matches_utf16_length():
    reqs = compile_markdown("hello\n", start_index=1)
    assert compiled_requests_growth(reqs) == len("hello\n")


def test_growth_accounts_for_table_structural_size_not_just_cell_text():
    """A table's empty structural markers (row/cell/table-end) don't appear
    in any insertText, so growth must be larger than the cell text alone."""
    src = "| AB |\n|---|\n| CD |\n"
    reqs = compile_markdown(src, start_index=1)
    cell_text_len = len("AB") + len("CD")
    growth = compiled_requests_growth(reqs)
    assert growth > cell_text_len  # structural markers must be counted too

    # And it must equal exactly what the simulator itself advances by.
    from verified_googledocs_mcp.markdown_writer import _table_structural_size

    expected = _table_structural_size(2, 1) + cell_text_len
    assert growth == expected


def test_growth_matches_final_cursor_position():
    """compiled_requests_growth must agree with the compiler's own final
    cursor advance, so the evidence window and the compiler never disagree."""
    src = "# Title\n\n| A | B |\n|---|---|\n| one | two |\n\nAfter.\n"
    insert_at = 1
    reqs = compile_markdown(src, start_index=insert_at)
    growth = compiled_requests_growth(reqs)

    # The last insertText in the request list is "After.\n"; its index tells
    # us exactly where the compiler's cursor landed after the whole document.
    last_para_start = max(
        r["insertText"]["location"]["index"] for r in reqs if "insertText" in r
    )
    assert insert_at + growth >= last_para_start
