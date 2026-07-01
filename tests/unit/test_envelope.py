"""Tests for the error envelope — ErrorCode enum, ErrorEnvelope dataclass, VerifyError."""

import dataclasses

import pytest

from verified_googledocs_mcp.verify import (
    ErrorCode,
    ErrorEnvelope,
    VerifyError,
    _RETRYABLE_CODES,
    _make_error,
)


class TestErrorCodeCoverage:
    """All enumerated codes must exist."""

    EXPECTED_CODES = {
        "ZERO_MATCH",
        "MATCH_COUNT_MISMATCH",
        "REVISION_CONFLICT",
        "VERIFICATION_FAILED",
        "STALE_RANGE",
        "TAB_NOT_FOUND",
        "STRUCTURAL_BOUNDARY",
        "UNSUPPORTED_MARKDOWN",
        "QUOTE_NOT_FOUND",
        "COMMENT_STILL_OPEN",
        "INVALID_INPUT",
        "IMAGE_SOURCE_UNSUPPORTED",
        "AUTH_EXPIRED",
        "INDEX_SIMULATION_FAILED",
    }

    def test_all_codes_exist(self):
        actual = {m.value for m in ErrorCode}
        assert actual == self.EXPECTED_CODES

    def test_enum_count(self):
        assert len(ErrorCode) == 14


class TestRetryablePolicy:
    """Retryable set is consistent with the design contract."""

    def test_retryable_codes_are_transient(self):
        retryable = {c for c in ErrorCode if c in _RETRYABLE_CODES}
        assert retryable == {
            ErrorCode.REVISION_CONFLICT,
            ErrorCode.STALE_RANGE,
            ErrorCode.AUTH_EXPIRED,
        }

    def test_non_retryable_codes(self):
        non_retryable = {c for c in ErrorCode if c not in _RETRYABLE_CODES}
        assert ErrorCode.INVALID_INPUT in non_retryable
        assert ErrorCode.ZERO_MATCH in non_retryable
        assert ErrorCode.STRUCTURAL_BOUNDARY in non_retryable
        assert ErrorCode.MATCH_COUNT_MISMATCH in non_retryable


class TestErrorEnvelopeShape:
    """to_dict() must contain all four required keys."""

    def test_to_dict_keys(self):
        env = ErrorEnvelope(
            error_code=ErrorCode.ZERO_MATCH,
            message="not found",
            diagnostics={"ladder_report": [], "near_miss": None},
            retryable=False,
        )
        d = env.to_dict()
        assert set(d.keys()) == {"error_code", "message", "diagnostics", "retryable"}
        assert d["error_code"] == "ZERO_MATCH"
        assert d["retryable"] is False

    def test_envelope_is_frozen(self):
        env = ErrorEnvelope(
            error_code=ErrorCode.INVALID_INPUT,
            message="bad",
            diagnostics={},
            retryable=False,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.message = "modified"  # type: ignore[misc]

    def test_envelope_shape_per_code(self):
        """Every code can be wrapped in a well-formed envelope."""
        for code in ErrorCode:
            env = ErrorEnvelope(
                error_code=code,
                message=f"test for {code.value}",
                diagnostics={"code": code.value},
                retryable=code in _RETRYABLE_CODES,
            )
            d = env.to_dict()
            assert d["error_code"] == code.value
            assert d["retryable"] == (code in _RETRYABLE_CODES)


class TestVerifyError:
    """VerifyError carries the envelope and reports retryable correctly."""

    def test_verify_error_carries_envelope(self):
        err = _make_error(ErrorCode.INVALID_INPUT, "empty needle", {"detail": "x"})
        assert isinstance(err, VerifyError)
        assert err.envelope.error_code == ErrorCode.INVALID_INPUT
        assert err.envelope.retryable is False
        assert err.envelope.diagnostics["detail"] == "x"

    def test_verify_error_message(self):
        err = _make_error(ErrorCode.AUTH_EXPIRED, "token gone")
        assert str(err) == "token gone"

    def test_verify_error_retryable(self):
        for code in (ErrorCode.REVISION_CONFLICT, ErrorCode.STALE_RANGE, ErrorCode.AUTH_EXPIRED):
            err = _make_error(code, "transient")
            assert err.envelope.retryable is True

    def test_verify_error_non_retryable(self):
        for code in (
            ErrorCode.INVALID_INPUT,
            ErrorCode.ZERO_MATCH,
            ErrorCode.MATCH_COUNT_MISMATCH,
        ):
            err = _make_error(code, "permanent")
            assert err.envelope.retryable is False
