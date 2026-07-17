"""export_pdf — real Drive export against the canonical fixture doc.

Read-only from the document's perspective (no mutation); writes only to a
local per-test tmp file. The autouse isolated_audit_dir fixture (see
conftest.py) already points VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS at
the test tmp dir, so no extra env setup is needed here.

Note: export_pdf is registered by a later work unit, so this module only
runs (and only resolves the tool name) once that registration lands; until
then it is skipped like the rest of tests/live/ unless --run-live is passed.
"""

from __future__ import annotations

import hashlib
import os

import pytest

pytestmark = pytest.mark.live


class TestExportPdf:
    async def test_export_writes_valid_pdf_with_matching_evidence(
        self, client, canonical_doc_id, tmp_path
    ):
        target = tmp_path / "export.pdf"
        data = (
            await client.call_tool(
                "export_pdf",
                {"doc_id": canonical_doc_id, "output_path": str(target)},
            )
        ).data

        on_disk = target.read_bytes()
        assert data["bytes_written"] == os.stat(target).st_size
        assert on_disk.startswith(b"%PDF")
        assert data["sha256"] == hashlib.sha256(on_disk).hexdigest()
        assert data["page_count"] is None or data["page_count"] >= 1
        assert "applied" not in data

    async def test_output_path_outside_allowed_roots_is_tool_error(
        self, client, canonical_doc_id, tmp_path
    ):
        outside = tmp_path.parent / f"export-pdf-outside-{tmp_path.name}.pdf"
        result = await client.call_tool(
            "export_pdf",
            {"doc_id": canonical_doc_id, "output_path": str(outside)},
            raise_on_error=False,
        )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)
        assert not outside.exists()
