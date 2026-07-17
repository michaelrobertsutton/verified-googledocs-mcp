"""Drive API PDF export.

Implements the export_pdf pipeline:

  validate output_path (allowed roots + denylist, reused from
  markdown_mutations) → Drive files.export("application/pdf") → atomic
  write → sha256 / page-count / audit

This is a read/export tool, not a mutation: the document is never written
to, so the returned dict carries no "applied" key (same class as
diff_tab_vs_file). Use it when a caller needs a render-measured page count
to check against a page limit — Docs itself does not expose layout/page
metadata, only PDF export does.

Drive's files.export endpoint refuses documents whose exported PDF would
exceed roughly 10 MB; that surfaces as INVALID_INPUT (Drive's own error,
not a limit this module enforces).

API calls live here; verify.py stays pure.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .markdown_mutations import (
    _ALLOWED_FILE_ROOTS_ENV,
    _allowed_file_roots,
    _is_denylisted_sensitive_path,
)
from .verify import ErrorCode, _make_error, append_audit

# Matches "/Type /Page" object markers but not "/Type /Pages" (the \b word
# boundary fails between "Page" and the trailing "s"). Object-stream-
# compressed PDFs hide these markers inside a compressed stream, so a zero
# count there is expected and must never be reported as a real page count.
_PDF_PAGE_MARKER_RE = re.compile(rb"/Type\s*/Page\b")


# ---------------------------------------------------------------------------
# Path validation (write side; at least as strict as diff_tab_vs_file's read)
# ---------------------------------------------------------------------------


def _resolve_export_pdf_path(output_path: str) -> Path:
    """Validate and resolve *output_path* for export_pdf.

    Reuses _allowed_file_roots() and _is_denylisted_sensitive_path() from
    markdown_mutations rather than duplicating the policy. No implicit
    mkdir: the parent directory must already exist. The target itself may
    or may not exist (export_pdf both creates new files and overwrites
    existing ones), but if it exists it must be a regular file.
    """
    resolved = Path(output_path).expanduser().resolve(strict=False)

    parent = resolved.parent
    if not parent.is_dir():
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"Parent directory does not exist: {parent}",
            {"output_path": output_path, "resolved_path": str(resolved), "parent": str(parent)},
        )

    if _is_denylisted_sensitive_path(resolved):
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            (
                "Output path resolves to a well-known credential/secrets location "
                "and is never allowed by export_pdf, regardless of "
                f"{_ALLOWED_FILE_ROOTS_ENV}."
            ),
            {"output_path": output_path, "resolved_path": str(resolved)},
        )

    allowed_roots = _allowed_file_roots()
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            (
                "Output path is outside the allowed roots for export_pdf. "
                "By default this is the user's home directory — the path is "
                "outside that (or the caller has narrowed it), so set "
                f"{_ALLOWED_FILE_ROOTS_ENV} on the server process to a "
                f"{os.pathsep!r}-separated list of directories to widen it (e.g. in "
                "the MCP server's env config), then restart the server."
            ),
            {
                "output_path": output_path,
                "resolved_path": str(resolved),
                "allowed_roots": [str(root) for root in allowed_roots],
                "env_var": _ALLOWED_FILE_ROOTS_ENV,
            },
        )

    if resolved.exists() and not resolved.is_file():
        raise _make_error(
            ErrorCode.INVALID_INPUT,
            f"Output path exists and is not a regular file: {output_path!r}",
            {"output_path": output_path, "resolved_path": str(resolved)},
        )

    return resolved


# ---------------------------------------------------------------------------
# Download seam (isolated so unit tests can monkeypatch the network call)
# ---------------------------------------------------------------------------


def _translate_export_http_error(exc: Exception, doc_id: str) -> Exception:
    """Translate a googleapiclient HttpError from the export call.

    404 and 403 become a typed VerifyError(INVALID_INPUT); every other
    status (including the ~10MB export-size limit, which Drive reports as
    its own error) propagates unchanged.
    """
    try:
        from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
    except ImportError:
        return exc

    if not isinstance(exc, HttpError):
        return exc

    status = exc.resp.status if exc.resp else 0
    if status == 404:
        return _make_error(
            ErrorCode.INVALID_INPUT,
            f"Document {doc_id!r} not found or not exportable as PDF.",
            {"doc_id": doc_id, "http_status": status, "api_message": str(exc)},
        )
    if status == 403:
        return _make_error(
            ErrorCode.INVALID_INPUT,
            f"Permission denied exporting document {doc_id!r} as PDF.",
            {"doc_id": doc_id, "http_status": status, "api_message": str(exc)},
        )
    return exc


def _download_pdf(drive_service: Any, doc_id: str) -> bytes:
    """Export *doc_id* as a PDF via Drive v3 files.export and return the bytes.

    Downloads through MediaIoBaseDownload (num_retries=3 per chunk) so a
    transient failure mid-transfer retries the chunk rather than the whole
    export. Isolated as a module-level seam so unit tests can monkeypatch
    it without touching the network.
    """
    from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-untyped]

    request = drive_service.files().export_media(fileId=doc_id, mimeType="application/pdf")
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    try:
        while not done:
            _, done = downloader.next_chunk(num_retries=3)
    except Exception as exc:
        translated = _translate_export_http_error(exc, doc_id)
        raise translated from exc

    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def _atomic_write_pdf(final: Path, data: bytes) -> None:
    """Write *data* to *final* atomically via a same-directory temp file + rename."""
    tmp = tempfile.NamedTemporaryFile(dir=str(final.parent), delete=False, suffix=".pdf.tmp")
    try:
        tmp.write(data)
        tmp.close()
        os.replace(tmp.name, final)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Page count (best-effort; never guessed)
# ---------------------------------------------------------------------------


def _count_pdf_pages(data: bytes) -> int | None:
    """Return a best-effort page count, or None if it cannot be determined.

    Counts "/Type /Page" object markers in the raw bytes. Object-stream-
    compressed PDFs (common output of many exporters) hide these markers
    inside a compressed stream, so a zero count is reported as None rather
    than as an unverified page count of zero.
    """
    count = len(_PDF_PAGE_MARKER_RE.findall(data))
    return count if count > 0 else None


# ---------------------------------------------------------------------------
# execute_export_pdf
# ---------------------------------------------------------------------------


def execute_export_pdf(
    *,
    drive_service: Any,
    doc_id: str,
    output_path: str,
) -> dict[str, Any]:
    """Export *doc_id* to a PDF at *output_path*.

    Validates output_path (parent must exist, path must fall inside
    VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS, must not hit the
    credential-path denylist, and an existing target must be a regular
    file), downloads the PDF, writes it atomically, and returns evidence.

    This tool does not mutate the document, so the return dict has no
    "applied" key. Raises VerifyError(INVALID_INPUT) for every validation
    and translated-HttpError case described in _resolve_export_pdf_path and
    _translate_export_http_error; other exceptions propagate unchanged.
    """
    if not output_path.strip():
        raise _make_error(ErrorCode.INVALID_INPUT, "output_path must not be empty")

    final = _resolve_export_pdf_path(output_path)
    existed_before = final.exists()

    data = _download_pdf(drive_service, doc_id)

    _atomic_write_pdf(final, data)

    evidence: dict[str, Any] = {
        "doc_id": doc_id,
        "output_path": str(final),
        "bytes_written": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "page_count": _count_pdf_pages(data),
        "existed_before": existed_before,
        "audit_logged": True,
    }

    audit_ok, audit_reason = append_audit(
        doc=doc_id,
        tab="doc-level",
        tool="export_pdf",
        evidence=evidence,
    )
    evidence["audit_logged"] = audit_ok
    if not audit_ok:
        evidence["audit_log_reason"] = audit_reason

    return evidence
