"""§ format_text — character styling against the live API.

Covers the issue #63 acceptance sketch: bold a phrase in prose, verified at
the structured-run level; bold a phrase inside a merged-cell table cell,
verified the merge survives; zero/multi-match clean refusals; idempotent
re-run; and a concurrent-edit smoke test grounded in this repo's own observed
comment-driven revision bumps.

Mutating cases run against a fresh disposable copy; read-only / dry-run cases
run against the canonical fixture.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

DUP_SENTENCE = "The quick brown fox jumps over the lazy dog."


def _err(result) -> str:  # type: ignore[no-untyped-def]
    return str(result.content)


async def _read(client, doc_id, tab_id) -> str:  # type: ignore[no-untyped-def]
    r = await client.call_tool(
        "read_document", {"doc_id": doc_id, "tab_id": tab_id, "format": "markdown"}
    )
    return r.data["content"]


async def _structured_runs(client, doc_id, tab_id) -> list[dict]:  # type: ignore[no-untyped-def]
    r = await client.call_tool(
        "read_document", {"doc_id": doc_id, "tab_id": tab_id, "format": "structured"}
    )
    return [run for para in r.data["content"]["paragraphs"] for run in para["runs"]]


# ---------------------------------------------------------------------------
# Prose bold — verified at the structured-run level, not a text diff
# ---------------------------------------------------------------------------


class TestProseBold:
    async def test_bold_applied_and_confirmed_via_structured_read(self, client, scratch_doc):
        s = scratch_doc
        r = await client.call_tool(
            "format_text",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "find": "[rev-probe]",
                "style": {"bold": True},
            },
        )
        data = r.data
        assert data["applied"] is True
        assert data["style_mutated"] is True
        assert data["content_mutated"] is False
        assert data["compiled_request_kinds"] == ["updateTextStyle"]
        assert data["revision_before"] != data["revision_after"]

        # The plain-text read is unchanged (proves no content mutation)...
        after_md = await _read(client, s.doc_id, s.primary_tab)
        assert "[rev-probe]" in after_md.replace("\\", "")

        # ...but the structured read shows the run is now actually bold, not
        # merely re-serialized as **[rev-probe]** (the exact ambiguity issue
        # #63 calls out: a markdown diff can't tell literal asterisks from
        # genuine bold).
        runs = await _structured_runs(client, s.doc_id, s.primary_tab)
        matching = [run for run in runs if "[rev-probe]" in run["text"]]
        assert matching, "expected a run containing '[rev-probe]' after styling"
        assert all(run["bold"] is True for run in matching)


# ---------------------------------------------------------------------------
# Idempotent re-run — second call is a true no-op, no revision churn
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    async def test_rerun_with_same_style_issues_no_new_revision(self, client, scratch_doc):
        s = scratch_doc
        first = await client.call_tool(
            "format_text",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "find": "[rev-probe]",
                "style": {"bold": True},
            },
        )
        assert first.data["applied"] is True
        assert first.data["style_mutated"] is True
        rev_after_first = first.data["revision_after"]

        second = await client.call_tool(
            "format_text",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "find": "[rev-probe]",
                "style": {"bold": True},
            },
        )
        assert second.data["applied"] is True
        assert second.data["style_mutated"] is False
        assert second.data["revision_before"] == rev_after_first
        assert second.data["revision_after"] == rev_after_first


# ---------------------------------------------------------------------------
# Zero / multi-match — clean refusal, nothing applied
# ---------------------------------------------------------------------------


class TestZeroAndMultiMatch:
    async def test_zero_match_returns_near_miss(self, client, canonical_doc_id):
        r = await client.call_tool(
            "format_text",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.0",
                "find": "The quick brown fox vaulted over the laziest hound.",
                "style": {"bold": True},
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "ZERO_MATCH" in content
        assert "near_miss" in content

    async def test_duplicate_sentence_refused_nothing_applied(self, client, scratch_doc):
        s = scratch_doc
        before = await _read(client, s.doc_id, s.primary_tab)
        assert before.count("lazy dog") >= 2  # sanity: duplicate is present

        r = await client.call_tool(
            "format_text",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "find": DUP_SENTENCE,
                "style": {"bold": True},
            },
            raise_on_error=False,
        )
        assert r.is_error
        assert "MATCH_COUNT_MISMATCH" in _err(r)

        # No partial style leak: the tab is byte-for-byte unchanged.
        after = await _read(client, s.doc_id, s.primary_tab)
        assert after == before


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    async def test_dry_run_predicts_without_writing(self, client, scratch_doc):
        s = scratch_doc
        rev_before = (
            await client.call_tool("read_document", {"doc_id": s.doc_id, "tab_id": s.primary_tab})
        ).data["revision_id"]

        r = await client.call_tool(
            "format_text",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "find": "[rev-probe]",
                "style": {"bold": True},
                "dry_run": True,
            },
        )
        data = r.data
        assert data["applied"] is False
        assert data["dry_run"] is True
        assert data["runs_after"][0][0]["bold"] is True  # predicted overlay

        rev_after = (
            await client.call_tool("read_document", {"doc_id": s.doc_id, "tab_id": s.primary_tab})
        ).data["revision_id"]
        assert rev_after == rev_before


# ---------------------------------------------------------------------------
# Merged-cell table — style lands, merge survives, no structural ops
# ---------------------------------------------------------------------------


class TestMergedCellTable:
    async def test_bold_inside_merged_cell_preserves_the_merge(
        self, client, live_services, scratch_doc
    ):
        from verified_googledocs_mcp.docs import _find_tab_body, fetch_document

        docs, _ = live_services
        s = scratch_doc

        # 1. Create a 2x2 table.
        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertTable": {
                            "rows": 2,
                            "columns": 2,
                            "location": {"index": 1, "tabId": s.primary_tab},
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        doc = fetch_document(docs, s.doc_id)
        body = _find_tab_body(doc, s.primary_tab)
        table_elem = next(el for el in body["content"] if "table" in el)
        table_start = table_elem["startIndex"]

        # 2. Merge row 0's two cells into one (columnSpan=2), before either
        # carries any text — mirrors tests/live/test_tables.py's
        # TestMergedCellProbe request shape exactly.
        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "mergeTableCells": {
                            "tableRange": {
                                "tableCellLocation": {
                                    "tableStartLocation": {
                                        "index": table_start,
                                        "tabId": s.primary_tab,
                                    },
                                    "rowIndex": 0,
                                    "columnIndex": 0,
                                },
                                "rowSpan": 1,
                                "columnSpan": 2,
                            }
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        # 3. Insert text directly into the surviving merged cell's paragraph.
        # replace_table_row would refuse here (it deletes/reinserts, and
        # refuses merged cells by design) — this is exactly the gap #63 fills.
        doc = fetch_document(docs, s.doc_id)
        body = _find_tab_body(doc, s.primary_tab)
        table_elem = next(el for el in body["content"] if "table" in el)
        merged_cell = table_elem["table"]["tableRows"][0]["tableCells"][0]
        assert merged_cell["tableCellStyle"].get("columnSpan") == 2
        cell_para_start = merged_cell["content"][0]["startIndex"]

        docs.documents().batchUpdate(
            documentId=s.doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": cell_para_start, "tabId": s.primary_tab},
                            "text": "CellPhrase",
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        listed = await client.call_tool(
            "list_tables", {"doc_id": s.doc_id, "tab_id": s.primary_tab}
        )
        seeded = listed.data["tables"][0]
        assert seeded["has_merged_cells"] is True
        assert seeded["rows"] == 2
        assert seeded["columns"] == 2

        # 4. The tool under test: bold the cell text via format_text.
        r = await client.call_tool(
            "format_text",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "find": "CellPhrase",
                "style": {"bold": True},
            },
        )
        data = r.data
        assert data["applied"] is True
        assert data["style_mutated"] is True
        assert data["compiled_request_kinds"] == ["updateTextStyle"]

        # 5. The merge and table shape survive; cell text is unchanged.
        relisted = await client.call_tool(
            "list_tables", {"doc_id": s.doc_id, "tab_id": s.primary_tab}
        )
        reseeded = relisted.data["tables"][0]
        assert reseeded["has_merged_cells"] is True
        assert reseeded["rows"] == 2
        assert reseeded["columns"] == 2

        got = await client.call_tool(
            "get_table", {"doc_id": s.doc_id, "tab_id": s.primary_tab, "table_index": 0}
        )
        assert got.data["cells"][0][0] == "CellPhrase"


# ---------------------------------------------------------------------------
# Concurrent-edit smoke test — a comment-driven revision bump doesn't block
# ---------------------------------------------------------------------------


class TestConcurrentEditSmokeTest:
    async def test_comment_driven_revision_bump_does_not_block_format_text(
        self, client, scratch_doc
    ):
        """Grounded in the issue body's own observation ('six consecutive
        range writes killed by comment-driven revision bumps, 2026-08-06') —
        assert the bump actually happens before relying on it, so a future
        API change that stops bumping the revision fails loudly here instead
        of silently testing nothing."""
        s = scratch_doc
        rev_before = (
            await client.call_tool("read_document", {"doc_id": s.doc_id, "tab_id": s.primary_tab})
        ).data["revision_id"]

        added = await client.call_tool(
            "add_anchored_comment",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "quote": "[rev-probe]",
                "body": "bumping the revision",
            },
        )
        assert added.data["applied"] is True

        rev_after_comment = (
            await client.call_tool("read_document", {"doc_id": s.doc_id, "tab_id": s.primary_tab})
        ).data["revision_id"]
        assert rev_after_comment != rev_before, (
            "expected the comment action to bump the Docs revisionId — if this "
            "assertion fails, the premise this test exercises no longer holds"
        )

        r = await client.call_tool(
            "format_text",
            {
                "doc_id": s.doc_id,
                "tab_id": s.primary_tab,
                "find": "[rev-probe]",
                "style": {"bold": True},
            },
        )
        assert r.data["applied"] is True
        assert r.data["match_count"] == 1


# ---------------------------------------------------------------------------
# Input validation + unknown tab
# ---------------------------------------------------------------------------


class TestFormatTextErrors:
    async def test_empty_style_is_invalid_input(self, client, canonical_doc_id):
        r = await client.call_tool(
            "format_text",
            {"doc_id": canonical_doc_id, "tab_id": "t.0", "find": "Curly", "style": {}},
            raise_on_error=False,
        )
        assert r.is_error and "INVALID_INPUT" in _err(r)

    async def test_unknown_tab_is_tab_not_found(self, client, canonical_doc_id):
        r = await client.call_tool(
            "format_text",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.does-not-exist",
                "find": "Curly",
                "style": {"bold": True},
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "TAB_NOT_FOUND" in content
        assert "available_tabs" in content
