"""Errors surfaced through FastMCP are JSON envelopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from tests.unit.fixtures.docs_api import multi_tab_doc
from verified_googledocs_mcp.server import mcp


def _payload(result: Any) -> dict[str, Any]:
    assert result.is_error
    assert result.content
    text = getattr(result.content[0], "text", "")
    data = json.loads(text)
    assert {"error_code", "message", "diagnostics", "retryable"} <= data.keys()
    return data


@pytest.mark.asyncio
async def test_read_error_is_json_envelope() -> None:
    doc = multi_tab_doc()
    with (
        patch("verified_googledocs_mcp.server.get_credentials", return_value=MagicMock()),
        patch("verified_googledocs_mcp.server.fetch_document", return_value=doc),
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "read_document",
                {"doc_id": "doc-multi-tab", "tab_id": "missing"},
                raise_on_error=False,
            )
    assert _payload(result)["error_code"] == "TAB_NOT_FOUND"


@pytest.mark.asyncio
async def test_write_error_is_json_envelope() -> None:
    with (
        patch("verified_googledocs_mcp.server.get_credentials", return_value=MagicMock()),
        patch("verified_googledocs_mcp.server.build_docs_service", return_value=MagicMock()),
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "replace_text",
                {"doc_id": "doc-1", "tab_id": "tab-1", "find": "", "replace": "x"},
                raise_on_error=False,
            )
    assert _payload(result)["error_code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_comment_error_is_json_envelope() -> None:
    with (
        patch("verified_googledocs_mcp.server.get_credentials", return_value=MagicMock()),
        patch("verified_googledocs_mcp.server.build_drive_service", return_value=MagicMock()),
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "reply_to_comment",
                {"doc_id": "doc-1", "comment_id": "c-1", "body": " "},
                raise_on_error=False,
            )
    assert _payload(result)["error_code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_sync_error_is_json_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS", str(tmp_path))
    doc = multi_tab_doc()
    with (
        patch("verified_googledocs_mcp.server.get_credentials", return_value=MagicMock()),
        patch("verified_googledocs_mcp.server.build_docs_service", return_value=MagicMock()),
        patch("verified_googledocs_mcp.markdown_mutations.fetch_document", return_value=doc),
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "diff_tab_vs_file",
                {
                    "doc_id": "doc-multi-tab",
                    "tab_id": "tab-1",
                    "file_path": str(tmp_path / "missing.md"),
                },
                raise_on_error=False,
            )
    assert _payload(result)["error_code"] == "INVALID_INPUT"
