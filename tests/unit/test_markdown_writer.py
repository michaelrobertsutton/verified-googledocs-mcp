"""Unit tests for the markdown -> batchUpdate request compiler.

All tests are pure: no network, no Google API calls.
"""

from __future__ import annotations

import pytest

from verified_googledocs_mcp.markdown_writer import (
    UnsupportedMarkdown,
    _utf16_len,
    compile_markdown,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_texts(requests: list) -> list[str]:
    """Extract ordered insertText strings from a request list."""
    return [r["insertText"]["text"] for r in requests if "insertText" in r]


def _find_requests(requests: list, key: str) -> list[dict]:
    return [r[key] for r in requests if key in r]


# ---------------------------------------------------------------------------
# Empty / whitespace input
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_list():
    assert compile_markdown("") == []


def test_whitespace_only_returns_empty_list():
    assert compile_markdown("   \n  ") == []


# ---------------------------------------------------------------------------
# UTF-16 helper
# ---------------------------------------------------------------------------


def test_utf16_len_ascii():
    assert _utf16_len("hello") == 5


def test_utf16_len_astral_emoji():
    # 😀 is U+1F600, one code point, two UTF-16 units.
    assert _utf16_len("😀") == 2


def test_utf16_len_zwj_sequence():
    # 👨‍👩‍👧 is a ZWJ sequence: three emoji + two ZWJs = 3*2 + 2*1 = 8 UTF-16 units.
    seq = "👨‍👩‍👧"
    # Each of the three emoji is 2 units; each ZWJ is 1 unit.
    assert _utf16_len(seq) == 8


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------


def test_heading_level_1_produces_insert_and_para_style():
    reqs = compile_markdown("# Hello World\n")
    inserts = _find_requests(reqs, "insertText")
    assert any("Hello World" in r["text"] for r in inserts)
    para_styles = _find_requests(reqs, "updateParagraphStyle")
    assert len(para_styles) == 1
    assert para_styles[0]["paragraphStyle"]["namedStyleType"] == "HEADING_1"


def test_heading_level_2():
    reqs = compile_markdown("## Section\n")
    para_styles = _find_requests(reqs, "updateParagraphStyle")
    assert para_styles[0]["paragraphStyle"]["namedStyleType"] == "HEADING_2"


def test_heading_level_3():
    reqs = compile_markdown("### Sub\n")
    para_styles = _find_requests(reqs, "updateParagraphStyle")
    assert para_styles[0]["paragraphStyle"]["namedStyleType"] == "HEADING_3"


def test_heading_para_style_fields_mask():
    reqs = compile_markdown("# Title\n")
    para_styles = _find_requests(reqs, "updateParagraphStyle")
    assert para_styles[0]["fields"] == "namedStyleType"


# ---------------------------------------------------------------------------
# Bold / italic / bold+italic
# ---------------------------------------------------------------------------


def test_bold_span_produces_update_text_style():
    reqs = compile_markdown("**bold text**\n")
    text_styles = _find_requests(reqs, "updateTextStyle")
    bold_styles = [s for s in text_styles if s.get("textStyle", {}).get("bold")]
    assert len(bold_styles) >= 1
    assert bold_styles[0]["fields"] == "bold"


def test_italic_span():
    reqs = compile_markdown("*italic text*\n")
    text_styles = _find_requests(reqs, "updateTextStyle")
    italic_styles = [s for s in text_styles if s.get("textStyle", {}).get("italic")]
    assert len(italic_styles) >= 1
    assert italic_styles[0]["fields"] == "italic"


def test_bold_italic_combined():
    # markdown-it-py parses ***text*** as em > strong (nested spans).
    # The compiler emits separate updateTextStyle requests for bold and italic,
    # both covering the same text range.  We verify both styles are present.
    reqs = compile_markdown("***bold-italic***\n")
    text_styles = _find_requests(reqs, "updateTextStyle")
    has_bold = any(s.get("textStyle", {}).get("bold") for s in text_styles)
    has_italic = any(s.get("textStyle", {}).get("italic") for s in text_styles)
    assert has_bold, "Expected a bold updateTextStyle request"
    assert has_italic, "Expected an italic updateTextStyle request"


def test_style_range_indices_are_integers():
    reqs = compile_markdown("**bold**\n")
    for r in _find_requests(reqs, "updateTextStyle"):
        assert isinstance(r["range"]["startIndex"], int)
        assert isinstance(r["range"]["endIndex"], int)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_link_produces_update_text_style_with_link():
    reqs = compile_markdown("[Example](https://example.com)\n")
    text_styles = _find_requests(reqs, "updateTextStyle")
    link_styles = [s for s in text_styles if "link" in s.get("textStyle", {})]
    assert len(link_styles) == 1
    assert link_styles[0]["textStyle"]["link"]["url"] == "https://example.com"
    assert link_styles[0]["fields"] == "link"


def test_link_text_is_inserted():
    reqs = compile_markdown("[Click here](https://example.com)\n")
    texts = _insert_texts(reqs)
    combined = "".join(texts)
    assert "Click here" in combined


# ---------------------------------------------------------------------------
# Unordered list
# ---------------------------------------------------------------------------


def test_unordered_list_produces_bullets():
    reqs = compile_markdown("- item one\n- item two\n")
    bullet_reqs = _find_requests(reqs, "createParagraphBullets")
    assert len(bullet_reqs) == 2
    for br in bullet_reqs:
        assert br["bulletPreset"] == "BULLET_DISC_CIRCLE_SQUARE"


def test_ordered_list_produces_numbered_bullets():
    reqs = compile_markdown("1. first\n2. second\n")
    bullet_reqs = _find_requests(reqs, "createParagraphBullets")
    assert len(bullet_reqs) == 2
    for br in bullet_reqs:
        assert br["bulletPreset"] == "NUMBERED_DECIMAL_ALPHA_ROMAN"


# ---------------------------------------------------------------------------
# Nested lists
# ---------------------------------------------------------------------------


def test_nested_unordered_list():
    src = "- parent\n  - child\n  - child 2\n"
    reqs = compile_markdown(src)
    bullet_reqs = _find_requests(reqs, "createParagraphBullets")
    # 1 parent + 2 children = 3 bullet paragraphs.
    assert len(bullet_reqs) == 3


def test_nested_list_insert_text_present():
    src = "- parent\n  - child\n"
    reqs = compile_markdown(src)
    combined = "".join(_insert_texts(reqs))
    assert "parent" in combined
    assert "child" in combined


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def test_table_produces_insert_table_request():
    src = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    reqs = compile_markdown(src)
    table_reqs = _find_requests(reqs, "insertTable")
    assert len(table_reqs) == 1
    assert table_reqs[0]["rows"] == 2
    assert table_reqs[0]["columns"] == 2


def test_table_cell_text_is_inserted():
    src = "| Header |\n|---|\n| Cell text |\n"
    reqs = compile_markdown(src)
    combined = "".join(_insert_texts(reqs))
    assert "Header" in combined
    assert "Cell text" in combined


def test_table_insert_location_is_at_start_index():
    src = "| X |\n|---|\n| Y |\n"
    reqs = compile_markdown(src, start_index=5)
    table_reqs = _find_requests(reqs, "insertTable")
    assert table_reqs[0]["location"]["index"] == 5


def _cell_text_inserts(reqs: list, insert_at: int) -> list[tuple[int, str]]:
    """insertText requests at or after insert_at, in the order they were emitted."""
    return [
        (r["insertText"]["location"]["index"], r["insertText"]["text"])
        for r in reqs
        if "insertText" in r and r["insertText"]["location"]["index"] >= insert_at
    ]


def test_table_cell_indices_match_pinned_geometry():
    """Regression test for the off-by-two bug: cell(r, c) paragraph index must be
    table_start + 3 + r*stride + c*2, where table_start = insert_at + 1 — pinned by
    the live contract test tests/live/test_markdown_writes.py::TestTableGeometryProbe.
    """
    insert_at = 1
    src = "| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n"
    reqs = compile_markdown(src, start_index=insert_at)
    table_start = insert_at + 1
    n_cols = 3
    stride = n_cols * 2 + 1

    inserts_by_index = {
        r["insertText"]["location"]["index"]: r["insertText"]["text"]
        for r in reqs
        if "insertText" in r
    }
    expected = {
        (0, 0): "A",
        (0, 1): "B",
        (0, 2): "C",
        (1, 0): "1",
        (1, 1): "2",
        (1, 2): "3",
    }
    for (r_idx, c_idx), text in expected.items():
        cell_index = table_start + 3 + r_idx * stride + c_idx * 2
        assert inserts_by_index.get(cell_index) == text, (
            f"cell ({r_idx},{c_idx}) expected {text!r} at index {cell_index}, "
            f"got {inserts_by_index.get(cell_index)!r}"
        )


def test_content_after_table_accounts_for_cell_text_length():
    """The post-table cursor must include every inserted cell's UTF-16 length,
    not just the empty table's structural size — otherwise content after a
    table lands at a stale (pre-population) index."""
    insert_at = 1
    src = "| Header cell text |\n|---|\n| Body cell text |\n\nAfter the table.\n"
    reqs = compile_markdown(src, start_index=insert_at)

    n_rows, n_cols = 2, 1
    stride = n_cols * 2 + 1
    table_structural_size = 1 + 1 + n_rows * stride + 1
    total_cell_text_length = len("Header cell text") + len("Body cell text")
    expected_index = insert_at + table_structural_size + total_cell_text_length

    inserts_by_index = {
        r["insertText"]["location"]["index"]: r["insertText"]["text"]
        for r in reqs
        if "insertText" in r
    }
    assert inserts_by_index.get(expected_index, "").startswith("After the table")


def test_table_cell_bold_style_span_shifted_for_reverse_insertion():
    """Cells are inserted highest-index first; a lower-index cell's insertion
    shifts an already-inserted higher-index cell's text forward. Style spans
    (applied after every insert) must be shifted to match."""
    insert_at = 1
    src = "| **Left** | **Right** |\n|---|---|\n"
    reqs = compile_markdown(src, start_index=insert_at)

    table_start = insert_at + 1
    n_cols = 2
    stride = n_cols * 2 + 1
    left_index = table_start + 3 + 0 * stride + 0 * 2
    right_index = table_start + 3 + 0 * stride + 1 * 2

    # Sanity: both cells' own insertText land at the pinned (unshifted) indices —
    # the reverse-order insertion technique itself is untouched by this fix.
    inserts_by_index = {
        r["insertText"]["location"]["index"]: r["insertText"]["text"]
        for r in reqs
        if "insertText" in r
    }
    assert inserts_by_index.get(left_index) == "Left"
    assert inserts_by_index.get(right_index) == "Right"

    # "Right" is the higher-index cell; "Left" is inserted after it in the
    # batch, shifting "Right"'s already-placed text forward by len("Left").
    bold_styles = _find_requests(reqs, "updateTextStyle")
    right_style = next(
        s for s in bold_styles if s["range"]["startIndex"] == right_index + len("Left")
    )
    assert right_style["range"]["endIndex"] == right_index + len("Left") + len("Right")

    # "Left" is the lowest-index cell, inserted last — nothing shifts it.
    left_style = next(s for s in bold_styles if s["range"]["startIndex"] == left_index)
    assert left_style["range"]["endIndex"] == left_index + len("Left")


def test_table_first_element_after_heading():
    """Table as the first body element right after a heading — the original
    reported repro shape."""
    src = "# Title\n\n| A | B |\n|---|---|\n| one sentence here. | another sentence. |\n"
    reqs = compile_markdown(src, start_index=1)
    combined = "".join(_insert_texts(reqs))
    assert "Title" in combined
    assert "one sentence here." in combined
    assert "another sentence." in combined


def test_multi_row_table_all_cells_present():
    src = "| A | B |\n|---|---|\n| r0c0 | r0c1 |\n| r1c0 | r1c1 |\n| r2c0 | r2c1 |\n"
    reqs = compile_markdown(src, start_index=1)
    combined = "".join(_insert_texts(reqs))
    for expected in ("r0c0", "r0c1", "r1c0", "r1c1", "r2c0", "r2c1"):
        assert expected in combined


# ---------------------------------------------------------------------------
# UTF-16 index correctness with astral emoji
# ---------------------------------------------------------------------------


def test_utf16_index_after_astral_emoji():
    """An emoji paragraph is followed by a second paragraph; indices must use
    UTF-16 lengths, not Python len()."""
    src = "😀 smile\n\nsecond para\n"
    reqs = compile_markdown(src, start_index=1)

    # Collect all insertText requests in order.
    inserts = [
        (r["insertText"]["location"]["index"], r["insertText"]["text"])
        for r in reqs
        if "insertText" in r
    ]

    # Find the insert containing "second para".
    second_para_inserts = [(idx, t) for idx, t in inserts if "second" in t]
    assert second_para_inserts, "Expected an insert containing 'second para'"

    # The emoji text "😀 smile\n" has UTF-16 length:
    # 😀 = 2, ' ' = 1, 's','m','i','l','e' = 5, '\n' = 1 → total 9
    # So the second paragraph should start at 1 + 9 = 10.
    # (If Python len() were used: 1 + 8 = 9 — that would be wrong.)
    emoji_text = "😀 smile\n"
    expected_start = 1 + _utf16_len(emoji_text)
    actual_start = second_para_inserts[0][0]
    assert actual_start == expected_start, (
        f"Expected second para at index {expected_start}, got {actual_start}. "
        "Likely a UTF-16 vs code-point indexing error."
    )


# ---------------------------------------------------------------------------
# UnsupportedMarkdown exceptions
# ---------------------------------------------------------------------------


def test_image_raises_unsupported_markdown():
    with pytest.raises(UnsupportedMarkdown) as exc_info:
        compile_markdown("![alt](https://example.com/img.png)\n")
    assert exc_info.value.construct == "image"


def test_code_fence_raises_unsupported_markdown():
    with pytest.raises(UnsupportedMarkdown) as exc_info:
        compile_markdown("```python\nprint('hi')\n```\n")
    assert exc_info.value.construct == "fence"


def test_blockquote_raises_unsupported_markdown():
    with pytest.raises(UnsupportedMarkdown) as exc_info:
        compile_markdown("> a quote\n")
    assert exc_info.value.construct == "blockquote"


def test_unsupported_markdown_carries_source_map():
    with pytest.raises(UnsupportedMarkdown) as exc_info:
        compile_markdown("```\ncode\n```\n")
    # source_map may be None or a list — it must exist as an attribute.
    assert hasattr(exc_info.value, "source_map")


def test_unsupported_markdown_str_includes_construct():
    exc = UnsupportedMarkdown(construct="image", source_map=[0, 1])
    assert "image" in str(exc)
    assert "1" in str(exc)  # line numbers are 1-based in the str
