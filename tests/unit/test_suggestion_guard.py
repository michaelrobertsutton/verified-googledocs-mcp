"""Unit tests for the issue #56 pre-write suggestion guard.

Root cause: every mutation pipeline pre-reads with
suggestionsViewMode=PREVIEW_WITHOUT_SUGGESTIONS (needed so a pending
suggestion can't defeat the locator's match-count guard from issue #28), but
batchUpdate always mutates the document's real index space, which is
SUGGESTIONS_INLINE. When the target tab has a pending suggested insertion or
deletion, those two index spaces diverge by the net length of the suggested
content, so every index a pipeline computes from the PREVIEW read is wrong —
the write lands at the wrong offset (often mid-word) and corrupts the
document. assert_no_pending_suggestions (suggestions.py) is the pre-write
safety valve: refuse instead of corrupting.

Style-only suggestions (text/paragraph style changes) do not move indices
and must be allowed through. Detection is pinned to the revision the caller's
own PREVIEW pre-read captured, since a separate SUGGESTIONS_INLINE get could
otherwise race a suggestion added in between the two reads.

No network calls, no credentials — all docs are synthetic fixtures.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.unit.fixtures.suggestions.docs_suggestions import (
    doc_with_deletion,
    doc_with_insertion,
    doc_with_no_suggestions,
    doc_with_para_style_suggestion,
    doc_with_style_suggestion,
    doc_with_suggestion_in_other_tab,
)
from verified_googledocs_mcp.suggestions import assert_no_pending_suggestions
from verified_googledocs_mcp.verify import ErrorCode, VerifyError


def _service_returning(*docs: dict[str, Any]) -> MagicMock:
    """A MagicMock docs service whose get().execute() yields *docs* in order,
    repeating the last one for any calls beyond len(docs)."""
    service = MagicMock()
    calls = {"n": 0}

    def _execute(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        idx = min(calls["n"], len(docs) - 1)
        calls["n"] += 1
        return docs[idx]

    service.documents.return_value.get.return_value.execute.side_effect = _execute
    return service


# ---------------------------------------------------------------------------
# Index-affecting suggestions block the write
# ---------------------------------------------------------------------------


class TestIndexAffectingSuggestionsBlock:
    def test_insertion_raises_suggestions_present(self) -> None:
        doc = doc_with_insertion()
        service = _service_returning(doc)
        with pytest.raises(VerifyError) as exc_info:
            assert_no_pending_suggestions(
                service=service,
                doc_id=doc["documentId"],
                tab_id="tab-a",
                expected_revision=doc["revisionId"],
            )
        envelope = exc_info.value.envelope
        assert envelope.error_code == ErrorCode.SUGGESTIONS_PRESENT
        assert envelope.retryable is False
        assert envelope.diagnostics["suggestion_count"] == 1
        assert "ins-001" in envelope.diagnostics["suggestion_ids"]

    def test_deletion_raises_suggestions_present(self) -> None:
        doc = doc_with_deletion()
        service = _service_returning(doc)
        with pytest.raises(VerifyError) as exc_info:
            assert_no_pending_suggestions(
                service=service,
                doc_id=doc["documentId"],
                tab_id="tab-a",
                expected_revision=doc["revisionId"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.SUGGESTIONS_PRESENT


# ---------------------------------------------------------------------------
# Style-only suggestions do not move indices — must be allowed through
# ---------------------------------------------------------------------------


class TestStyleOnlySuggestionsAllowedThrough:
    def test_text_style_suggestion_does_not_raise(self) -> None:
        doc = doc_with_style_suggestion()
        service = _service_returning(doc)
        assert_no_pending_suggestions(
            service=service,
            doc_id=doc["documentId"],
            tab_id="tab-a",
            expected_revision=doc["revisionId"],
        )  # must not raise

    def test_paragraph_style_suggestion_does_not_raise(self) -> None:
        doc = doc_with_para_style_suggestion()
        service = _service_returning(doc)
        assert_no_pending_suggestions(
            service=service,
            doc_id=doc["documentId"],
            tab_id="tab-a",
            expected_revision=doc["revisionId"],
        )  # must not raise


# ---------------------------------------------------------------------------
# No suggestions at all — the common case, must be a no-op
# ---------------------------------------------------------------------------


class TestNoSuggestions:
    def test_clean_doc_does_not_raise(self) -> None:
        doc = doc_with_no_suggestions()
        service = _service_returning(doc)
        assert_no_pending_suggestions(
            service=service,
            doc_id=doc["documentId"],
            tab_id="tab-a",
            expected_revision=doc["revisionId"],
        )  # must not raise


# ---------------------------------------------------------------------------
# Tab-scoped: a suggestion in a DIFFERENT tab must not block this tab
# ---------------------------------------------------------------------------


class TestOtherTabNotBlocked:
    def test_suggestion_in_a_different_tab_does_not_raise(self) -> None:
        doc = doc_with_suggestion_in_other_tab()
        service = _service_returning(doc)
        # tab-a is clean even though tab-b has a pending insertion.
        assert_no_pending_suggestions(
            service=service,
            doc_id=doc["documentId"],
            tab_id="tab-a",
            expected_revision=doc["revisionId"],
        )  # must not raise

    def test_suggestion_in_the_target_tab_still_raises(self) -> None:
        doc = doc_with_suggestion_in_other_tab()
        service = _service_returning(doc)
        with pytest.raises(VerifyError) as exc_info:
            assert_no_pending_suggestions(
                service=service,
                doc_id=doc["documentId"],
                tab_id="tab-b",
                expected_revision=doc["revisionId"],
            )
        assert exc_info.value.envelope.error_code == ErrorCode.SUGGESTIONS_PRESENT


# ---------------------------------------------------------------------------
# Revision race: the INLINE read must be pinned to the caller's own
# PREVIEW-read revision, not just "whatever is current"
# ---------------------------------------------------------------------------


class TestRevisionRace:
    def test_retries_once_then_succeeds_on_matching_revision(self) -> None:
        fresh = doc_with_no_suggestions()
        stale = {**fresh, "revisionId": "rev-old"}
        service = _service_returning(stale, fresh)
        # First get() returns a stale revision; the retry returns the
        # matching one. Must not raise.
        assert_no_pending_suggestions(
            service=service,
            doc_id=fresh["documentId"],
            tab_id="tab-a",
            expected_revision=fresh["revisionId"],
        )

    def test_persistent_mismatch_defers_to_required_revision_id_check(self) -> None:
        # A revision mismatch that survives the retry means the document is
        # being edited concurrently, right now, by someone else — a
        # different situation from "a suggestion sitting at a stable
        # revision" (this guard's actual job). The mutation pipeline's own
        # upcoming batchUpdate call pins requiredRevisionId=expected_revision
        # and will reject the write with a real REVISION_CONFLICT if the
        # revision has genuinely moved on, independent of this guard. So on
        # a persistent mismatch, the check is silently skipped here rather
        # than raising a second, less-specific error that would only shadow
        # that more accurate signal.
        stale = doc_with_no_suggestions()
        service = _service_returning(stale, stale)  # never matches
        assert_no_pending_suggestions(
            service=service,
            doc_id=stale["documentId"],
            tab_id="tab-a",
            expected_revision="rev-current-does-not-match",
        )  # must not raise
