"""Unit tests for diff_tab_vs_file tool.

Tests:
- Returns a structured diff when tab and file differ.
- Returns identical=True when tab and file are the same.
- Returns TAB_NOT_FOUND when the tab does not exist.
- Returns INVALID_INPUT when the file does not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from verified_googledocs_mcp.server import mcp


@pytest.fixture(autouse=True)
def allow_tmp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _para(text: str, start: int, end: int | None = None) -> dict[str, Any]:
    if end is None:
        end = start + len(text)
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text},
                }
            ],
        },
    }


def _heading_para(level: int, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": f"HEADING_{level}"},
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text + "\n"},
                }
            ],
        },
    }


def _doc_with_content(
    content_elements: list[dict[str, Any]], revision: str = "rev-1"
) -> dict[str, Any]:
    return {
        "documentId": "doc-diff",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": {"content": content_elements}},
                "childTabs": [],
            }
        ],
    }


def _mock_fetch_doc(doc: dict[str, Any]):
    def _fake_get_creds():
        return MagicMock()

    def _fake_build_service(_creds: Any) -> Any:
        return MagicMock()

    def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
        return doc

    return [
        patch("verified_googledocs_mcp.server.get_credentials", _fake_get_creds),
        patch("verified_googledocs_mcp.server.build_docs_service", _fake_build_service),
        patch("verified_googledocs_mcp.server.fetch_document", _fake_fetch),
        patch("verified_googledocs_mcp.markdown_mutations.fetch_document", _fake_fetch),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiffTabVsFile:
    @pytest.mark.asyncio
    async def test_identical_returns_identical_true(self, tmp_path: Path) -> None:
        doc = _doc_with_content(
            [
                _heading_para(1, "Hello", 1, 8),
            ]
        )
        # Export the doc to find what markdown it produces, then match that exactly.
        # The tab will produce "# Hello\n" via to_markdown.
        file = tmp_path / "test.md"
        file.write_text("# Hello\n", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(file),
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["identical"] is True
        assert "hunks" in data
        assert "unified_diff" in data

    @pytest.mark.asyncio
    async def test_different_content_returns_diff(self, tmp_path: Path) -> None:
        doc = _doc_with_content(
            [
                _para("Hello world\n", 1, 13),
            ]
        )
        file = tmp_path / "test.md"
        file.write_text("Hello planet\n", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(file),
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["identical"] is False
        assert len(data["hunks"]) > 0
        # At least one hunk should be replace or delete/insert
        tags = {h["tag"] for h in data["hunks"]}
        assert tags - {"equal"} != set()  # some non-equal hunks

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self, tmp_path: Path) -> None:
        doc = _doc_with_content([_para("Hello\n", 1, 7)])
        file = tmp_path / "test.md"
        file.write_text("content", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "missing-tab",
                        "file_path": str(file),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "TAB_NOT_FOUND" in str(result.content)

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(self, tmp_path: Path) -> None:
        doc = _doc_with_content([_para("Hello\n", 1, 7)])
        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(tmp_path / "nonexistent.md"),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_outside_allowed_root_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(allowed))
        outside = tmp_path / "outside.md"
        outside.write_text("Hello\n", encoding="utf-8")
        doc = _doc_with_content([_para("Hello\n", 1, 7)])

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(outside),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "outside the allowed roots" in str(result.content)

    @pytest.mark.asyncio
    async def test_symlink_escape_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(allowed))
        outside = tmp_path / "outside.md"
        outside.write_text("Hello\n", encoding="utf-8")
        link = allowed / "link.md"
        link.symlink_to(outside)
        doc = _doc_with_content([_para("Hello\n", 1, 7)])

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(link),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "outside the allowed roots" in str(result.content)

    @pytest.mark.asyncio
    async def test_rejection_error_names_the_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test: a rejected path's error must name the exact env
        var to set, not just say "outside the allowed roots" — that's what
        previously cost a round-trip of grepping source to find the fix."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(allowed))
        outside = tmp_path / "outside.md"
        outside.write_text("Hello\n", encoding="utf-8")
        doc = _doc_with_content([_para("Hello\n", 1, 7)])

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(outside),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS" in str(result.content)

    @pytest.mark.asyncio
    async def test_oversized_file_returns_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_MAX_DIFF_FILE_BYTES", "3")
        file = tmp_path / "large.md"
        file.write_text("Hello\n", encoding="utf-8")
        doc = _doc_with_content([_para("Hello\n", 1, 7)])

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(file),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "too large" in str(result.content)

    @pytest.mark.asyncio
    async def test_result_contains_metadata(self, tmp_path: Path) -> None:
        doc = _doc_with_content([_para("Hello\n", 1, 7)])
        file = tmp_path / "test.md"
        file.write_text("Hello\n", encoding="utf-8")

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(file),
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["doc_id"] == "doc-diff"
        assert data["tab_id"] == "tab-1"
        assert data["file_path"] == str(file)
        assert data["revision_id"] == "rev-1"

    @pytest.mark.asyncio
    async def test_diff_is_not_read_only_blocked(self) -> None:
        """diff_tab_vs_file is a READ tool and should not be in MUTATING_TOOLS."""
        from verified_googledocs_mcp.middleware import MUTATING_TOOLS

        assert "diff_tab_vs_file" not in MUTATING_TOOLS


# ---------------------------------------------------------------------------
# Default allowed root: the user's home directory, not the server's cwd.
#
# MCP clients typically register this server pinned to one repo's directory
# (e.g. `--directory /path/to/GoogleDocs-MCP`), while the diff target is
# almost always in whichever *other* project the caller is actually working
# in. Defaulting to cwd made cross-repo diffing fail unless the operator had
# already discovered and set VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS.
# ---------------------------------------------------------------------------


class TestAllowedFileRootsDefault:
    def test_default_is_home_directory_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verified_googledocs_mcp.markdown_mutations import _allowed_file_roots

        monkeypatch.delenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", raising=False)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        roots = _allowed_file_roots()
        assert roots == [fake_home.resolve()]

    @pytest.mark.asyncio
    async def test_file_under_home_but_outside_cwd_is_allowed_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for the cross-repo diff failure: a file that lives
        under the user's home directory — in a different project than
        wherever the server process happens to have been launched from —
        must be readable with zero env var configuration."""
        monkeypatch.delenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", raising=False)
        fake_home = tmp_path / "home"
        other_project = fake_home / "some-other-repo"
        other_project.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        target = other_project / "notes.md"
        target.write_text("Hello\n", encoding="utf-8")
        doc = _doc_with_content([_para("Hello\n", 1, 7)])

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(target),
                    },
                )
        assert not result.is_error
        assert result.data["identical"] is True

    def test_env_var_still_overrides_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting the env var still narrows (or widens) the allowlist —
        the home-directory default only applies when it's unset."""
        from verified_googledocs_mcp.markdown_mutations import _allowed_file_roots

        narrow = tmp_path / "narrow"
        narrow.mkdir()
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(narrow))
        assert _allowed_file_roots() == [narrow.resolve()]


# ---------------------------------------------------------------------------
# Sensitive-path denylist: closes the exposure the home-directory default
# would otherwise open. Applied unconditionally, because the threat model is
# a document's own content tricking an agent into reading credentials
# (prompt injection), not an operator's VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS
# choice.
# ---------------------------------------------------------------------------


class TestSensitivePathDenylist:
    @pytest.mark.parametrize(
        "relative_path",
        [
            ".ssh/id_rsa",
            ".aws/credentials",
            ".gnupg/secring.gpg",
            ".netrc",
            ".git-credentials",
            ".config/gh/hosts.yml",
            ".docker/config.json",
            ".npmrc",
        ],
    )
    def test_denylisted_paths_under_home_are_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_path: str
    ) -> None:
        from verified_googledocs_mcp.markdown_mutations import _is_denylisted_sensitive_path

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        target = fake_home / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret\n", encoding="utf-8")

        assert _is_denylisted_sensitive_path(target.resolve()) is True

    def test_ordinary_project_file_is_not_denylisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verified_googledocs_mcp.markdown_mutations import _is_denylisted_sensitive_path

        fake_home = tmp_path / "home"
        (fake_home / "some-repo").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        target = fake_home / "some-repo" / "notes.md"
        target.write_text("Hello\n", encoding="utf-8")

        assert _is_denylisted_sensitive_path(target.resolve()) is False

    @pytest.mark.asyncio
    async def test_diff_tab_vs_file_rejects_ssh_key_under_home_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: even with the home-directory default allowed root
        (which technically contains ~/.ssh), diff_tab_vs_file must refuse to
        read an SSH private key — the exact prompt-injection scenario this
        denylist exists for ("diff against ~/.ssh/id_rsa")."""
        monkeypatch.delenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", raising=False)
        fake_home = tmp_path / "home"
        ssh_dir = fake_home / ".ssh"
        ssh_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        key = ssh_dir / "id_rsa"
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n", encoding="utf-8")
        doc = _doc_with_content([_para("Hello\n", 1, 7)])

        patchers = _mock_fetch_doc(doc)
        with patchers[0], patchers[1], patchers[2], patchers[3]:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "diff_tab_vs_file",
                    {
                        "doc_id": "doc-diff",
                        "tab_id": "tab-1",
                        "file_path": str(key),
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "credential" in str(result.content).lower()
