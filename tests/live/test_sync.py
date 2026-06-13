"""§5 Sync — diff_tab_vs_file in both directions.

Read-only: runs against the canonical fixture and local temp files.
"""

from __future__ import annotations


import pytest

pytestmark = pytest.mark.live


def _err(result) -> str:  # type: ignore[no-untyped-def]
    return str(result.content)


async def _tab_markdown(client, doc_id, tab_id) -> str:  # type: ignore[no-untyped-def]
    r = await client.call_tool(
        "read_document", {"doc_id": doc_id, "tab_id": tab_id, "format": "markdown"}
    )
    return r.data["content"]


class TestDiffTabVsFile:
    async def test_identical_when_file_matches_tab(self, client, canonical_doc_id, tmp_path):
        md = await _tab_markdown(client, canonical_doc_id, "t.0")
        f = tmp_path / "same.md"
        f.write_text(md, encoding="utf-8")

        data = (
            await client.call_tool(
                "diff_tab_vs_file",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "file_path": str(f)},
            )
        ).data
        assert data["identical"] is True
        assert all(h["tag"] == "equal" for h in data["hunks"])

    async def test_file_ahead_of_doc_shows_insert(self, client, canonical_doc_id, tmp_path):
        md = await _tab_markdown(client, canonical_doc_id, "t.0")
        f = tmp_path / "file_ahead.md"
        f.write_text(md + "\nExtra line only in the file.\n", encoding="utf-8")

        data = (
            await client.call_tool(
                "diff_tab_vs_file",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "file_path": str(f)},
            )
        ).data
        assert data["identical"] is False
        # File has content the doc lacks → an insert hunk carrying that line.
        inserts = [h for h in data["hunks"] if h["tag"] in ("insert", "replace")]
        assert inserts
        assert any("Extra line only in the file." in "".join(h["file_lines"]) for h in inserts)

    async def test_doc_ahead_of_file_shows_delete(self, client, canonical_doc_id, tmp_path):
        md = await _tab_markdown(client, canonical_doc_id, "t.0")
        lines = md.splitlines(keepends=True)
        # Drop a distinctive line so the doc is "ahead" of the file.
        kept = [ln for ln in lines if "Soft hyphen" not in ln]
        assert len(kept) < len(lines)
        f = tmp_path / "doc_ahead.md"
        f.write_text("".join(kept), encoding="utf-8")

        data = (
            await client.call_tool(
                "diff_tab_vs_file",
                {"doc_id": canonical_doc_id, "tab_id": "t.0", "file_path": str(f)},
            )
        ).data
        assert data["identical"] is False
        # Doc has a line the file lacks → a delete hunk carrying it.
        deletes = [h for h in data["hunks"] if h["tag"] in ("delete", "replace")]
        assert deletes
        assert any("Soft hyphen" in "".join(h["tab_lines"]) for h in deletes)

    async def test_missing_file_is_invalid_input(self, client, canonical_doc_id, tmp_path):
        missing = tmp_path / "does_not_exist.md"
        r = await client.call_tool(
            "diff_tab_vs_file",
            {"doc_id": canonical_doc_id, "tab_id": "t.0", "file_path": str(missing)},
            raise_on_error=False,
        )
        assert r.is_error and "INVALID_INPUT" in _err(r)

    async def test_unknown_tab_is_tab_not_found(self, client, canonical_doc_id, tmp_path):
        f = tmp_path / "x.md"
        f.write_text("hello\n", encoding="utf-8")
        r = await client.call_tool(
            "diff_tab_vs_file",
            {"doc_id": canonical_doc_id, "tab_id": "t.nope", "file_path": str(f)},
            raise_on_error=False,
        )
        assert r.is_error and "TAB_NOT_FOUND" in _err(r)
