"""Tests for the text-edit evidence helper in verify.py.

Pure unit tests; no network or credentials.
"""

from __future__ import annotations

from verified_googledocs_mcp.verify import (
    LocateResult,
    RUNG_EXACT,
    assemble_text_edit_evidence,
    _u16_to_codepoint,
)


# ---------------------------------------------------------------------------
# Helpers for building minimal tab JSON (same shape as test_locate.py)
# ---------------------------------------------------------------------------


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _para(text: str, start: int) -> dict:
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


def _tab(paragraphs: list[str]) -> dict:
    content = []
    cursor = 1
    for text in paragraphs:
        raw = text + "\n"
        content.append(_para(raw, cursor))
        cursor += _utf16_len(raw)
    return {"body": {"content": content}}


def _tab_raw(raw_texts: list[str]) -> dict:
    content = []
    cursor = 1
    for text in raw_texts:
        content.append(_para(text, cursor))
        cursor += _utf16_len(text)
    return {"body": {"content": content}}


# ---------------------------------------------------------------------------
# _u16_to_codepoint
# ---------------------------------------------------------------------------


class TestU16ToCodepoint:
    def test_ascii_identity(self):
        from verified_googledocs_mcp.verify import _flatten_tab

        tab = _tab(["Hello world"])
        text, u16_map, _ = _flatten_tab(tab)
        # For pure ASCII, u16_map[i] == i + 1 (body starts at 1).
        for cp_idx in range(len(text)):
            recovered = _u16_to_codepoint(u16_map[cp_idx], u16_map)
            assert recovered == cp_idx

    def test_astral_char_offset(self):
        from verified_googledocs_mcp.verify import _flatten_tab

        # 🌍 = 2 UTF-16 units; text = "🌍A"
        emoji = "\U0001f30d"
        tab = _tab_raw([emoji + "A\n"])
        text, u16_map, _ = _flatten_tab(tab)
        # code-point 0 = emoji (starts at u16 index 1)
        assert _u16_to_codepoint(1, u16_map) == 0
        # code-point 1 = "A" (starts at u16 index 3 because emoji = 2 units)
        assert _u16_to_codepoint(3, u16_map) == 1


# ---------------------------------------------------------------------------
# assemble_text_edit_evidence — evidence shape
# ---------------------------------------------------------------------------


def _make_locate_result(spans: list[tuple[int, int]], count: int = 1) -> LocateResult:
    return LocateResult(spans=spans, rung=RUNG_EXACT, match_count=count)


class TestAssembleTextEditEvidence:
    def test_evidence_keys_present(self):
        tab = _tab(["Hello world"])
        locate_result = _make_locate_result([(1, 6)])  # "Hello"
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        for key in (
            "applied",
            "match_count",
            "rung",
            "before",
            "after",
            "revision_before",
            "revision_after",
            "audit_logged",
        ):
            assert key in evidence, f"key {key!r} missing from evidence"

    def test_applied_false_for_dryrun(self):
        tab = _tab(["Hello world"])
        locate_result = _make_locate_result([(1, 6)])
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="rev-1",
            revision_after="",
            applied=False,
            audit_logged=False,
            audit_log_reason="dry_run",
        )
        assert evidence["applied"] is False
        assert evidence["revision_after"] == ""
        assert evidence["audit_log_reason"] == "dry_run"

    def test_match_count_and_rung_propagated(self):
        tab = _tab(["word and word"])
        locate_result = LocateResult(spans=[(1, 5), (10, 14)], rung="exact", match_count=2)
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        assert evidence["match_count"] == 2
        assert evidence["rung"] == "exact"

    def test_before_excerpt_contains_target_text(self):
        tab = _tab(["The quick brown fox"])
        # "quick" starts at UTF-16 5 (1-based body + 4 chars "The "), ends at 10
        locate_result = _make_locate_result([(5, 10)])
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        assert "quick" in evidence["before"]

    def test_audit_logged_false_embedded(self):
        tab = _tab(["Hello"])
        locate_result = _make_locate_result([(1, 6)])
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=False,
            audit_log_reason="disk full",
        )
        assert evidence["audit_logged"] is False
        assert evidence["audit_log_reason"] == "disk full"

    def test_no_audit_log_reason_key_when_empty(self):
        tab = _tab(["Hello"])
        locate_result = _make_locate_result([(1, 6)])
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        # audit_log_reason should be absent when empty
        assert "audit_log_reason" not in evidence

    def test_revision_ids_propagated(self):
        tab = _tab(["Hello world"])
        locate_result = _make_locate_result([(1, 6)])
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="REV-BEFORE",
            revision_after="REV-AFTER",
            applied=True,
            audit_logged=True,
        )
        assert evidence["revision_before"] == "REV-BEFORE"
        assert evidence["revision_after"] == "REV-AFTER"

    def test_astral_char_before_span(self):
        """Excerpt with astral char before the span must not crash or mis-index."""
        emoji = "\U0001f30d"  # 2 UTF-16 units
        tab = _tab_raw([emoji + "Hello\n"])
        # "Hello" starts at UTF-16 index 1+2=3, ends at 3+5=8
        locate_result = _make_locate_result([(3, 8)])
        evidence = assemble_text_edit_evidence(
            locate_result=locate_result,
            pre_tab_json=tab,
            post_tab_json=tab,
            revision_before="r1",
            revision_after="r2",
            applied=True,
            audit_logged=True,
        )
        # before excerpt must include "Hello"
        assert "Hello" in evidence["before"]
