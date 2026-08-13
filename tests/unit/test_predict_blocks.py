"""Unit tests for verify.predict_blocks (issue #65's pre-flight prediction).

predict_blocks reasons only about the REQUESTS compile_markdown emits — never
about the compiler's internal intent — so it can catch exactly the class of
bug that caused issue #65: a compiler whose internal model said one thing
(nesting=1) but whose emitted requests encoded another (nesting=0). Every
positive test here compiles real markdown and confirms predict_blocks agrees
with _parse_markdown_blocks (the input-side parser) block-for-block; the
negative tests confirm a deliberately corrupted request list is caught.

All tests are pure: no network, no Google API calls.
"""

from __future__ import annotations

import copy

import pytest

from verified_googledocs_mcp.markdown_writer import compile_markdown
from verified_googledocs_mcp.verify import (
    _blocks_structurally_equal,
    _parse_markdown_blocks,
    predict_blocks,
)


def _predicted_matches_input(markdown: str, *, start_index: int = 1) -> bool:
    """Compile *markdown*, predict its requests' block structure, and
    confirm it agrees with the input-side parse — the exact comparison
    _predict_or_raise performs before any batchUpdate is sent."""
    requests = compile_markdown(markdown, start_index=start_index)
    predicted = predict_blocks(requests)
    input_blocks = _parse_markdown_blocks(markdown)
    if len(predicted) != len(input_blocks):
        return False
    return all(_blocks_structurally_equal(a, b) for a, b in zip(input_blocks, predicted))


# ---------------------------------------------------------------------------
# Basic block types agree with the input-side parser
# ---------------------------------------------------------------------------


def test_plain_paragraph():
    assert _predicted_matches_input("Just a paragraph.\n")


def test_heading_levels():
    assert _predicted_matches_input("# H1\n\n## H2\n\n### H3\n")


def test_bold_italic_and_link_do_not_affect_block_text():
    assert _predicted_matches_input("**bold** and *italic* and a [link](https://example.com).\n")


def test_multiple_paragraphs():
    assert _predicted_matches_input("First paragraph.\n\nSecond paragraph.\n\nThird.\n")


# ---------------------------------------------------------------------------
# Lists: nesting, ordered-ness, mixed runs — the exact surface issue #65
# regressed on.
# ---------------------------------------------------------------------------


def test_flat_unordered_list():
    assert _predicted_matches_input("- item one\n- item two\n- item three\n")


def test_flat_ordered_list():
    assert _predicted_matches_input("1. first\n2. second\n3. third\n")


def test_the_issues_own_minimal_repro():
    # Verbatim from the issue: a nested unordered list, then a separate
    # ordered list.
    md = "* Parent bullet\n  * Child bullet\n\n1. First\n2. Second\n"
    assert _predicted_matches_input(md)


def test_nested_unordered_list_two_levels():
    md = "- parent\n  - child\n    - grandchild\n"
    assert _predicted_matches_input(md)


def test_nested_ordered_list_two_levels():
    md = "1. parent\n   1. child\n   2. child two\n2. parent two\n"
    assert _predicted_matches_input(md)


def test_mixed_ordered_and_unordered_nested_list():
    md = "- parent\n  1. sub one\n  2. sub two\n"
    assert _predicted_matches_input(md)


def test_two_lists_separated_by_a_paragraph():
    md = "- item one\n\nplain paragraph\n\n- item two\n"
    assert _predicted_matches_input(md)


def test_list_at_max_supported_nesting_depth():
    md = "\n".join(f"{'  ' * i}- level {i}" for i in range(9)) + "\n"
    assert _predicted_matches_input(md)


# ---------------------------------------------------------------------------
# Tables: cell text, styled/linked cells, and interleaving with lists
# ---------------------------------------------------------------------------


def test_simple_table():
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert _predicted_matches_input(md)


def test_multi_row_table():
    md = "| A | B |\n|---|---|\n| r0c0 | r0c1 |\n| r1c0 | r1c1 |\n| r2c0 | r2c1 |\n"
    assert _predicted_matches_input(md)


def test_table_with_styled_and_linked_cells():
    # Regression: table cells are inserted highest-index-first, and their
    # style/link updateTextStyle ranges are expressed in the POST-shift
    # coordinate space — a naive request walk mismatches these.
    md = "| **Left** | [Right](https://right.example) |\n|---|---|\n"
    assert _predicted_matches_input(md)


def test_table_with_every_cell_linked():
    # Stress case: every cell contributes to the cumulative shift.
    md = (
        "| [A](https://a.example) | [B](https://b.example) |\n"
        "|---|---|\n"
        "| [C](https://c.example) | [D](https://d.example) |\n"
    )
    assert _predicted_matches_input(md)


def test_table_between_paragraphs():
    md = "Before the table.\n\n| X | Y |\n|---|---|\n| 1 | 2 |\n\nAfter the table.\n"
    assert _predicted_matches_input(md)


def test_table_first_element():
    md = "| X | Y |\n|---|---|\n| 1 | 2 |\n\nAfter the table.\n"
    assert _predicted_matches_input(md)


def test_table_last_element():
    md = "Before the table.\n\n| X | Y |\n|---|---|\n| 1 | 2 |\n"
    assert _predicted_matches_input(md)


def test_kitchen_sink_heading_list_table_and_links():
    md = (
        "# Heading\n\n"
        "Some [link](https://example.com) text.\n\n"
        "| A | B |\n|---|---|\n| **x** | [y](https://y.example) |\n\n"
        "- item one\n- item two\n"
    )
    assert _predicted_matches_input(md)


# ---------------------------------------------------------------------------
# Negative: predict_blocks must actually catch a corrupted request list —
# this is the regression guard for issue #65's own bug shape.
# ---------------------------------------------------------------------------


def test_catches_nesting_lost_from_a_stripped_tab():
    """Simulates the ORIGINAL bug: the compiler's leading-tab insertText is
    missing from the emitted requests, so nesting information never reached
    the batch even though the input markdown described a nested list."""
    md = "- parent\n  - child\n"
    requests = compile_markdown(md, start_index=1)
    broken = copy.deepcopy(requests)
    for req in broken:
        if req.get("insertText", {}).get("text") == "\t":
            req["insertText"]["text"] = ""

    predicted = predict_blocks(broken)
    input_blocks = _parse_markdown_blocks(md)
    assert len(predicted) == len(input_blocks)
    assert not _blocks_structurally_equal(input_blocks[1], predicted[1])
    assert input_blocks[1]["nesting"] == 1
    assert predicted[1]["nesting"] == 0


def test_catches_ordered_list_written_with_the_wrong_preset():
    """A createParagraphBullets request whose bulletPreset doesn't match
    what the input asked for must be caught."""
    md = "1. first\n2. second\n"
    requests = compile_markdown(md, start_index=1)
    broken = copy.deepcopy(requests)
    for req in broken:
        if "createParagraphBullets" in req:
            req["createParagraphBullets"]["bulletPreset"] = "BULLET_DISC_CIRCLE_SQUARE"

    predicted = predict_blocks(broken)
    input_blocks = _parse_markdown_blocks(md)
    assert not all(_blocks_structurally_equal(a, b) for a, b in zip(input_blocks, predicted))


def test_catches_a_dropped_table():
    """If insertTable (and its cell text) never made it into the compiled
    requests, predict_blocks must report fewer blocks than the input."""
    md = "Some text.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    requests = compile_markdown(md, start_index=1)
    broken = [r for r in requests if "insertTable" not in r]
    # Also drop the cell-text inserts (they'd otherwise land as trunk text).
    broken = [
        r
        for r in broken
        if not ("insertText" in r and r["insertText"]["text"] in ("A", "B", "1", "2"))
    ]
    predicted = predict_blocks(broken)
    input_blocks = _parse_markdown_blocks(md)
    assert len(predicted) != len(input_blocks)


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_requests_produce_no_blocks():
    assert predict_blocks([]) == []


@pytest.mark.parametrize(
    "md",
    [
        "Just a paragraph.\n",
        "# H1\n\nBody.\n",
        "- a\n- b\n",
        "1. a\n2. b\n",
        "| A |\n|---|\n| 1 |\n",
    ],
)
def test_predicted_blocks_are_pure_and_deterministic(md):
    requests = compile_markdown(md, start_index=1)
    first = predict_blocks(requests)
    second = predict_blocks(requests)
    assert first == second
