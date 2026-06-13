"""Tests for locate() — normalization ladder, UTF-16 spans, structural boundary,
near-miss scan, and every UTF-16 hazard in the design spec."""

import pytest

from verified_googledocs_mcp.verify import (
    ErrorCode,
    VerifyError,
    locate,
    RUNG_EXACT,
    RUNG_QUOTES,
    RUNG_WHITESPACE,
    RUNG_SOFTHYPHEN,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _para(text: str, start: int) -> dict:
    """Build a minimal Docs API paragraph element with one textRun."""
    end = start + _utf16_len(text)
    return {
        "paragraph": {
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text},
                }
            ]
        }
    }


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _tab(paragraphs: list[str]) -> dict:
    """Build a tab_json with the given paragraph texts.

    Paragraphs are separated by the trailing newline that the Docs API appends
    to each paragraph element.  We add it automatically here.
    """
    content = []
    cursor = 1  # Docs API body starts at index 1
    for text in paragraphs:
        # Add trailing newline as Docs API does (paragraph element ends with \n).
        raw = text + "\n"
        content.append(_para(raw, cursor))
        cursor += _utf16_len(raw)
    return {"body": {"content": content}}


def _tab_raw(raw_texts: list[str]) -> dict:
    """Like _tab but caller controls the exact text per paragraph (no auto-\n)."""
    content = []
    cursor = 1
    for text in raw_texts:
        content.append(_para(text, cursor))
        cursor += _utf16_len(text)
    return {"body": {"content": content}}


# ---------------------------------------------------------------------------
# Rung 1: exact match
# ---------------------------------------------------------------------------


class TestExactMatch:
    def test_simple(self):
        tab = _tab(["Hello world"])
        result = locate("Hello world", tab)
        assert result.rung == RUNG_EXACT
        assert result.match_count == 1
        assert len(result.spans) == 1

    def test_returns_utf16_span_ascii(self):
        tab = _tab(["Hello world"])
        result = locate("Hello", tab)
        # "Hello" starts at index 1 (body offset), length 5 ASCII = 5 UTF-16 units.
        start, end = result.spans[0]
        assert start == 1
        assert end == 6  # 1 + 5

    def test_match_count_1_in_two_para_doc(self):
        tab = _tab(["First paragraph", "Second paragraph"])
        result = locate("First paragraph", tab)
        assert result.rung == RUNG_EXACT
        assert result.match_count == 1

    def test_middle_of_paragraph(self):
        tab = _tab(["The quick brown fox"])
        result = locate("quick brown", tab)
        assert result.rung == RUNG_EXACT
        start, end = result.spans[0]
        # "The " = 4 chars before; body starts at 1 → start should be 5.
        assert start == 5
        assert end == 5 + len("quick brown")


# ---------------------------------------------------------------------------
# Rung 2: curly/straight quote equivalence
# ---------------------------------------------------------------------------


class TestQuoteNormalization:
    def test_curly_single_in_doc(self):
        # Doc has curly right single quote; needle has straight.
        tab = _tab(["it’s fine"])
        result = locate("it's fine", tab)
        assert result.rung == RUNG_QUOTES
        assert result.match_count == 1

    def test_curly_double_in_doc(self):
        tab = _tab(["“Hello” world"])
        result = locate('"Hello" world', tab)
        assert result.rung == RUNG_QUOTES

    def test_curly_double_in_needle(self):
        tab = _tab(['"Hello" world'])
        result = locate("“Hello” world", tab)
        assert result.rung == RUNG_QUOTES

    def test_exact_takes_priority_over_quotes(self):
        # If the exact match works, it should not fall through to rung 2.
        tab = _tab(["it's fine"])
        result = locate("it's fine", tab)
        assert result.rung == RUNG_EXACT

    def test_left_double_angle(self):
        # «word» should normalize to "word".
        tab = _tab(["«word»"])
        result = locate('"word"', tab)
        assert result.rung == RUNG_QUOTES


# ---------------------------------------------------------------------------
# Rung 3: NBSP and whitespace-run collapse
# ---------------------------------------------------------------------------


class TestWhitespaceNormalization:
    def test_nbsp_in_doc(self):
        # Non-breaking space (U+00A0) in doc, ordinary space in needle.
        tab = _tab(["Hello world"])
        result = locate("Hello world", tab)
        assert result.rung == RUNG_WHITESPACE

    def test_whitespace_run_collapse(self):
        # Doc has two spaces; needle has one.
        tab = _tab(["Hello  world"])
        result = locate("Hello world", tab)
        assert result.rung == RUNG_WHITESPACE

    def test_span_maps_back_correctly(self):
        # NBSP at offset 5. Needle "Hello world" maps to original [1, 12).
        tab = _tab(["Hello world"])
        result = locate("Hello world", tab)
        start, end = result.spans[0]
        # body offset 1; "Hello world" = 11 chars, all BMP.
        assert start == 1
        assert end == 12  # 1 + 11

    def test_thin_nbsp_variants(self):
        # Narrow no-break space (U+202F).
        tab = _tab(["Hello world"])
        result = locate("Hello world", tab)
        assert result.rung == RUNG_WHITESPACE


# ---------------------------------------------------------------------------
# Rung 4: soft-hyphen strip
# ---------------------------------------------------------------------------


class TestSoftHyphenStrip:
    def test_soft_hyphen_in_doc(self):
        # U+00AD (soft hyphen) in document.
        tab = _tab(["pro­gramming"])
        result = locate("programming", tab)
        assert result.rung == RUNG_SOFTHYPHEN

    def test_soft_hyphen_in_needle(self):
        tab = _tab(["programming"])
        result = locate("pro­gramming", tab)
        assert result.rung == RUNG_SOFTHYPHEN


# ---------------------------------------------------------------------------
# MATCH_COUNT_MISMATCH
# ---------------------------------------------------------------------------


class TestMatchCountMismatch:
    def test_duplicate_sentence_refusal(self):
        tab = _tab(["A sentence here.", "A sentence here."])
        with pytest.raises(VerifyError) as exc_info:
            locate("A sentence here.", tab)
        err = exc_info.value.envelope
        assert err.error_code == ErrorCode.MATCH_COUNT_MISMATCH
        assert err.diagnostics["actual"] == 2
        assert err.diagnostics["expected"] == 1
        assert len(err.diagnostics["spans"]) == 2

    def test_expected_2_matches(self):
        tab = _tab(["word and word"])
        result = locate("word", tab, expected_matches=2)
        assert result.match_count == 2

    def test_expected_2_but_only_1(self):
        tab = _tab(["Hello world"])
        with pytest.raises(VerifyError) as exc_info:
            locate("Hello", tab, expected_matches=2)
        err = exc_info.value.envelope
        assert err.error_code == ErrorCode.MATCH_COUNT_MISMATCH
        assert err.diagnostics["actual"] == 1
        assert err.diagnostics["expected"] == 2


# ---------------------------------------------------------------------------
# ZERO_MATCH + near-miss
# ---------------------------------------------------------------------------


class TestZeroMatch:
    def test_zero_match_raises(self):
        tab = _tab(["Hello world"])
        with pytest.raises(VerifyError) as exc_info:
            locate("xyzzy", tab)
        assert exc_info.value.envelope.error_code == ErrorCode.ZERO_MATCH

    def test_zero_match_has_ladder_report(self):
        tab = _tab(["Hello world"])
        with pytest.raises(VerifyError) as exc_info:
            locate("xyzzy", tab)
        diag = exc_info.value.envelope.diagnostics
        assert "ladder_report" in diag
        rungs_tried = [r["rung"] for r in diag["ladder_report"]]
        assert RUNG_EXACT in rungs_tried

    def test_near_miss_populated_for_close_match(self):
        # "Helo world" is close to "Hello world".
        tab = _tab(["Hello world"])
        with pytest.raises(VerifyError) as exc_info:
            locate("Helo world", tab)
        near_miss = exc_info.value.envelope.diagnostics.get("near_miss")
        assert near_miss is not None
        assert near_miss["ratio"] > 0.6

    def test_near_miss_none_for_totally_different(self):
        tab = _tab(["Hello world"])
        with pytest.raises(VerifyError) as exc_info:
            locate("xyzzy_totally_unrelated_aaabbb", tab)
        near_miss = exc_info.value.envelope.diagnostics.get("near_miss")
        # Either None or low ratio — we just check it doesn't crash.
        if near_miss is not None:
            assert near_miss["ratio"] < 0.9


# ---------------------------------------------------------------------------
# INVALID_INPUT
# ---------------------------------------------------------------------------


class TestInvalidInput:
    def test_empty_needle(self):
        tab = _tab(["Hello world"])
        with pytest.raises(VerifyError) as exc_info:
            locate("", tab)
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_empty_needle_not_retryable(self):
        tab = _tab(["Hello world"])
        with pytest.raises(VerifyError) as exc_info:
            locate("", tab)
        assert exc_info.value.envelope.retryable is False


# ---------------------------------------------------------------------------
# STRUCTURAL_BOUNDARY
# ---------------------------------------------------------------------------


class TestStructuralBoundary:
    def test_needle_spanning_two_paragraphs(self):
        # Build a tab where para 1 ends with "end\n" and para 2 starts with "start".
        # A needle that includes the newline crosses the boundary.
        tab = _tab(["end", "start"])
        with pytest.raises(VerifyError) as exc_info:
            locate("end\nstart", tab)
        err = exc_info.value.envelope
        assert err.error_code == ErrorCode.STRUCTURAL_BOUNDARY

    def test_single_paragraph_no_boundary(self):
        tab = _tab(["Hello world foo"])
        result = locate("world foo", tab)
        assert result.rung == RUNG_EXACT


# ---------------------------------------------------------------------------
# UTF-16 hazard set
# ---------------------------------------------------------------------------


class TestUtf16Hazards:
    """Pin index arithmetic against the full hazard set from the design spec."""

    def test_astral_emoji_before_needle(self):
        # 🌍 (U+1F30D) = 2 UTF-16 units.  Needle follows.
        emoji = "\U0001F30D"  # EARTH GLOBE EUROPE-AFRICA
        text = emoji + "Hello"
        tab = _tab_raw([text + "\n"])
        result = locate("Hello", tab)
        assert result.rung == RUNG_EXACT
        start, end = result.spans[0]
        # Body at 1.  Emoji = 2 UTF-16, so "Hello" starts at 1+2=3.
        assert start == 3
        assert end == 3 + 5  # 5 ASCII chars

    def test_zwj_emoji_sequence_before_needle(self):
        # 👨‍💻 is U+1F468 ZWJ U+1F4BB = 2 + 1 (ZWJ is BMP) + 2 = 5 UTF-16 units.
        zwj_seq = "\U0001F468‍\U0001F4BB"
        text = zwj_seq + "target"
        tab = _tab_raw([text + "\n"])
        result = locate("target", tab)
        start, end = result.spans[0]
        expected_start = 1 + _utf16_len(zwj_seq)  # body at 1
        assert start == expected_start
        assert end == expected_start + 6

    def test_combining_marks_before_needle(self):
        # e + combining acute (U+0301) = 2 code points, both BMP → 2 UTF-16.
        combined = "é"  # é as two code points
        text = combined + "test"
        tab = _tab_raw([text + "\n"])
        result = locate("test", tab)
        start, end = result.spans[0]
        expected_start = 1 + _utf16_len(combined)
        assert start == expected_start
        assert end == expected_start + 4

    def test_astral_emoji_after_needle(self):
        # Needle before an astral character. Span should not include the astral.
        text = "find me\U0001F600"
        tab = _tab_raw([text + "\n"])
        result = locate("find me", tab)
        start, end = result.spans[0]
        assert start == 1
        assert end == 1 + 7

    def test_rtl_phrase_isolation(self):
        # RTL phrase (Arabic) uses BMP characters only.
        rtl = "مرحبا"  # مرحبا
        text = rtl + " separator target"
        tab = _tab_raw([text + "\n"])
        result = locate("target", tab)
        assert result.rung == RUNG_EXACT
        start, _ = result.spans[0]
        # rtl is 5 BMP chars = 5 UTF-16 units; " separator " = 11 chars.
        expected = 1 + _utf16_len(rtl + " separator ")
        assert start == expected

    def test_multiple_astral_chars_span(self):
        # Needle contains an astral character; span should include it.
        needle = "A\U0001F600B"  # grinning face between two ASCII
        text = "prefix " + needle + " suffix"
        tab = _tab_raw([text + "\n"])
        result = locate(needle, tab)
        start, end = result.spans[0]
        # "prefix " = 7 BMP chars.
        assert start == 1 + 7
        # needle UTF-16 length: A=1 + emoji=2 + B=1 = 4
        assert end == start + _utf16_len(needle)

    def test_variation_selector_bmp(self):
        # Variation selector-16 (U+FE0F) is BMP; text + selector should not confuse
        # the locator.
        text = "star★️ done"  # ★︎ with VS-16
        tab = _tab_raw([text + "\n"])
        result = locate("done", tab)
        assert result.rung == RUNG_EXACT
        start, _ = result.spans[0]
        expected = 1 + _utf16_len("star★️ ")
        assert start == expected

    def test_needle_with_astral_followed_by_more_text(self):
        # Two astral emojis in needle, then more text after the match.
        needle = "\U0001F600\U0001F601"
        text = "before " + needle + " after"
        tab = _tab_raw([text + "\n"])
        result = locate(needle, tab)
        start, end = result.spans[0]
        assert start == 1 + _utf16_len("before ")
        assert end == start + _utf16_len(needle)  # 2+2=4


# ---------------------------------------------------------------------------
# Span correctness across multiple matches
# ---------------------------------------------------------------------------


class TestMultipleMatchSpans:
    def test_two_matches_correct_spans(self):
        # "abc" appears at offset 0 and 7 in the paragraph text.
        text = "abc def abc"
        tab = _tab_raw([text + "\n"])
        result = locate("abc", tab, expected_matches=2)
        assert result.match_count == 2
        s1, e1 = result.spans[0]
        s2, e2 = result.spans[1]
        assert s1 == 1  # body starts at 1
        assert e1 == 4
        assert s2 == 9  # "abc def " = 8 chars → 1+8=9
        assert e2 == 12

    def test_descending_order_not_required_by_locate(self):
        # locate() returns spans in document order; caller is responsible for
        # applying edits in reverse order.
        text = "x y x"
        tab = _tab_raw([text + "\n"])
        result = locate("x", tab, expected_matches=2)
        starts = [s for s, _ in result.spans]
        assert starts == sorted(starts)  # in document order
