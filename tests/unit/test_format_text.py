"""Unit tests for the format_text tool via the FastMCP in-memory client.

All Google API calls are mocked; no network or credentials required. Mirrors
tests/unit/test_replace_text.py's harness (fetch_document via side_effect for
pre-read then post-read), with formatting.py as the patch target instead of
mutations.py. Style-runtime-validation edge cases (non-dict style, non-bool
values) are exercised by calling execute_format_text directly instead of
through the client — FastMCP's own schema layer may reject some malformed
JSON-RPC payloads before they ever reach our Python validation, so calling
the pure function directly is the reliable way to pin OUR validation logic.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client

from tests.unit.fixtures.evidence import assert_top_level_evidence
from tests.unit.fixtures.tables import build_table, doc_with_content
from verified_googledocs_mcp.formatting import execute_format_text
from verified_googledocs_mcp.server import mcp
from verified_googledocs_mcp.verify import ErrorCode, VerifyError


@contextmanager
def _patch_fetch_and_guard(fake_fetch: Any):  # type: ignore[no-untyped-def]
    """Combine the formatting.fetch_document patch with a no-op suggestion guard.

    Mirrors test_replace_text.py's _patch_fetch_and_guard, targeting
    formatting.py (format_text's pipeline module) instead of mutations.py.
    """
    with (
        patch("verified_googledocs_mcp.formatting.fetch_document", fake_fetch),
        patch(
            "verified_googledocs_mcp.formatting.assert_no_pending_suggestions",
            lambda **kwargs: None,
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _run_para(run_specs: list[tuple[str, dict[str, Any] | None]], start: int) -> dict[str, Any]:
    """A NORMAL_TEXT paragraph built from one or more textRuns.

    ``run_specs`` is ``[(text, style_or_None), ...]``; concatenated, the runs
    form the paragraph's full raw text (the last entry should carry the
    terminating "\\n", mirroring the real Docs API). Sets startIndex/endIndex
    on BOTH the outer structural element and each inline element — unlike
    test_replace_text.py's simpler ``_para``, format_text's pipeline calls
    ``_tab_extent`` (for the index simulator pre-flight), which reads the
    outer structural element's own indices.
    """
    elements = []
    cursor = start
    for text, style in run_specs:
        end = cursor + _utf16_len(text)
        text_run: dict[str, Any] = {"content": text}
        if style:
            text_run["textStyle"] = style
        elements.append({"startIndex": cursor, "endIndex": end, "textRun": text_run})
        cursor = end
    return {
        "startIndex": start,
        "endIndex": cursor,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": elements,
        },
    }


def _doc_from_runs(
    run_specs: list[tuple[str, dict[str, Any] | None]],
    revision: str = "rev-1",
    doc_id: str = "doc-test",
    tab_id: str = "tab-1",
) -> dict[str, Any]:
    """Wrap a single multi-run paragraph into a minimal single-tab document."""
    para = _run_para(run_specs, 1)
    return {
        "documentId": doc_id,
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": "Tab One", "index": 0},
                "documentTab": {"body": {"content": [para]}},
                "childTabs": [],
            }
        ],
    }


def _simple_doc(
    text: str,
    style: dict[str, Any] | None = None,
    revision: str = "rev-1",
    doc_id: str = "doc-test",
    tab_id: str = "tab-1",
) -> dict[str, Any]:
    """Single-paragraph, single-run document, optionally pre-styled."""
    return _doc_from_runs([(text + "\n", style)], revision=revision, doc_id=doc_id, tab_id=tab_id)


def _multi_match_doc(texts: list[str], revision: str = "rev-1") -> dict[str, Any]:
    """Document with multiple paragraphs, one per text entry."""
    content = []
    cursor = 1
    for text in texts:
        para = _run_para([(text + "\n", None)], cursor)
        content.append(para)
        cursor = para["endIndex"]
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


def _mock_format(pre_doc: dict[str, Any], post_doc: dict[str, Any]):
    """Return patchers that mock credentials, service build, and fetch calls.

    fetch_document returns pre_doc on the first call and post_doc on the
    second (unused on the no-op or dry-run paths, which never re-read).
    """
    mock_service = MagicMock()
    mock_service.documents.return_value.batchUpdate.return_value.execute.return_value = {}

    fetch_side_effects = [pre_doc, post_doc]
    fetch_call_count = [0]

    def _fake_get_credentials() -> Any:
        return MagicMock()

    def _fake_build_service(_creds: Any) -> Any:
        return mock_service

    def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
        idx = fetch_call_count[0]
        fetch_call_count[0] += 1
        return fetch_side_effects[idx] if idx < len(fetch_side_effects) else post_doc

    p1 = patch("verified_googledocs_mcp.server.get_credentials", _fake_get_credentials)
    p2 = patch("verified_googledocs_mcp.server.build_docs_service", _fake_build_service)
    p3 = patch("verified_googledocs_mcp.server.fetch_document", _fake_fetch)
    p4 = _patch_fetch_and_guard(_fake_fetch)
    return p1, p2, p3, p4, mock_service


def _batch_body(mock_service: MagicMock) -> dict[str, Any]:
    call_args = mock_service.documents.return_value.batchUpdate.call_args
    body = call_args.kwargs.get("body") or (
        call_args[1].get("body") if len(call_args) > 1 else None
    )
    assert body is not None, "batchUpdate was not called with a body argument"
    return body


def _error_payload(result: Any) -> dict[str, Any]:
    assert result.is_error
    assert result.content
    return json.loads(getattr(result.content[0], "text", ""))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestFormatTextHappyPath:
    @pytest.mark.asyncio
    async def test_bold_applied(self) -> None:
        pre = _simple_doc("Hello world", revision="rev-1")
        post = _simple_doc("Hello world", style={"bold": True}, revision="rev-2")
        p1, p2, p3, p4, mock_service = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        assert data["style_mutated"] is True
        assert data["content_mutated"] is False
        assert data["compiled_request_kinds"] == ["updateTextStyle"]
        assert data["revision_before"] == "rev-1"
        assert data["revision_after"] == "rev-2"
        assert data["runs_before"][0][0]["bold"] is False
        assert data["runs_after"][0][0]["bold"] is True
        assert mock_service.documents().batchUpdate.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_evidence_keys(self) -> None:
        pre = _simple_doc("Hello world", revision="rev-1")
        post = _simple_doc("Hello world", style={"bold": True}, revision="rev-2")
        p1, p2, p3, p4, _ = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        assert_top_level_evidence(result)
        for key in (
            "applied",
            "dry_run",
            "match_count",
            "rung",
            "style",
            "spans",
            "runs_before",
            "runs_after",
            "content_mutated",
            "style_mutated",
            "compiled_request_kinds",
            "revision_before",
            "revision_after",
            "audit_logged",
        ):
            assert key in result.data, f"key {key!r} missing from evidence"

    @pytest.mark.asyncio
    async def test_unbold_carries_false_in_fields_mask(self) -> None:
        """bold: false must actually clear bold, not be filtered out (the trap
        the plan calls out: tables.py's truthy_fields pattern would silently
        drop this)."""
        pre = _simple_doc("Hello world", style={"bold": True}, revision="rev-1")
        post = _simple_doc("Hello world", style=None, revision="rev-2")
        p1, p2, p3, p4, mock_service = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": False},
                    },
                )
        assert not result.is_error
        assert result.data["applied"] is True
        assert result.data["style_mutated"] is True
        body = _batch_body(mock_service)
        requests = body["requests"]
        assert len(requests) == 1
        style_req = requests[0]["updateTextStyle"]
        assert style_req["textStyle"] == {"bold": False}
        assert style_req["fields"] == "bold"

    @pytest.mark.asyncio
    async def test_idempotent_rerun_issues_no_batchupdate(self) -> None:
        """Re-running with a style that already matches must not write at all."""
        pre = _simple_doc("Hello world", style={"bold": True}, revision="rev-1")
        post = pre  # unused: the no-op path never re-reads
        p1, p2, p3, p4, mock_service = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        assert data["style_mutated"] is False
        assert data["compiled_request_kinds"] == []
        assert data["revision_before"] == "rev-1"
        assert data["revision_after"] == "rev-1"
        assert mock_service.documents().batchUpdate.call_count == 0

    @pytest.mark.asyncio
    async def test_find_spanning_two_runs_surfaces_prior_boundaries(self) -> None:
        """A find straddling an unstyled run and an already-bold run must
        apply across the whole span and report both runs' prior style."""
        pre = _doc_from_runs([("Hello ", None), ("World\n", {"bold": True})], revision="rev-1")
        post = _doc_from_runs([("Hello World\n", {"bold": True})], revision="rev-2")
        p1, p2, p3, p4, _ = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello World",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        assert data["style_mutated"] is True
        runs_before = data["runs_before"][0]
        assert len(runs_before) == 2
        assert {r["bold"] for r in runs_before} == {False, True}
        assert all(r["bold"] is True for r in data["runs_after"][0])

    @pytest.mark.asyncio
    async def test_table_cell_with_merged_cells_no_structural_ops(self) -> None:
        """Styling text inside a merged-cell table must compile ONLY
        updateTextStyle — no delete/insert/merge request."""
        table_elem, _ = build_table(1, [["CellPhrase"]], merges={(0, 0): {"columnSpan": 2}})
        pre = doc_with_content([table_elem], doc_id="doc-table", tab_id="tab-1", revision="rev-1")
        post_table_elem, _ = build_table(
            1, [["CellPhrase"]], styles={(0, 0): {"bold": True}}, merges={(0, 0): {"columnSpan": 2}}
        )
        post = doc_with_content(
            [post_table_elem], doc_id="doc-table", tab_id="tab-1", revision="rev-2"
        )
        p1, p2, p3, p4, mock_service = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-table",
                        "tab_id": "tab-1",
                        "find": "CellPhrase",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        assert result.data["applied"] is True
        assert result.data["compiled_request_kinds"] == ["updateTextStyle"]
        body = _batch_body(mock_service)
        for req in body["requests"]:
            assert set(req.keys()) == {"updateTextStyle"}


# ---------------------------------------------------------------------------
# style_mutated regression — must not be derived from a positional
# before/after run pairing (see verify.py's assemble_format_text_evidence)
# ---------------------------------------------------------------------------


class TestFormatTextStyleMutatedRegression:
    @pytest.mark.asyncio
    async def test_style_mutated_true_when_post_write_runs_merge(self) -> None:
        """A real write can change how many runs cover the target span: here
        a bold "He" run and an unstyled "llo world" run pre-write become a
        single merged "Hello" run (bold) plus a separate " world" run
        post-write — 2 runs before, 1 after. An earlier implementation
        derived style_mutated from zip(runs_before, runs_after), which
        silently truncates to the shorter list and would have paired only
        ("He", bold=True) against ("Hello", bold=True) — missing the "llo"
        run's real bold=False -> True change and reporting style_mutated:
        false alongside a revision that did change. style_mutated must come
        from whether runs_before already satisfied the requested style, not
        from comparing differently-sized run lists.
        """
        pre = _doc_from_runs([("He", {"bold": True}), ("llo world\n", None)], revision="rev-1")
        post = _doc_from_runs([("Hello", {"bold": True}), (" world\n", None)], revision="rev-2")
        p1, p2, p3, p4, mock_service = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        data = result.data
        assert data["applied"] is True
        # The mismatched run counts are the point of this test.
        assert len(data["runs_before"][0]) == 2
        assert len(data["runs_after"][0]) == 1
        assert mock_service.documents().batchUpdate.call_count == 1
        assert data["style_mutated"] is True


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestFormatTextDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_no_batchupdate_and_predicted_runs_after(self) -> None:
        pre = _simple_doc("Hello world", revision="rev-1")
        post = pre  # unused: dry_run never re-reads
        p1, p2, p3, p4, mock_service = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                        "dry_run": True,
                    },
                )
        assert not result.is_error
        data = result.data
        assert mock_service.documents().batchUpdate.call_count == 0
        assert data["applied"] is False
        assert data["dry_run"] is True
        assert data["revision_after"] == ""
        assert data["runs_before"][0][0]["bold"] is False
        assert data["runs_after"][0][0]["bold"] is True  # predicted overlay

    @pytest.mark.asyncio
    async def test_dry_run_follows_replace_text_audit_contract(self) -> None:
        """format_text audits dry runs (tagged audit_log_reason='dry_run'),
        matching replace_text's contract — not the table/markdown writers',
        which never call append_audit on a dry run."""
        pre = _simple_doc("Hello world", revision="rev-1")
        p1, p2, p3, p4, _ = _mock_format(pre, pre)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                        "dry_run": True,
                    },
                )
        assert not result.is_error
        assert result.data["audit_logged"] is True
        assert result.data["audit_log_reason"] == "dry_run"

    @pytest.mark.asyncio
    async def test_dry_run_audit_failure_logged_in_evidence(self) -> None:
        pre = _simple_doc("Hello world", revision="rev-1")
        p1, p2, p3, p4, _ = _mock_format(pre, pre)
        with (
            p1,
            p2,
            p3,
            p4,
            patch(
                "verified_googledocs_mcp.formatting.append_audit",
                return_value=(False, "permission denied"),
            ),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                        "dry_run": True,
                    },
                )
        assert not result.is_error
        assert result.data["audit_logged"] is False
        assert result.data["audit_log_reason"] == "permission denied"


# ---------------------------------------------------------------------------
# Locate errors
# ---------------------------------------------------------------------------


class TestFormatTextLocateErrors:
    @pytest.mark.asyncio
    async def test_zero_match_returns_error_nothing_applied(self) -> None:
        pre = _simple_doc("Hello world")
        p1, p2, p3, p4, mock_service = _mock_format(pre, pre)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "nonexistent phrase",
                        "style": {"bold": True},
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "ZERO_MATCH" in str(result.content)
        assert mock_service.documents().batchUpdate.call_count == 0

    @pytest.mark.asyncio
    async def test_match_count_mismatch_returns_error_nothing_applied(self) -> None:
        pre = _multi_match_doc(["word here", "word there"])
        p1, p2, p3, p4, mock_service = _mock_format(pre, pre)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-multi",
                        "tab_id": "tab-1",
                        "find": "word",
                        "style": {"bold": True},
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "MATCH_COUNT_MISMATCH" in str(result.content)
        assert mock_service.documents().batchUpdate.call_count == 0

    @pytest.mark.asyncio
    async def test_tab_not_found_returns_error(self) -> None:
        pre = _simple_doc("Hello world")
        p1, p2, p3, p4, _ = _mock_format(pre, pre)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "nonexistent-tab",
                        "find": "Hello",
                        "style": {"bold": True},
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
        mock_service = MagicMock()
        mock_service.documents.return_value.batchUpdate.return_value.execute.side_effect = exc_409

        def _fake_get_credentials() -> Any:
            return MagicMock()

        def _fake_build_service(_creds: Any) -> Any:
            return mock_service

        def _fake_fetch(_service: Any, _doc_id: str) -> dict[str, Any]:
            return pre  # only the pre-read is ever reached

        p1 = patch("verified_googledocs_mcp.server.get_credentials", _fake_get_credentials)
        p2 = patch("verified_googledocs_mcp.server.build_docs_service", _fake_build_service)
        p3 = patch("verified_googledocs_mcp.server.fetch_document", _fake_fetch)
        p4 = _patch_fetch_and_guard(_fake_fetch)

        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                    raise_on_error=False,
                )
        assert result.is_error
        assert "REVISION_CONFLICT" in str(result.content)


# ---------------------------------------------------------------------------
# Post-write verification
# ---------------------------------------------------------------------------


class TestFormatTextPostWriteVerification:
    @pytest.mark.asyncio
    async def test_style_not_confirmed_surfaces_verification_failed(self) -> None:
        """The write is sent and the API accepts it, but the post-read shows
        the style did not land — must surface VERIFICATION_FAILED, not a bare
        is_error, and applied must be false."""
        pre = _simple_doc("Hello world", revision="rev-1")
        post = _simple_doc("Hello world", style=None, revision="rev-2")  # unchanged
        p1, p2, p3, p4, _ = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                    raise_on_error=False,
                )
        payload = _error_payload(result)
        assert payload["error_code"] == "VERIFICATION_FAILED"
        assert payload["diagnostics"]["evidence"]["applied"] is False

    @pytest.mark.asyncio
    async def test_post_locate_failure_wrapped_not_leaked(self) -> None:
        """If the post-read can no longer locate `find` at all (e.g. a
        concurrent edit), the top-level error_code must be VERIFICATION_FAILED
        — a raw ZERO_MATCH escaping here would invite a dangerous retry."""
        pre = _simple_doc("Hello world", revision="rev-1")
        post = _simple_doc("Completely different text", revision="rev-2")
        p1, p2, p3, p4, _ = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                    raise_on_error=False,
                )
        payload = _error_payload(result)
        assert payload["error_code"] == "VERIFICATION_FAILED"
        # The original locate failure is preserved for debugging, but only in
        # diagnostics — not as the top-level code a naive caller might retry on.
        assert payload["diagnostics"]["post_locate_error"]["error_code"] == "ZERO_MATCH"


# ---------------------------------------------------------------------------
# Style validation (direct calls — see module docstring)
# ---------------------------------------------------------------------------


class TestFormatTextStyleValidation:
    def test_non_dict_style_raises_invalid_input(self) -> None:
        with pytest.raises(VerifyError) as exc_info:
            execute_format_text(
                service=MagicMock(),
                doc_id="doc-test",
                tab_id="tab-1",
                find="x",
                style="bold",  # type: ignore[arg-type]
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_empty_style_raises_invalid_input(self) -> None:
        with pytest.raises(VerifyError) as exc_info:
            execute_format_text(
                service=MagicMock(), doc_id="doc-test", tab_id="tab-1", find="x", style={}
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_unknown_style_key_raises_invalid_input(self) -> None:
        with pytest.raises(VerifyError) as exc_info:
            execute_format_text(
                service=MagicMock(),
                doc_id="doc-test",
                tab_id="tab-1",
                find="x",
                style={"strikethrough": True},
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    @pytest.mark.parametrize("bad_value", [1, 0, "true", None])
    def test_non_bool_style_value_raises_invalid_input(self, bad_value: Any) -> None:
        with pytest.raises(VerifyError) as exc_info:
            execute_format_text(
                service=MagicMock(),
                doc_id="doc-test",
                tab_id="tab-1",
                find="x",
                style={"bold": bad_value},
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT

    def test_empty_find_raises_invalid_input(self) -> None:
        with pytest.raises(VerifyError) as exc_info:
            execute_format_text(
                service=MagicMock(),
                doc_id="doc-test",
                tab_id="tab-1",
                find="",
                style={"bold": True},
            )
        assert exc_info.value.envelope.error_code == ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class TestFormatTextAuditTrail:
    @pytest.mark.asyncio
    async def test_one_audit_line_per_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        pre = _simple_doc("Hello world", revision="rev-1")
        post = _simple_doc("Hello world", style={"bold": True}, revision="rev-2")
        p1, p2, p3, p4, _ = _mock_format(pre, post)
        with p1, p2, p3, p4:
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                )
        assert result.data["audit_logged"] is True
        audit_file = tmp_path / "verified-googledocs-mcp" / "audit.jsonl"
        assert audit_file.exists()
        lines = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["tool"] == "format_text"
        assert record["evidence"]["applied"] is True

    @pytest.mark.asyncio
    async def test_audit_failure_logged_in_evidence_real_write(self) -> None:
        """If append_audit raises on the real-write path, evidence carries
        audit_logged: false rather than failing the whole call."""
        pre = _simple_doc("Hello world", revision="rev-1")
        post = _simple_doc("Hello world", style={"bold": True}, revision="rev-2")
        p1, p2, p3, p4, _ = _mock_format(pre, post)
        with (
            p1,
            p2,
            p3,
            p4,
            patch(
                "verified_googledocs_mcp.formatting.append_audit",
                return_value=(False, "permission denied"),
            ),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        assert result.data["audit_logged"] is False
        assert result.data["audit_log_reason"] == "permission denied"

    @pytest.mark.asyncio
    async def test_audit_failure_logged_in_evidence_no_op(self) -> None:
        """Same, on the no-op (idempotent) path."""
        pre = _simple_doc("Hello world", style={"bold": True}, revision="rev-1")
        p1, p2, p3, p4, _ = _mock_format(pre, pre)
        with (
            p1,
            p2,
            p3,
            p4,
            patch(
                "verified_googledocs_mcp.formatting.append_audit",
                return_value=(False, "permission denied"),
            ),
        ):
            async with Client(mcp) as client:
                result = await client.call_tool(
                    "format_text",
                    {
                        "doc_id": "doc-test",
                        "tab_id": "tab-1",
                        "find": "Hello world",
                        "style": {"bold": True},
                    },
                )
        assert not result.is_error
        assert result.data["applied"] is True
        assert result.data["audit_logged"] is False
        assert result.data["audit_log_reason"] == "permission denied"
