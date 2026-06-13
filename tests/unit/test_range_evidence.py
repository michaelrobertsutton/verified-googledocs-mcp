"""Unit tests for range/markdown evidence and structural diff in verify.py.

Tests:
- assemble_range_markdown_evidence: structural comparison, accepted lossy transforms,
  genuine table drop flagged, guardrail behavior.
- assemble_structural_evidence: inline object confirmation.
- _parse_markdown_blocks: accepted lossy transform equivalences.
"""

from __future__ import annotations

from typing import Any

from verified_googledocs_mcp.verify import (
    _blocks_structurally_equal,
    _parse_markdown_blocks,
    assemble_range_markdown_evidence,
    assemble_structural_evidence,
)


# ---------------------------------------------------------------------------
# Primitive body builders
# ---------------------------------------------------------------------------


def _para(text: str, start: int, end: int | None = None) -> dict[str, Any]:
    if end is None:
        end = start + len(text)
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text},
                }
            ],
        },
    }


def _heading_para(level: int, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": f"HEADING_{level}"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text + "\n"},
                }
            ],
        },
    }


def _inline_image_para(obj_id: str, start: int, end: int) -> dict[str, Any]:
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "inlineObjectElement": {"inlineObjectId": obj_id},
                }
            ],
        },
    }


def _table_elem(start: int, end: int) -> dict[str, Any]:
    return {
        "startIndex": start,
        "endIndex": end,
        "table": {
            "rows": 2,
            "columns": 2,
            "tableRows": [
                {
                    "tableCells": [
                        {"content": [_para("Header A\n", start + 2, start + 11)]},
                        {"content": [_para("Header B\n", start + 13, start + 22)]},
                    ]
                },
                {
                    "tableCells": [
                        {"content": [_para("Cell 1\n", start + 24, start + 31)]},
                        {"content": [_para("Cell 2\n", start + 33, start + 40)]},
                    ]
                },
            ],
        },
    }


def _body(*elems: dict[str, Any]) -> dict[str, Any]:
    return {"content": list(elems)}


# ---------------------------------------------------------------------------
# _parse_markdown_blocks: accepted lossy transforms
# ---------------------------------------------------------------------------


class TestParsedMarkdownBlocks:
    def test_heading_block(self) -> None:
        blocks = _parse_markdown_blocks("# Hello World")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "heading"
        assert blocks[0]["level"] == 1
        assert blocks[0]["text"] == "Hello World"

    def test_paragraph_block(self) -> None:
        blocks = _parse_markdown_blocks("Plain paragraph text")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"
        assert "Plain paragraph text" in blocks[0]["text"]

    def test_table_block(self) -> None:
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        blocks = _parse_markdown_blocks(md)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "table"
        assert blocks[0]["rows"] == 2
        assert blocks[0]["cols"] == 2

    def test_list_item_blocks(self) -> None:
        blocks = _parse_markdown_blocks("- item one\n- item two")
        list_items = [b for b in blocks if b["type"] == "list_item"]
        assert len(list_items) == 2

    def test_bullet_marker_style_is_lossy(self) -> None:
        """Bullet style '-' vs '*' vs '1.' should produce equivalent list_item blocks."""
        blocks_dash = _parse_markdown_blocks("- item one")
        blocks_star = _parse_markdown_blocks("* item one")
        blocks_num = _parse_markdown_blocks("1. item one")
        # All three should yield a list_item with the same text
        assert blocks_dash[0]["type"] == "list_item"
        assert blocks_star[0]["type"] == "list_item"
        assert blocks_num[0]["type"] == "list_item"
        assert blocks_dash[0]["text"] == blocks_star[0]["text"] == blocks_num[0]["text"]

    def test_whitespace_runs_normalized(self) -> None:
        blocks = _parse_markdown_blocks("Hello  world")
        assert blocks[0]["text"] == "Hello world"

    def test_markdown_escaping_stripped(self) -> None:
        blocks = _parse_markdown_blocks(r"Hello \*world\*")
        assert blocks[0]["text"] == "Hello *world*"

    def test_empty_markdown_returns_no_blocks(self) -> None:
        assert _parse_markdown_blocks("") == []
        assert _parse_markdown_blocks("   \n\n  ") == []


# ---------------------------------------------------------------------------
# _blocks_structurally_equal
# ---------------------------------------------------------------------------


class TestBlocksStructurallyEqual:
    def test_equal_headings(self) -> None:
        a = {"type": "heading", "level": 2, "text": "Introduction", "link_targets": []}
        b = {"type": "heading", "level": 2, "text": "Introduction", "link_targets": []}
        assert _blocks_structurally_equal(a, b)

    def test_different_heading_level(self) -> None:
        a = {"type": "heading", "level": 1, "text": "Intro", "link_targets": []}
        b = {"type": "heading", "level": 2, "text": "Intro", "link_targets": []}
        assert not _blocks_structurally_equal(a, b)

    def test_different_block_type(self) -> None:
        a = {"type": "heading", "level": 1, "text": "Hi", "link_targets": []}
        b = {"type": "paragraph", "text": "Hi", "link_targets": []}
        assert not _blocks_structurally_equal(a, b)

    def test_equal_tables(self) -> None:
        a = {"type": "table", "rows": 3, "cols": 2, "link_targets": []}
        b = {"type": "table", "rows": 3, "cols": 2, "link_targets": []}
        assert _blocks_structurally_equal(a, b)

    def test_different_table_dims(self) -> None:
        a = {"type": "table", "rows": 3, "cols": 2, "link_targets": []}
        b = {"type": "table", "rows": 2, "cols": 2, "link_targets": []}
        assert not _blocks_structurally_equal(a, b)


# ---------------------------------------------------------------------------
# assemble_range_markdown_evidence
# ---------------------------------------------------------------------------


class TestAssembleRangeMarkdownEvidence:
    def test_keys_present(self) -> None:
        post = _body(_para("Hello world\n", 1, 13))
        ev = assemble_range_markdown_evidence(
            input_markdown="Hello world",
            post_body=post,
            start_index=1,
            end_index=13,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        for key in (
            "applied",
            "revision_before",
            "revision_after",
            "structural_match",
            "input_blocks",
            "post_blocks",
            "audit_logged",
        ):
            assert key in ev, f"key {key!r} missing"

    def test_structural_match_on_same_content(self) -> None:
        # A post body with plain text that matches the input.
        post = _body(_para("Hello world\n", 1, 13))
        ev = assemble_range_markdown_evidence(
            input_markdown="Hello world",
            post_body=post,
            start_index=1,
            end_index=13,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        assert ev["structural_match"] is True
        assert "structural_diff" not in ev

    def test_structural_mismatch_on_dropped_table(self) -> None:
        # Input has a table; post body has a plain paragraph (table was dropped).
        input_md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        post = _body(_para("Some text\n", 1, 11))  # no table
        ev = assemble_range_markdown_evidence(
            input_markdown=input_md,
            post_body=post,
            start_index=1,
            end_index=11,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        assert ev["structural_match"] is False
        assert "structural_diff" in ev
        assert len(ev["structural_diff"]) > 0

    def test_accepted_lossy_transform_whitespace(self) -> None:
        # Input has extra spaces; post body normalizes to single space.
        # Both sides should match after normalization.
        post = _body(_para("Hello world\n", 1, 13))
        ev = assemble_range_markdown_evidence(
            input_markdown="Hello  world",  # double space (lossy transform)
            post_body=post,
            start_index=1,
            end_index=13,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        assert ev["structural_match"] is True

    def test_revision_ids_propagated(self) -> None:
        post = _body(_para("Hello\n", 1, 7))
        ev = assemble_range_markdown_evidence(
            input_markdown="Hello",
            post_body=post,
            start_index=1,
            end_index=7,
            revision_before="rev-AAA",
            revision_after="rev-BBB",
            applied=True,
            audit_logged=True,
        )
        assert ev["revision_before"] == "rev-AAA"
        assert ev["revision_after"] == "rev-BBB"

    def test_audit_log_reason_absent_when_empty(self) -> None:
        post = _body(_para("Hello\n", 1, 7))
        ev = assemble_range_markdown_evidence(
            input_markdown="Hello",
            post_body=post,
            start_index=1,
            end_index=7,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
            audit_log_reason="",
        )
        assert "audit_log_reason" not in ev

    def test_audit_log_reason_present_when_set(self) -> None:
        post = _body(_para("Hello\n", 1, 7))
        ev = assemble_range_markdown_evidence(
            input_markdown="Hello",
            post_body=post,
            start_index=1,
            end_index=7,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=False,
            audit_logged=False,
            audit_log_reason="permission denied",
        )
        assert ev["audit_log_reason"] == "permission denied"

    def test_heading_match_across_roundtrip(self) -> None:
        # A heading in input should match a heading in post body.
        post = _body(_heading_para(2, "My Section", 1, 14))
        ev = assemble_range_markdown_evidence(
            input_markdown="## My Section",
            post_body=post,
            start_index=1,
            end_index=14,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        assert ev["structural_match"] is True

    def test_consecutive_paragraphs_match_across_roundtrip(self) -> None:
        # Regression for #36: the re-export joined consecutive paragraphs with a
        # single newline, so markdown-it merged them into one paragraph and the
        # block count came up short (input 3, post 2) — a false negative. The
        # body and the input are structurally identical, so this must match.
        post = _body(
            _heading_para(1, "Synced Document", 1, 18),
            _para("First paragraph after sync.\n", 18, 47),
            _para("Second paragraph after sync.\n", 47, 77),
        )
        ev = assemble_range_markdown_evidence(
            input_markdown=(
                "# Synced Document\n\nFirst paragraph after sync.\n\nSecond paragraph after sync.\n"
            ),
            post_body=post,
            start_index=1,
            end_index=77,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        assert ev["input_blocks"] == 3
        assert ev["post_blocks"] == 3
        assert ev["structural_match"] is True
        assert "structural_diff" not in ev


# ---------------------------------------------------------------------------
# assemble_structural_evidence
# ---------------------------------------------------------------------------


class TestAssembleStructuralEvidence:
    def test_keys_present(self) -> None:
        post = _body(_para("Hello\n", 1, 7))
        ev = assemble_structural_evidence(
            post_body=post,
            anchor_paragraph_start=1,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        for key in (
            "applied",
            "revision_before",
            "revision_after",
            "inline_object_confirmed",
            "audit_logged",
        ):
            assert key in ev

    def test_inline_object_confirmed_when_present(self) -> None:
        # Post body: anchor para at [1,10), image para at [10,12).
        post = _body(
            _para("Hello anchor text\n", 1, 19),
            _inline_image_para("img-001", 19, 21),
        )
        ev = assemble_structural_evidence(
            post_body=post,
            anchor_paragraph_start=1,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        assert ev["inline_object_confirmed"] is True

    def test_inline_object_not_confirmed_when_absent(self) -> None:
        post = _body(_para("Hello\n", 1, 7))
        ev = assemble_structural_evidence(
            post_body=post,
            anchor_paragraph_start=1,
            revision_before="rev-1",
            revision_after="rev-2",
            applied=True,
            audit_logged=True,
        )
        assert ev["inline_object_confirmed"] is False

    def test_revision_ids_propagated(self) -> None:
        post = _body(_para("Hello\n", 1, 7))
        ev = assemble_structural_evidence(
            post_body=post,
            anchor_paragraph_start=1,
            revision_before="rev-AAA",
            revision_after="rev-BBB",
            applied=True,
            audit_logged=True,
        )
        assert ev["revision_before"] == "rev-AAA"
        assert ev["revision_after"] == "rev-BBB"
