"""Unit tests for exports.py: execute_export_pdf and its helpers.

All Drive API calls are mocked (or the _download_pdf seam is monkeypatched
directly). No network access, no credentials.

Coverage:
  - happy path: file written, bytes_written/sha256 correct, existed_before False
  - overwrite: existed_before True, content atomically replaced
  - parent directory missing → INVALID_INPUT
  - path outside allowed roots → INVALID_INPUT
  - denylisted sensitive path → INVALID_INPUT
  - target is a directory → INVALID_INPUT
  - page-count regex: 2-page marker bytes → 2; marker-free bytes → None
  - HttpError 404 from the downloader → INVALID_INPUT envelope
  - audit line written on success
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from verified_googledocs_mcp.exports import (
    _count_pdf_pages,
    execute_export_pdf,
)
from verified_googledocs_mcp.verify import ErrorCode, VerifyError

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def allow_tmp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "_state"))


_FAKE_PDF_BYTES = b"%PDF-1.4\n...fake pdf bytes...\n%%EOF"


def _patched_download(data: bytes = _FAKE_PDF_BYTES):
    return patch("verified_googledocs_mcp.exports._download_pdf", return_value=data)


# ---------------------------------------------------------------------------
# Happy path / overwrite
# ---------------------------------------------------------------------------


class TestExecuteExportPdfHappyPath:
    def test_writes_file_and_returns_correct_metadata(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"
        with _patched_download():
            evidence = execute_export_pdf(
                drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
            )

        assert target.read_bytes() == _FAKE_PDF_BYTES
        assert evidence["bytes_written"] == len(_FAKE_PDF_BYTES)
        assert evidence["sha256"] == hashlib.sha256(_FAKE_PDF_BYTES).hexdigest()
        assert evidence["existed_before"] is False
        assert evidence["output_path"] == str(target)
        assert evidence["doc_id"] == "doc-1"

    def test_no_applied_key(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"
        with _patched_download():
            evidence = execute_export_pdf(
                drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
            )
        assert "applied" not in evidence

    def test_return_dict_keys(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"
        with _patched_download():
            evidence = execute_export_pdf(
                drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
            )
        for key in (
            "doc_id",
            "output_path",
            "bytes_written",
            "sha256",
            "page_count",
            "existed_before",
            "audit_logged",
        ):
            assert key in evidence, f"key {key!r} missing"

    def test_overwrite_existing_file_replaces_content_atomically(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"
        target.write_bytes(b"stale content")

        with _patched_download():
            evidence = execute_export_pdf(
                drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
            )

        assert evidence["existed_before"] is True
        assert target.read_bytes() == _FAKE_PDF_BYTES

    def test_relative_path_resolved_against_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with _patched_download():
            evidence = execute_export_pdf(
                drive_service=MagicMock(), doc_id="doc-1", output_path="out.pdf"
            )
        assert evidence["output_path"] == str((tmp_path / "out.pdf").resolve())
        assert (tmp_path / "out.pdf").read_bytes() == _FAKE_PDF_BYTES


# ---------------------------------------------------------------------------
# Path validation failures
# ---------------------------------------------------------------------------


class TestPathValidation:
    def test_empty_output_path_is_invalid_input(self) -> None:
        with pytest.raises(VerifyError) as exc_info:
            execute_export_pdf(drive_service=MagicMock(), doc_id="doc-1", output_path="   ")
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_missing_parent_directory_is_invalid_input(self, tmp_path: Path) -> None:
        target = tmp_path / "does-not-exist" / "out.pdf"
        with pytest.raises(VerifyError) as exc_info:
            execute_export_pdf(drive_service=MagicMock(), doc_id="doc-1", output_path=str(target))
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert "Parent directory" in exc_info.value.envelope.message

    def test_outside_allowed_root_is_invalid_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(allowed))
        outside = tmp_path / "outside.pdf"

        with pytest.raises(VerifyError) as exc_info:
            execute_export_pdf(drive_service=MagicMock(), doc_id="doc-1", output_path=str(outside))
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert "outside the allowed roots" in exc_info.value.envelope.message

    def test_denylisted_sensitive_path_is_invalid_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "home"
        ssh_dir = fake_home / ".ssh"
        ssh_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.delenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", raising=False)

        target = ssh_dir / "export.pdf"
        with pytest.raises(VerifyError) as exc_info:
            execute_export_pdf(drive_service=MagicMock(), doc_id="doc-1", output_path=str(target))
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert "credential" in exc_info.value.envelope.message.lower()

    def test_target_is_directory_is_invalid_input(self, tmp_path: Path) -> None:
        target_dir = tmp_path / "already-a-dir"
        target_dir.mkdir()
        with pytest.raises(VerifyError) as exc_info:
            execute_export_pdf(
                drive_service=MagicMock(), doc_id="doc-1", output_path=str(target_dir)
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert "not a regular file" in exc_info.value.envelope.message

    def test_validation_happens_before_download(self, tmp_path: Path) -> None:
        """A rejected path must never trigger a network call."""
        target_dir = tmp_path / "already-a-dir"
        target_dir.mkdir()
        with patch("verified_googledocs_mcp.exports._download_pdf") as mock_download:
            with pytest.raises(VerifyError):
                execute_export_pdf(
                    drive_service=MagicMock(), doc_id="doc-1", output_path=str(target_dir)
                )
        mock_download.assert_not_called()


# ---------------------------------------------------------------------------
# Page-count regex
# ---------------------------------------------------------------------------


class TestCountPdfPages:
    def test_two_page_uncompressed_pdf_counts_two(self) -> None:
        data = (
            b"%PDF-1.4\n"
            b"1 0 obj << /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >> endobj\n"
            b"2 0 obj << /Type /Page /Parent 1 0 R >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 1 0 R >> endobj\n"
            b"%%EOF"
        )
        assert _count_pdf_pages(data) == 2

    def test_marker_free_bytes_returns_none(self) -> None:
        data = b"%PDF-1.4\n<< totally opaque object-stream compressed content >>\n%%EOF"
        assert _count_pdf_pages(data) is None

    def test_pages_object_alone_does_not_count_as_a_page(self) -> None:
        data = b"1 0 obj << /Type /Pages /Count 0 >> endobj"
        assert _count_pdf_pages(data) is None


# ---------------------------------------------------------------------------
# HttpError translation
# ---------------------------------------------------------------------------


def _http_error(status: int, message: bytes = b"error") -> Exception:
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    resp = Response({"status": status})
    return HttpError(resp, message)


class TestDownloadHttpErrorTranslation:
    def test_404_from_downloader_is_invalid_input(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"

        class _FailingDownloader:
            def __init__(self, _fd: object, _request: object) -> None:
                pass

            def next_chunk(self, num_retries: int = 0) -> tuple[object, bool]:
                raise _http_error(404, b"Not Found")

        with patch("googleapiclient.http.MediaIoBaseDownload", _FailingDownloader):
            with pytest.raises(VerifyError) as exc_info:
                execute_export_pdf(
                    drive_service=MagicMock(), doc_id="doc-missing", output_path=str(target)
                )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT
        assert not target.exists()

    def test_403_from_downloader_is_invalid_input(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"

        class _FailingDownloader:
            def __init__(self, _fd: object, _request: object) -> None:
                pass

            def next_chunk(self, num_retries: int = 0) -> tuple[object, bool]:
                raise _http_error(403, b"Forbidden")

        with patch("googleapiclient.http.MediaIoBaseDownload", _FailingDownloader):
            with pytest.raises(VerifyError) as exc_info:
                execute_export_pdf(
                    drive_service=MagicMock(), doc_id="doc-forbidden", output_path=str(target)
                )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_other_http_status_propagates_unchanged(self, tmp_path: Path) -> None:
        from googleapiclient.errors import HttpError

        target = tmp_path / "out.pdf"

        class _FailingDownloader:
            def __init__(self, _fd: object, _request: object) -> None:
                pass

            def next_chunk(self, num_retries: int = 0) -> tuple[object, bool]:
                raise _http_error(500, b"Server Error")

        with patch("googleapiclient.http.MediaIoBaseDownload", _FailingDownloader):
            with pytest.raises(HttpError):
                execute_export_pdf(
                    drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
                )


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class TestAuditLogging:
    def test_audit_logged_true_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"
        with _patched_download():
            evidence = execute_export_pdf(
                drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
            )
        assert evidence["audit_logged"] is True

    def test_audit_failure_embedded_when_append_fails(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"
        with _patched_download():
            with patch(
                "verified_googledocs_mcp.exports.append_audit",
                return_value=(False, "disk full"),
            ):
                evidence = execute_export_pdf(
                    drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
                )
        assert evidence["audit_logged"] is False
        assert evidence["audit_log_reason"] == "disk full"

    def test_append_audit_called_with_export_pdf_tool_name(self, tmp_path: Path) -> None:
        target = tmp_path / "out.pdf"
        with _patched_download():
            with patch(
                "verified_googledocs_mcp.exports.append_audit",
                return_value=(True, ""),
            ) as mock_append:
                execute_export_pdf(
                    drive_service=MagicMock(), doc_id="doc-1", output_path=str(target)
                )
        _, kwargs = mock_append.call_args
        assert kwargs["tool"] == "export_pdf"
        assert kwargs["doc"] == "doc-1"
        assert kwargs["evidence"]["output_path"] == str(target)
        assert kwargs["evidence"]["bytes_written"] == len(_FAKE_PDF_BYTES)
        assert kwargs["evidence"]["sha256"] == hashlib.sha256(_FAKE_PDF_BYTES).hexdigest()
