"""Unit tests for comments.py: Drive API operations and comment-state evidence.

All Drive API calls are mocked.  No network access, no credentials.

Coverage:
  - assemble_comment_state_evidence keys and values
  - resolve: re-queries and returns resolved=True on success
  - resolve: COMMENT_STILL_OPEN when comment stays open after resolve
  - reply: happy path returns evidence with applied=True
  - add_anchored_comment: QUOTE_NOT_FOUND with candidates when quote absent
  - add_anchored_comment: happy path when quote present
  - get_comment_thread: returns full reply chain
  - execute_reply_to_comment: INVALID_INPUT on empty body
  - format_comment: structured correctly
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from verified_googledocs_mcp.comments import (
    _format_comment,
    assemble_comment_state_evidence,
    execute_add_anchored_comment,
    execute_reply_to_comment,
    execute_resolve_comment,
    get_comment_thread,
)
from verified_googledocs_mcp.verify import ErrorCode, VerifyError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _raw_comment(
    comment_id: str = "c-001",
    resolved: bool = False,
    content: str = "Nice section",
    quoted: str = "the quick brown fox",
    replies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": comment_id,
        "content": content,
        "resolved": resolved,
        "quotedFileContent": {"mimeType": "text/plain", "value": quoted},
        "author": {"displayName": "Alice"},
        "createdTime": "2026-01-01T00:00:00Z",
        "modifiedTime": "2026-01-01T00:00:00Z",
        "replies": replies or [],
    }


def _make_drive_service(
    *,
    comment: dict[str, Any] | None = None,
    resolve_side_effect: Exception | None = None,
    re_query_comment: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a mock Drive service.

    comment: the raw comment returned by comments.get / comments.create.
    re_query_comment: if provided, comments.get returns comment first then re_query_comment.
    resolve_side_effect: exception raised by replies.create.
    """
    svc = MagicMock()

    # comments.get: first call → comment; optional second call → re_query_comment
    if re_query_comment is not None:
        svc.comments.return_value.get.return_value.execute.side_effect = [
            comment,
            re_query_comment,
        ]
    elif comment is not None:
        svc.comments.return_value.get.return_value.execute.return_value = comment

    # comments.create
    if comment is not None:
        svc.comments.return_value.create.return_value.execute.return_value = comment

    # replies.create
    if resolve_side_effect is not None:
        svc.replies.return_value.create.return_value.execute.side_effect = resolve_side_effect
    else:
        svc.replies.return_value.create.return_value.execute.return_value = {
            "id": "r-resolve-001",
            "action": "resolve",
        }

    return svc


def _simple_tab_doc(text: str = "the quick brown fox jumps") -> dict[str, Any]:
    """Minimal single-tab Docs API document for locate() testing."""
    raw = text + "\n"
    end = len(raw)  # simple ASCII, no astral
    return {
        "documentId": "doc-1",
        "revisionId": "rev-1",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 1 + end,
                                            "textRun": {"content": raw},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }


# ---------------------------------------------------------------------------
# assemble_comment_state_evidence
# ---------------------------------------------------------------------------


class TestAssembleCommentStateEvidence:
    def test_keys_present(self) -> None:
        raw = _raw_comment(comment_id="c-1", resolved=True)
        comment = _format_comment(raw)
        ev = assemble_comment_state_evidence(comment=comment, applied=True, audit_logged=True)
        for key in (
            "applied",
            "comment_id",
            "resolved",
            "reply_count",
            "content",
            "quoted_text",
            "author",
            "audit_logged",
        ):
            assert key in ev, f"key {key!r} missing"

    def test_resolved_true_when_comment_resolved(self) -> None:
        raw = _raw_comment(resolved=True)
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=True, audit_logged=True
        )
        assert ev["resolved"] is True

    def test_resolved_false_when_comment_open(self) -> None:
        raw = _raw_comment(resolved=False)
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=True, audit_logged=True
        )
        assert ev["resolved"] is False

    def test_applied_false(self) -> None:
        raw = _raw_comment()
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=False, audit_logged=False
        )
        assert ev["applied"] is False

    def test_reply_count_zero_when_no_replies(self) -> None:
        raw = _raw_comment(replies=[])
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=True, audit_logged=True
        )
        assert ev["reply_count"] == 0

    def test_reply_count_matches_replies(self) -> None:
        replies = [{"id": "r1", "content": "ok", "action": "", "author": {}, "createdTime": ""}]
        raw = _raw_comment(replies=replies)
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=True, audit_logged=True
        )
        assert ev["reply_count"] == 1

    def test_audit_log_reason_absent_when_empty(self) -> None:
        raw = _raw_comment()
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=True, audit_logged=True, audit_log_reason=""
        )
        assert "audit_log_reason" not in ev

    def test_audit_log_reason_present_when_set(self) -> None:
        raw = _raw_comment()
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw),
            applied=True,
            audit_logged=False,
            audit_log_reason="disk full",
        )
        assert ev["audit_log_reason"] == "disk full"

    def test_quoted_text_propagated(self) -> None:
        raw = _raw_comment(quoted="important phrase")
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=True, audit_logged=True
        )
        assert ev["quoted_text"] == "important phrase"

    def test_comment_id_propagated(self) -> None:
        raw = _raw_comment(comment_id="c-xyz")
        ev = assemble_comment_state_evidence(
            comment=_format_comment(raw), applied=True, audit_logged=True
        )
        assert ev["comment_id"] == "c-xyz"


# ---------------------------------------------------------------------------
# execute_resolve_comment
# ---------------------------------------------------------------------------


class TestResolveComment:
    def test_happy_path_returns_resolved_true(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        resolved_raw = _raw_comment(comment_id="c-1", resolved=True)
        drive_svc = _make_drive_service(
            comment=resolved_raw,
            re_query_comment=resolved_raw,
        )
        evidence = execute_resolve_comment(
            drive_service=drive_svc,
            doc_id="doc-1",
            comment_id="c-1",
        )
        assert evidence["applied"] is True
        assert evidence["resolved"] is True

    def test_happy_path_evidence_keys(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        resolved_raw = _raw_comment(resolved=True)
        drive_svc = _make_drive_service(comment=resolved_raw, re_query_comment=resolved_raw)
        evidence = execute_resolve_comment(
            drive_service=drive_svc, doc_id="doc-1", comment_id="c-1"
        )
        for key in ("applied", "comment_id", "resolved", "reply_count", "audit_logged"):
            assert key in evidence, f"key {key!r} missing"

    def test_comment_still_open_raises_verify_error(self) -> None:
        """A comment that remains unresolved must raise VerifyError(COMMENT_STILL_OPEN)."""
        open_raw = _raw_comment(resolved=False)
        drive_svc = _make_drive_service(comment=open_raw, re_query_comment=open_raw)
        with pytest.raises(VerifyError) as exc_info:
            execute_resolve_comment(drive_service=drive_svc, doc_id="doc-1", comment_id="c-1")
        assert exc_info.value.envelope.error_code == ErrorCode.COMMENT_STILL_OPEN

    def test_comment_still_open_not_retryable(self) -> None:
        open_raw = _raw_comment(resolved=False)
        drive_svc = _make_drive_service(comment=open_raw, re_query_comment=open_raw)
        with pytest.raises(VerifyError) as exc_info:
            execute_resolve_comment(drive_service=drive_svc, doc_id="doc-1", comment_id="c-1")
        assert not exc_info.value.envelope.retryable

    def test_post_state_in_diagnostics_when_still_open(self) -> None:
        open_raw = _raw_comment(resolved=False, comment_id="c-bad")
        drive_svc = _make_drive_service(comment=open_raw, re_query_comment=open_raw)
        with pytest.raises(VerifyError) as exc_info:
            execute_resolve_comment(drive_service=drive_svc, doc_id="doc-1", comment_id="c-bad")
        diag = exc_info.value.envelope.diagnostics
        assert "post_state" in diag

    def test_audit_logged_true_on_success(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        resolved_raw = _raw_comment(resolved=True)
        drive_svc = _make_drive_service(comment=resolved_raw, re_query_comment=resolved_raw)
        evidence = execute_resolve_comment(
            drive_service=drive_svc, doc_id="doc-1", comment_id="c-1"
        )
        assert evidence["audit_logged"] is True

    def test_audit_failure_embedded_when_append_fails(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        resolved_raw = _raw_comment(resolved=True)
        drive_svc = _make_drive_service(comment=resolved_raw, re_query_comment=resolved_raw)
        with patch(
            "verified_googledocs_mcp.comments.append_audit",
            return_value=(False, "disk full"),
        ):
            evidence = execute_resolve_comment(
                drive_service=drive_svc, doc_id="doc-1", comment_id="c-1"
            )
        assert evidence["audit_logged"] is False
        assert "audit_log_reason" in evidence


# ---------------------------------------------------------------------------
# execute_reply_to_comment
# ---------------------------------------------------------------------------


class TestReplyToComment:
    def _make_drive_with_reply(self, comment: dict[str, Any]) -> MagicMock:
        svc = MagicMock()
        # replies.create returns a reply
        svc.replies.return_value.create.return_value.execute.return_value = {
            "id": "r-1",
            "content": "Thanks",
            "action": "",
        }
        # comments.get returns the updated comment (now with 1 reply)
        svc.comments.return_value.get.return_value.execute.return_value = comment
        return svc

    def test_happy_path_evidence_keys(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        replies = [
            {"id": "r-1", "content": "Thanks", "action": "", "author": {}, "createdTime": ""}
        ]
        comment_raw = _raw_comment(replies=replies)
        drive_svc = self._make_drive_with_reply(comment_raw)
        evidence = execute_reply_to_comment(
            drive_service=drive_svc, doc_id="doc-1", comment_id="c-1", body="Thanks"
        )
        for key in ("applied", "comment_id", "resolved", "reply_count", "audit_logged"):
            assert key in evidence

    def test_happy_path_applied_true(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        replies = [{"id": "r-1", "content": "ok", "action": "", "author": {}, "createdTime": ""}]
        comment_raw = _raw_comment(replies=replies)
        drive_svc = self._make_drive_with_reply(comment_raw)
        evidence = execute_reply_to_comment(
            drive_service=drive_svc, doc_id="doc-1", comment_id="c-1", body="ok"
        )
        assert evidence["applied"] is True

    def test_reply_count_incremented(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        replies = [
            {"id": "r-1", "content": "first", "action": "", "author": {}, "createdTime": ""},
            {"id": "r-2", "content": "second", "action": "", "author": {}, "createdTime": ""},
        ]
        comment_raw = _raw_comment(replies=replies)
        drive_svc = self._make_drive_with_reply(comment_raw)
        evidence = execute_reply_to_comment(
            drive_service=drive_svc, doc_id="doc-1", comment_id="c-1", body="second"
        )
        assert evidence["reply_count"] == 2

    def test_empty_body_raises_invalid_input(self) -> None:
        drive_svc = MagicMock()
        with pytest.raises(VerifyError) as exc_info:
            execute_reply_to_comment(
                drive_service=drive_svc, doc_id="doc-1", comment_id="c-1", body="   "
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------------------
# execute_add_anchored_comment
# ---------------------------------------------------------------------------


class TestAddAnchoredComment:
    def _mock_docs(self, doc: dict[str, Any]) -> Any:
        svc = MagicMock()
        svc.documents.return_value.get.return_value.execute.return_value = doc
        return svc

    def _mock_drive(self, comment_raw: dict[str, Any]) -> Any:
        svc = MagicMock()
        svc.comments.return_value.create.return_value.execute.return_value = comment_raw
        return svc

    def test_quote_not_found_raises_verify_error(self) -> None:
        doc = _simple_tab_doc("the quick brown fox jumps")
        docs_svc = self._mock_docs(doc)
        drive_svc = self._mock_drive(_raw_comment())

        with patch("verified_googledocs_mcp.comments.fetch_document", return_value=doc):
            with pytest.raises(VerifyError) as exc_info:
                execute_add_anchored_comment(
                    drive_service=drive_svc,
                    docs_service=docs_svc,
                    doc_id="doc-1",
                    tab_id="tab-1",
                    quote="completely absent phrase",
                    body="my comment",
                )
        assert exc_info.value.envelope.error_code == ErrorCode.QUOTE_NOT_FOUND

    def test_quote_not_found_diagnostics_contain_candidates(self) -> None:
        doc = _simple_tab_doc("the quick brown fox jumps")
        docs_svc = self._mock_docs(doc)
        drive_svc = self._mock_drive(_raw_comment())

        with patch("verified_googledocs_mcp.comments.fetch_document", return_value=doc):
            with pytest.raises(VerifyError) as exc_info:
                execute_add_anchored_comment(
                    drive_service=drive_svc,
                    docs_service=docs_svc,
                    doc_id="doc-1",
                    tab_id="tab-1",
                    quote="quikc borwn fox",  # misspelled — near miss
                    body="my comment",
                )
        diag = exc_info.value.envelope.diagnostics
        assert "candidates" in diag

    def test_happy_path_returns_evidence(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        doc = _simple_tab_doc("the quick brown fox jumps")
        docs_svc = self._mock_docs(doc)
        comment_raw = _raw_comment(comment_id="c-new", quoted="the quick brown fox")
        drive_svc = self._mock_drive(comment_raw)

        with patch("verified_googledocs_mcp.comments.fetch_document", return_value=doc):
            evidence = execute_add_anchored_comment(
                drive_service=drive_svc,
                docs_service=docs_svc,
                doc_id="doc-1",
                tab_id="tab-1",
                quote="the quick brown fox",
                body="Looks good",
            )
        assert evidence["applied"] is True
        for key in ("applied", "comment_id", "resolved", "reply_count", "audit_logged"):
            assert key in evidence

    def test_empty_body_raises_invalid_input(self) -> None:
        drive_svc = MagicMock()
        docs_svc = MagicMock()
        with pytest.raises(VerifyError) as exc_info:
            execute_add_anchored_comment(
                drive_service=drive_svc,
                docs_service=docs_svc,
                doc_id="doc-1",
                tab_id="tab-1",
                quote="something",
                body="",
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_empty_quote_raises_invalid_input(self) -> None:
        drive_svc = MagicMock()
        docs_svc = MagicMock()
        with pytest.raises(VerifyError) as exc_info:
            execute_add_anchored_comment(
                drive_service=drive_svc,
                docs_service=docs_svc,
                doc_id="doc-1",
                tab_id="tab-1",
                quote="   ",
                body="body",
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_tab_not_found_raises_verify_error(self) -> None:
        doc = _simple_tab_doc("hello world")
        docs_svc = self._mock_docs(doc)
        drive_svc = self._mock_drive(_raw_comment())

        with patch("verified_googledocs_mcp.comments.fetch_document", return_value=doc):
            with pytest.raises(VerifyError) as exc_info:
                execute_add_anchored_comment(
                    drive_service=drive_svc,
                    docs_service=docs_svc,
                    doc_id="doc-1",
                    tab_id="nonexistent-tab",
                    quote="hello",
                    body="comment",
                )
        assert exc_info.value.envelope.error_code == ErrorCode.TAB_NOT_FOUND


# ---------------------------------------------------------------------------
# get_comment_thread
# ---------------------------------------------------------------------------


class TestGetCommentThread:
    def test_returns_formatted_comment(self) -> None:
        replies = [{"id": "r-1", "content": "Sure", "action": "", "author": {}, "createdTime": ""}]
        raw = _raw_comment(comment_id="c-99", replies=replies)
        drive_svc = MagicMock()
        drive_svc.comments.return_value.get.return_value.execute.return_value = raw

        result = get_comment_thread(drive_svc, "doc-1", "c-99")
        assert result["comment_id"] == "c-99"
        assert len(result["replies"]) == 1
        assert result["replies"][0]["reply_id"] == "r-1"

    def test_reply_chain_content(self) -> None:
        replies = [
            {"id": "r-a", "content": "First reply", "action": "", "author": {}, "createdTime": ""},
            {"id": "r-b", "content": "Second reply", "action": "", "author": {}, "createdTime": ""},
        ]
        raw = _raw_comment(replies=replies)
        drive_svc = MagicMock()
        drive_svc.comments.return_value.get.return_value.execute.return_value = raw

        result = get_comment_thread(drive_svc, "doc-1", "c-1")
        contents = [r["content"] for r in result["replies"]]
        assert "First reply" in contents
        assert "Second reply" in contents

    def test_scope_is_document(self) -> None:
        raw = _raw_comment()
        drive_svc = MagicMock()
        drive_svc.comments.return_value.get.return_value.execute.return_value = raw

        result = get_comment_thread(drive_svc, "doc-1", "c-1")
        assert result["scope"] == "document"


# ---------------------------------------------------------------------------
# _format_comment
# ---------------------------------------------------------------------------


class TestFormatComment:
    def test_comment_id_mapped(self) -> None:
        raw = _raw_comment(comment_id="c-42")
        result = _format_comment(raw)
        assert result["comment_id"] == "c-42"

    def test_resolved_false_by_default(self) -> None:
        raw = _raw_comment(resolved=False)
        result = _format_comment(raw)
        assert result["resolved"] is False

    def test_reply_count_zero(self) -> None:
        raw = _raw_comment(replies=[])
        result = _format_comment(raw)
        assert result["reply_count"] == 0

    def test_quoted_text_extracted(self) -> None:
        raw = _raw_comment(quoted="some quoted passage")
        result = _format_comment(raw)
        assert result["quoted_text"] == "some quoted passage"

    def test_scope_always_document(self) -> None:
        raw = _raw_comment()
        result = _format_comment(raw)
        assert result["scope"] == "document"

    def test_author_display_name(self) -> None:
        raw = _raw_comment()  # author is Alice
        result = _format_comment(raw)
        assert result["author"] == "Alice"
