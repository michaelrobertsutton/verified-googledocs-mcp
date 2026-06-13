"""§2 Verified text edits — replace_text against the live API.

Covers the normalization ladder (all four rungs), the match-count guard, tab
scoping, UTF-16 hazards, dry-run, the revision precondition, evidence shape,
and the error codes that originate in replace_text / locate
(ZERO_MATCH, MATCH_COUNT_MISMATCH, STRUCTURAL_BOUNDARY, REVISION_CONFLICT,
INVALID_INPUT, TAB_NOT_FOUND).

Mutating cases run against a fresh disposable copy; read-only / dry-run cases
run against the canonical fixture.
"""

from __future__ import annotations

from unittest.mock import patch

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


def _unescape(md: str) -> str:
    """Drop markdown backslash-escapes so literal substring checks work.

    read_document renders e.g. "[rev-probe]" as "\\[rev\\-probe\\]"; the raw
    text is recovered by removing the escaping backslashes.
    """
    return md.replace("\\", "")


# ---------------------------------------------------------------------------
# Normalization ladder — each rung reported correctly
# ---------------------------------------------------------------------------


class TestNormalizationLadder:
    async def test_exact_rung(self, client, scratch_doc):
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "[rev-probe]",
                "replace": "[rev-applied]",
            },
        )
        assert r.data["applied"] is True
        assert r.data["rung"] == "exact"
        assert r.data["match_count"] == 1

    async def test_curly_straight_quote_rung(self, client, scratch_doc):
        # Document has curly “ ” quotes; we search with straight " ".
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": '"Hello, world!"',
                "replace": '"Hi, world!"',
            },
        )
        assert r.data["applied"] is True
        assert r.data["rung"] == "curly_straight_quotes"

    async def test_nbsp_whitespace_rung(self, client, scratch_doc):
        # "before<NBSP>after" matched with a normal space.
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "before after",
                "replace": "before-and-after",
            },
        )
        assert r.data["applied"] is True
        assert r.data["rung"] == "nbsp_whitespace_runs"

    async def test_soft_hyphen_rung(self, client, scratch_doc):
        # "super<SOFT HYPHEN>seded" matched with plain "superseded".
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "superseded",
                "replace": "replaced",
            },
        )
        assert r.data["applied"] is True
        assert r.data["rung"] == "soft_hyphen_strip"


# ---------------------------------------------------------------------------
# Zero match → ZERO_MATCH with near-miss
# ---------------------------------------------------------------------------


class TestZeroMatch:
    async def test_zero_match_returns_near_miss(self, client, canonical_doc_id):
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.0",
                "find": "The quick brown fox vaulted over the laziest hound.",
                "replace": "x",
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "ZERO_MATCH" in content
        assert "near_miss" in content  # nearest near-miss span included


# ---------------------------------------------------------------------------
# Match-count guard → MATCH_COUNT_MISMATCH, no edit, all locations
# ---------------------------------------------------------------------------


class TestMatchCountGuard:
    async def test_duplicate_sentence_refused_on_copy(self, client, scratch_doc):
        """The seeded duplicate sentence appears twice → guard refuses the edit."""
        before = await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)
        assert before.count("lazy dog") >= 2  # sanity: duplicate is present

        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": DUP_SENTENCE,
                "replace": "REPLACED",
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "MATCH_COUNT_MISMATCH" in content
        assert "spans" in content  # all locations returned

        # No edit was made — re-read confirms the duplicate is intact.
        after = await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)
        assert after.count("lazy dog") == before.count("lazy dog")
        assert "REPLACED" not in after

    async def test_duplicate_sentence_refused_on_canonical(self, client, canonical_doc_id):
        # With #28 fixed (fetch_document pins PREVIEW_WITHOUT_SUGGESTIONS) the
        # canonical doc's duplicate resolves to 2 matches and the guard fires.
        # dry_run keeps the canonical fixture unmutated regardless of outcome.
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.0",
                "find": DUP_SENTENCE,
                "replace": "REPLACED",
                "dry_run": True,
            },
            raise_on_error=False,
        )
        assert r.is_error and "MATCH_COUNT_MISMATCH" in _err(r)


# ---------------------------------------------------------------------------
# Tab scoping — edit one tab, the other is untouched
# ---------------------------------------------------------------------------


class TestTabScoping:
    async def test_edit_scoped_to_one_tab(self, client, scratch_doc):
        # "Hazards" exists in both tabs ("Text Hazards" / "Unicode Hazards").
        other_tab = scratch_doc.tab_ids[1]
        before_other = await _read(client, scratch_doc.doc_id, other_tab)
        assert "Unicode Hazards" in before_other

        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "Hazards",
                "replace": "Perils",
            },
        )
        assert r.data["applied"] is True
        assert r.data["match_count"] == 1

        # Edited tab changed; the other tab is byte-for-byte untouched.
        edited = await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab)
        assert "Perils" in edited
        after_other = await _read(client, scratch_doc.doc_id, other_tab)
        assert after_other == before_other
        assert "Unicode Hazards" in after_other


# ---------------------------------------------------------------------------
# UTF-16 hazards — edit positioned after astral / ZWJ / combining / RTL text
# ---------------------------------------------------------------------------


class TestUtf16Hazards:
    async def test_edit_after_hazards_lands_on_intended_text(self, client, scratch_doc):
        unicode_tab = scratch_doc.tab_ids[1]
        # "(Hebrew shalom)" sits after the emoji, ZWJ sequence, combining marks,
        # and the RTL Hebrew word — all width-≠-1 in UTF-16.
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": unicode_tab,
                "find": "(Hebrew shalom)",
                "replace": "(shalom greeting)",
            },
        )
        assert r.data["applied"] is True
        assert r.data["match_count"] == 1

        after = await _read(client, scratch_doc.doc_id, unicode_tab)
        # The edit landed on the intended text...
        assert "(shalom greeting)" in after
        assert "(Hebrew shalom)" not in after
        # ...and the hazardous characters before it are intact, not shifted/corrupted.
        assert "🎉" in after
        assert "👨‍👩‍👧" in after
        assert "שָׁלוֹם" in after


# ---------------------------------------------------------------------------
# Dry run — predicted diff, no change, revision unchanged
# ---------------------------------------------------------------------------


class TestDryRun:
    async def test_dry_run_predicts_without_writing(self, client, scratch_doc):
        rev_before = (
            await client.call_tool(
                "read_document",
                {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab},
            )
        ).data["revision_id"]

        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "[rev-probe]",
                "replace": "[changed]",
                "dry_run": True,
            },
        )
        data = r.data
        assert data["applied"] is False
        # Predicted "after" reflects the replacement...
        assert "[changed]" in data["after"]

        # ...but nothing was written: revision is unchanged and the text remains.
        rev_after = (
            await client.call_tool(
                "read_document",
                {"doc_id": scratch_doc.doc_id, "tab_id": scratch_doc.primary_tab},
            )
        ).data["revision_id"]
        assert rev_after == rev_before
        current = _unescape(await _read(client, scratch_doc.doc_id, scratch_doc.primary_tab))
        assert "[rev-probe]" in current and "[changed]" not in current


# ---------------------------------------------------------------------------
# Revision precondition → REVISION_CONFLICT
# ---------------------------------------------------------------------------


class TestRevisionConflict:
    async def test_stale_required_revision_rejected_by_api(
        self, client, scratch_doc, live_services
    ):
        """A concurrent edit between read and write makes the API reject the write.

        Simulated by feeding replace_text a pre-read whose revisionId is stale
        (an out-of-band edit has since moved the head). The 400/revision
        rejection comes from the *real* Docs API and maps to REVISION_CONFLICT.
        """
        from verified_googledocs_mcp.docs import fetch_document

        docs, _ = live_services
        # Capture a pre-edit snapshot (its revisionId is about to go stale).
        stale_doc = fetch_document(docs, scratch_doc.doc_id)

        # Out-of-band edit moves the document head past stale_doc's revision.
        docs.documents().batchUpdate(
            documentId=scratch_doc.doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1, "tabId": scratch_doc.primary_tab},
                            "text": "x",
                        }
                    }
                ]
            },
        ).execute(num_retries=3)

        # replace_text now pre-reads the stale snapshot → requiredRevisionId is stale.
        with patch(
            "verified_googledocs_mcp.mutations.fetch_document",
            lambda service, doc_id: stale_doc,
        ):
            r = await client.call_tool(
                "replace_text",
                {
                    "doc_id": scratch_doc.doc_id,
                    "tab_id": scratch_doc.primary_tab,
                    "find": "[rev-probe]",
                    "replace": "[conflict]",
                },
                raise_on_error=False,
            )
        assert r.is_error
        assert "REVISION_CONFLICT" in _err(r)


# ---------------------------------------------------------------------------
# Evidence shape — server re-reads, revision delta, match_count + rung, audit
# ---------------------------------------------------------------------------


class TestEvidenceShape:
    async def test_success_evidence_is_a_server_reread(self, client, scratch_doc):
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "[rev-probe]",
                "replace": "[evidence-check]",
            },
        )
        d = r.data
        assert d["applied"] is True
        # before/after are document re-reads, not echoes of the arguments.
        assert "[rev-probe]" in d["before"]
        assert "[evidence-check]" in d["after"]
        assert "[rev-probe]" not in d["after"]
        # revision advanced.
        assert d["revision_before"] and d["revision_after"]
        assert d["revision_before"] != d["revision_after"]
        # match_count and rung present; mutation recorded.
        assert d["match_count"] == 1
        assert d["rung"] == "exact"
        assert d["audit_logged"] is True


# ---------------------------------------------------------------------------
# Input validation + unknown tab + structural boundary
# ---------------------------------------------------------------------------


class TestReplaceTextErrors:
    async def test_empty_find_is_invalid_input(self, client, canonical_doc_id):
        r = await client.call_tool(
            "replace_text",
            {"doc_id": canonical_doc_id, "tab_id": "t.0", "find": "", "replace": "x"},
            raise_on_error=False,
        )
        assert r.is_error and "INVALID_INPUT" in _err(r)

    async def test_find_equals_replace_is_invalid_input(self, client, canonical_doc_id):
        r = await client.call_tool(
            "replace_text",
            {"doc_id": canonical_doc_id, "tab_id": "t.0", "find": "Curly", "replace": "Curly"},
            raise_on_error=False,
        )
        assert r.is_error and "INVALID_INPUT" in _err(r)

    async def test_unknown_tab_is_tab_not_found(self, client, canonical_doc_id):
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.does-not-exist",
                "find": "Curly",
                "replace": "x",
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "TAB_NOT_FOUND" in content
        assert "available_tabs" in content  # available tabs listed

    async def test_match_crossing_paragraph_boundary_is_structural_boundary(
        self, client, scratch_doc
    ):
        # "dog.\nThe quick" spans the boundary between two paragraphs.
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "dog.\nThe quick",
                "replace": "dog. The quick",
            },
            raise_on_error=False,
        )
        assert r.is_error and "STRUCTURAL_BOUNDARY" in _err(r)
