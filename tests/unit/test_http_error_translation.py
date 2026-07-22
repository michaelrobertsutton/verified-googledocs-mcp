"""Unit tests for _translate_http_error (issue #56 defect 4).

Before this fix, only a 409 or a 400 whose message mentioned "revision" got
wrapped into a typed envelope; every other 400 — e.g. the Docs API's
"Invalid deletion range" response from the issue #56 reproduction — escaped
uncaught as a raw googleapiclient.errors.HttpError. Every 400 that isn't a
revision conflict is now wrapped as a non-retryable INVALID_RANGE, with the
verbatim API message preserved in diagnostics.

Uses real Docs API error message text (from the issue #56 reproduction and
public Docs API error strings), not synthetic ones, per the review action to
test against realistic messages rather than only the exact substrings the
implementation happens to check.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from verified_googledocs_mcp.mutations import _translate_http_error
from verified_googledocs_mcp.verify import ErrorCode, VerifyError

try:
    from googleapiclient.errors import HttpError
except ImportError:  # pragma: no cover
    HttpError = None  # type: ignore[assignment,misc]


def _http_error(status: int, message: str) -> "HttpError":
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=message.encode())


@pytest.mark.skipif(HttpError is None, reason="googleapiclient not available")
class TestRevisionConflictStillTypedFirst:
    """The pre-existing 409 / 400+"revision" -> REVISION_CONFLICT path must
    still take priority over the new, broader 400 handling."""

    def test_409_is_revision_conflict(self) -> None:
        exc = _http_error(409, "The document has been modified.")
        translated = _translate_http_error(exc, "doc-1")
        assert isinstance(translated, VerifyError)
        assert translated.envelope.error_code == ErrorCode.REVISION_CONFLICT
        assert translated.envelope.retryable is True

    def test_400_with_revision_in_message_is_revision_conflict(self) -> None:
        exc = _http_error(400, "Invalid requests[0].updateDocument: revision id mismatch")
        translated = _translate_http_error(exc, "doc-1")
        assert isinstance(translated, VerifyError)
        assert translated.envelope.error_code == ErrorCode.REVISION_CONFLICT


@pytest.mark.skipif(HttpError is None, reason="googleapiclient not available")
class TestOtherFourHundredsBecomeInvalidRange:
    """issue #56: 'Invalid deletion range' from the live reproduction, and
    the other common Docs API index/range rejection messages, must become a
    typed, non-retryable INVALID_RANGE — never a raw HttpError."""

    def test_invalid_deletion_range_from_issue_56_repro(self) -> None:
        # Verbatim from the issue #56 reproduction's replace_table_row failure.
        exc = _http_error(
            400,
            "Invalid requests[2].deleteContentRange: Invalid deletion range. "
            "Cannot delete the requested range.",
        )
        translated = _translate_http_error(exc, "doc-1")
        assert isinstance(translated, VerifyError)
        assert translated.envelope.error_code == ErrorCode.INVALID_RANGE
        assert translated.envelope.retryable is False
        assert "Invalid deletion range" in translated.envelope.diagnostics["api_message"]

    def test_insertion_index_out_of_bounds_message(self) -> None:
        exc = _http_error(
            400,
            "Invalid requests[0].insertText: The insertion index must be inside the "
            "bounds of the existing text.",
        )
        translated = _translate_http_error(exc, "doc-1")
        assert isinstance(translated, VerifyError)
        assert translated.envelope.error_code == ErrorCode.INVALID_RANGE

    def test_start_index_not_less_than_end_index_message(self) -> None:
        exc = _http_error(
            400,
            "Invalid requests[1].deleteContentRange: The range's start index (10) "
            "must be less than its end index (5).",
        )
        translated = _translate_http_error(exc, "doc-1")
        assert isinstance(translated, VerifyError)
        assert translated.envelope.error_code == ErrorCode.INVALID_RANGE

    def test_message_is_preserved_verbatim_in_diagnostics(self) -> None:
        original_message = "Invalid deletion range. Cannot delete the requested range."
        exc = _http_error(400, original_message)
        translated = _translate_http_error(exc, "doc-1")
        assert original_message in translated.envelope.diagnostics["api_message"]
        assert translated.envelope.diagnostics["http_status"] == 400


@pytest.mark.skipif(HttpError is None, reason="googleapiclient not available")
class TestNonBatchUpdateErrorsPropagateUnchanged:
    def test_500_propagates_as_raw_http_error(self) -> None:
        exc = _http_error(500, "Internal error.")
        translated = _translate_http_error(exc, "doc-1")
        assert translated is exc  # unchanged — not wrapped

    def test_non_http_error_propagates_unchanged(self) -> None:
        exc = ValueError("not an HttpError at all")
        translated = _translate_http_error(exc, "doc-1")
        assert translated is exc
