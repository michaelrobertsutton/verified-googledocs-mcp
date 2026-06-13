"""Unit tests for the replace_text tool via the FastMCP in-memory client.

All Google API calls are mocked; no network or credentials required.
The fetch_document mock uses side_effect so consecutive calls return
pre-read (before the write) then post-read (after the write) documents
with different revisionIds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from googledocs_mcp.server import mcp


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _para(text: str, start: int) -> dict[str, Any]:
    end = start + _utf16_len(text)
    return {
        "paragraph": {
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text},
                }
            ]
        }
    }


def _simple_tab_body(text: str) -> dict[str, Any]:
    """Build a minimal tab body with one paragraph."""
    raw = text + "\n"
    return {"content": [_para(raw, 1)]}


def _simple_doc(text: str, revision: str = "rev-1") -> dict[str, Any]:
    """Build a minimal single-tab document."""
    return {
        "documentId": "doc-test",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab One", "index": 0},
                "documentTab": {"body": _simple_tab_body(text)},
                "childTabs": [],
            }
        ],
    }


def _multi_match_doc(texts: list[str], revision: str = "rev-1") -> dict[str, Any]:
    """Document with multiple paragraphs, one per text entry."""
    content = []
    cursor = 1
    for text in texts:
        raw = text + "\n"
        content.append(_para(raw, cursor))
        cursor += _utf16_len(raw)
    return {
        "documentId": "doc-multi",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Tab", "index": 0},
                "documentTab": {"body": {"content": content}},
                "childTabs": [],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_replace(pre_doc: dict[str, Any], post_doc: dict[str, Any]):
    """Return patchers that mock credentials, service build, and fetch calls.

    The batchUpdate mock is included so no real API call is made.
    Tests inspect mock_service.documents().batchUpdate.call_args for assertions.

    fetch_document returns pre_doc on the first call and post_doc on the second.
    """
    mock_service = MagicMock()
    mock_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

    # fetch_document is called twice: once for pre-read, once for post-read.
    fetch_side_effects = [pre_doc, post_doc]

    def _fake_get_credentials():
        return MagicMock()

    def _fake_build_service(_creds: Any) -> Any:
        return mock_service

    fetch_call_count = [0]

    def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
        idx = fetch_call_count[0]
        fetch_call_count[0] += 1
        return fetch_side_effects[idx] if idx < len(fetch_side_effects) else post_doc

    p1 = patch("googledocs_mcp.server.get_credentials", _fake_get_credentials)
    p2 = patch("googledocs_mcp.server.build_docs_service", _fake_build_service)
    p3 = patch("googledocs_mcp.server.fetch_document", _fake_fetch)
    p4 = patch("googledocs_mcp.mutations.fetch_document", _fake_fetch)
    return p1, p2, p3, p4, mock_service


def _mock_replace_simple(text_before: str, text_after: str):
    """Convenience: single-tab doc with different revision IDs."""
    pre = _simple_doc(text_before, revision="rev-1")
    post = _simple_doc(text_after, revision="rev-2")
    return _mock_replace(pre, post)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestReplaceTextHappyPath:
    @pytest.mark.asyncio
    async def test_returns_evidence_keys(self) -> None:
        p1, p2, p3, p4, _ = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        assert not result.is_error
        data = result.data
        for key in ("applied", "match_count", "rung", "before", "after",
                    "revision_before", "revision_after", "audit_logged"):
            assert key in data, f"key {key!r} missing from evidence"

    @pytest.mark.asyncio
    async def test_applied_true(self) -> None:
        p1, p2, p3, p4, _ = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        assert result.data["applied"] is True

    @pytest.mark.asyncio
    async def test_revision_ids_different(self) -> None:
        p1, p2, p3, p4, _ = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        data = result.data
        assert data["revision_before"] == "rev-1"
        assert data["revision_after"] == "rev-2"

    @pytest.mark.asyncio
    async def test_rung_surfaced_in_evidence(self) -> None:
        # Curly quote in document → rung = curly_straight_quotes
        pre = _simple_doc("it’s fine", revision="rev-1")  # right single quotation mark
        post = _simple_doc("it’s fixed", revision="rev-2")
        p1, p2, p3, p4, _ = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "it's fine",  # straight quote needle
                        "replace": "it’s fixed",
                    },
                )
        assert result.data["rung"] == "curly_straight_quotes"

    @pytest.mark.asyncio
    async def test_batchupdate_called_once(self) -> None:
        p1, p2, p3, p4, mock_service = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        assert mock_service.documents().batchUpdate.call_count == 1

    @pytest.mark.asyncio
    async def test_required_revision_id_in_write_control(self) -> None:
        p1, p2, p3, p4, mock_service = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        # Use .return_value chain to get the stable mock (not a fresh call)
        batch_call_args = mock_service.documents.return_value.batchUpdate.call_args
        body = batch_call_args.kwargs.get("body") or (
            batch_call_args[1].get("body") if len(batch_call_args) > 1 else None
        )
        assert body is not None, "batchUpdate was not called with a body argument"
        assert "writeControl" in body
        assert body["writeControl"].get("requiredRevisionId") == "rev-1"

    @pytest.mark.asyncio
    async def test_requests_in_descending_order_for_multiple_matches(self) -> None:
        """For two matches, the second (later) span must appear first in requests."""
        # Two occurrences of "x" in the same paragraph
        pre = _simple_doc("x and x", revision="rev-1")
        post = _simple_doc("y and y", revision="rev-2")
        p1, p2, p3, p4, mock_service = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "x",
                        "replace": "y",
                        "expected_matches": 2,
                    },
                )
        batch_call_args = mock_service.documents.return_value.batchUpdate.call_args
        body = batch_call_args.kwargs.get("body") or (
            batch_call_args[1].get("body") if len(batch_call_args) > 1 else None
        )
        assert body is not None
        requests = body["requests"]
        # Collect all deleteContentRange start indices
        starts = [
            r["deleteContentRange"]["range"]["startIndex"]
            for r in requests
            if "deleteContentRange" in r
        ]
        # Should be in descending order
        assert starts == sorted(starts, reverse=True)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestReplaceTextDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_no_batchupdate(self) -> None:
        p1, p2, p3, p4, mock_service = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                        "dry_run": True,
                    },
                )
        assert mock_service.documents().batchUpdate.call_count == 0
        assert result.data["applied"] is False

    @pytest.mark.asyncio
    async def test_dry_run_revision_after_empty(self) -> None:
        p1, p2, p3, p4, _ = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                        "dry_run": True,
                    },
                )
        assert result.data["revision_after"] == ""


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestReplaceTextErrors:
    @pytest.mark.asyncio
    async def test_empty_find_returns_error(self) -> None:
        pre = _simple_doc("Hello world")
        post = _simple_doc("Hello world")
        p1, p2, p3, p4, _ = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "",
                        "replace": "something",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_find_equals_replace_returns_error(self) -> None:
        pre = _simple_doc("Hello world")
        post = _simple_doc("Hello world")
        p1, p2, p3, p4, _ = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello",
                        "replace": "Hello",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "INVALID_INPUT" in str(result.content)

    @pytest.mark.asyncio
    async def test_zero_match_returns_error_with_near_miss(self) -> None:
        pre = _simple_doc("Hello world")
        post = _simple_doc("Hello world")
        p1, p2, p3, p4, _ = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Helo world",  # near miss
                        "replace": "new text",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        content_str = str(result.content)
        assert "ZERO_MATCH" in content_str

    @pytest.mark.asyncio
    async def test_match_count_mismatch_returns_error(self) -> None:
        # Document has two occurrences of "word" but expected_matches defaults to 1
        pre = _multi_match_doc(["word here", "word there"])
        post = _multi_match_doc(["word here", "word there"])
        p1, p2, p3, p4, _ = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-multi",
                        "tab_id": "tab-1",
                        "find": "word",
                        "replace": "token",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "MATCH_COUNT_MISMATCH" in str(result.content)

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self) -> None:
        pre = _simple_doc("Hello world")
        post = _simple_doc("Hello world")
        p1, p2, p3, p4, _ = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "nonexistent-tab",
                        "find": "Hello",
                        "replace": "Hi",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "TAB_NOT_FOUND" in str(result.content)

    @pytest.mark.asyncio
    async def test_revision_conflict_returns_error(self) -> None:
        """A 409 from batchUpdate must become a REVISION_CONFLICT error."""
        try:
            from googleapiclient.errors import HttpError
        except ImportError:
            pytest.skip("googleapiclient not available")

        mock_resp = MagicMock()
        mock_resp.status = 409
        exc_409 = HttpError(resp=mock_resp, content=b"revision conflict")

        pre = _simple_doc("Hello world")
        post = _simple_doc("Hello planet")

        # Service whose batchUpdate raises 409
        mock_service = MagicMock()
        mock_service.documents.return_value.batchUpdate.return_value.execute.side_effect = exc_409

        fetch_responses = [pre]  # only pre-read; post-read never reached
        fetch_idx = [0]

        def _fake_get_credentials():
            return MagicMock()

        def _fake_build_service(_creds: Any) -> Any:
            return mock_service

        def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
            idx = fetch_idx[0]
            fetch_idx[0] += 1
            return fetch_responses[idx] if idx < len(fetch_responses) else post

        p1 = patch("googledocs_mcp.server.get_credentials", _fake_get_credentials)
        p2 = patch("googledocs_mcp.server.build_docs_service", _fake_build_service)
        p3 = patch("googledocs_mcp.server.fetch_document", _fake_fetch)
        p4 = patch("googledocs_mcp.mutations.fetch_document", _fake_fetch)

        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello",
                        "replace": "Hi",
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "REVISION_CONFLICT" in str(result.content)

    @pytest.mark.asyncio
    async def test_audit_failure_logged_in_evidence(self) -> None:
        """If append_audit raises, evidence carries audit_logged: false."""
        p1, p2, p3, p4, _ = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4, patch(
            "googledocs_mcp.mutations.append_audit",
            return_value=(False, "permission denied"),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        assert not result.is_error
        assert result.data["audit_logged"] is False


# ---------------------------------------------------------------------------
# UTF-16 span correctness with astral character
# ---------------------------------------------------------------------------


class TestReplaceTextUtf16:
    @pytest.mark.asyncio
    async def test_astral_char_before_find_correct_span(self) -> None:
        """Astral emoji before the target text must not offset the replace span."""
        emoji = "\U0001F30D"  # 2 UTF-16 units
        pre = _simple_doc(emoji + "Hello world", revision="rev-1")
        post = _simple_doc(emoji + "Hello planet", revision="rev-2")
        p1, p2, p3, p4, mock_service = _mock_replace(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        assert not result.is_error
        assert result.data["applied"] is True
        # The deleteContentRange startIndex should account for the 2 UTF-16 units of emoji
        batch_call_args = mock_service.documents.return_value.batchUpdate.call_args
        body = batch_call_args.kwargs.get("body") or (
            batch_call_args[1].get("body") if len(batch_call_args) > 1 else None
        )
        assert body is not None
        requests = body["requests"]
        delete_req = next(r for r in requests if "deleteContentRange" in r)
        start_idx = delete_req["deleteContentRange"]["range"]["startIndex"]
        # body starts at 1; emoji = 2 UTF-16 units; "Hello " = 6 chars; "world" starts at 1+2+6=9
        assert start_idx == 9, f"Expected 9 but got {start_idx}"


# ---------------------------------------------------------------------------
# Dry-run predicted diff
# ---------------------------------------------------------------------------


class TestReplaceTextDryRunDiff:
    @pytest.mark.asyncio
    async def test_dry_run_after_shows_predicted_replacement(self) -> None:
        """Dry-run 'after' must show the predicted text, not the unchanged text."""
        p1, p2, p3, p4, mock_service = _mock_replace_simple("Hello world", "unused")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                        "dry_run": True,
                    },
                )
        assert mock_service.documents().batchUpdate.call_count == 0
        assert result.data["applied"] is False
        assert "world" in result.data["before"]
        # The predicted diff must reflect the replacement, and differ from before.
        assert "planet" in result.data["after"]
        assert result.data["after"] != result.data["before"]


# ---------------------------------------------------------------------------
# Audit trail: exactly one line per mutation
# ---------------------------------------------------------------------------


class TestReplaceTextAuditTrail:
    @pytest.mark.asyncio
    async def test_one_audit_line_per_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful replace writes exactly one audit record, with full evidence."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        p1, p2, p3, p4, _ = _mock_replace_simple("Hello world", "Hello planet")
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "replace_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "world",
                        "replace": "planet",
                    },
                )
        assert result.data["audit_logged"] is True
        audit_file = tmp_path / "googledocs-mcp" / "audit.jsonl"
        assert audit_file.exists()
        lines = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected one audit line, got {len(lines)}"
        record = json.loads(lines[0])
        assert record["tool"] == "replace_text"
        assert record["evidence"]["applied"] is True
        assert record["evidence"]["match_count"] == 1
