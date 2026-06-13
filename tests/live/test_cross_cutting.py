"""§6 Cross-cutting guarantees — enforcement middleware, audit log, auth,
input validation, unknown tab.

The audit assertions rely on the autouse ``isolated_audit_dir`` fixture, which
points XDG_STATE_HOME at a per-test tmp dir so each test's audit file is clean.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.live


def _err(result) -> str:  # type: ignore[no-untyped-def]
    return str(result.content)


def _lines(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [json.loads(ln) for ln in audit_path.read_text().splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Enforcement middleware — evidence present, does not misfire on real responses
# ---------------------------------------------------------------------------


class TestEnforcementMiddleware:
    async def test_real_mutation_carries_evidence_and_passes(self, client, scratch_doc):
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "[rev-probe]",
                "replace": "[mw-check]",
            },
        )
        # The middleware accepted a genuine live response carrying evidence.
        assert not r.is_error
        assert "applied" in r.data

    async def test_typed_error_passes_through_not_a_middleware_reject(
        self, client, canonical_doc_id
    ):
        r = await client.call_tool(
            "replace_text",
            {
                "doc_id": canonical_doc_id,
                "tab_id": "t.0",
                "find": "definitely-not-present-zzz",
                "replace": "x",
            },
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        # It is the typed VerifyError envelope, not the middleware's
        # "returned a result without evidence" backstop.
        assert "ZERO_MATCH" in content
        assert "without evidence" not in content


# ---------------------------------------------------------------------------
# Audit log — one line per mutation, 0600 file under 0700 dir, redaction
# ---------------------------------------------------------------------------


class TestAuditLog:
    async def test_one_jsonl_line_per_mutation(self, client, scratch_doc, isolated_audit_dir):
        assert _lines(isolated_audit_dir) == []

        await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "[rev-probe]",
                "replace": "[audit-1]",
            },
        )
        after_one = _lines(isolated_audit_dir)
        assert len(after_one) == 1
        assert after_one[0]["tool"] == "replace_text"
        assert "evidence" in after_one[0]

        await client.call_tool(
            "append_markdown",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "markdown": "second mutation\n",
            },
        )
        after_two = _lines(isolated_audit_dir)
        assert len(after_two) == 2  # exactly one new line for the second mutation
        assert after_two[1]["tool"] == "append_markdown"

    async def test_audit_file_is_owner_only_under_owner_only_dir(
        self, client, scratch_doc, isolated_audit_dir
    ):
        await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "[rev-probe]",
                "replace": "[perms]",
            },
        )
        assert isolated_audit_dir.exists()
        assert oct(isolated_audit_dir.stat().st_mode & 0o777) == "0o600"
        assert oct(isolated_audit_dir.parent.stat().st_mode & 0o777) == "0o700"

    def test_audit_excerpts_false_redacts_before_after(self, isolated_audit_dir):
        """The redaction logic itself, exercised directly (it has no tool surface yet — #30)."""
        from verified_googledocs_mcp.verify import append_audit

        logged, reason = append_audit(
            doc="doc-x",
            tab="t.0",
            tool="replace_text",
            evidence={"applied": True, "before": "secret before", "after": "secret after"},
            audit_excerpts=False,
        )
        assert logged, reason
        rec = _lines(isolated_audit_dir)[0]
        ev = rec["evidence"]
        assert ev["applied"] is True  # metadata kept
        assert "secret before" not in json.dumps(ev)  # content redacted
        assert ev["before"].startswith("[redacted")
        assert ev["after"].startswith("[redacted")

    @pytest.mark.xfail(
        reason="blocked by #30 — audit_excerpts has no tool/env/config surface, so a live "
        "mutation cannot request redaction. Flips to pass once #30 exposes the toggle.",
        strict=False,
    )
    async def test_audit_excerpts_false_via_env(
        self, client, scratch_doc, isolated_audit_dir, monkeypatch
    ):
        monkeypatch.setenv("VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS", "false")
        await client.call_tool(
            "replace_text",
            {
                "doc_id": scratch_doc.doc_id,
                "tab_id": scratch_doc.primary_tab,
                "find": "[rev-probe]",
                "replace": "[redact-live]",
            },
        )
        rec = _lines(isolated_audit_dir)[0]
        assert rec["evidence"]["before"].startswith("[redacted")


# ---------------------------------------------------------------------------
# Auth — fail fast with AUTH_EXPIRED (blocked by #29)
# ---------------------------------------------------------------------------


class TestAuth:
    @pytest.mark.xfail(
        reason="blocked by #29 — auth failure raises a bare RuntimeError, not an AUTH_EXPIRED "
        "envelope. Flips to pass once #29 maps credential failures to AUTH_EXPIRED.",
        strict=False,
    )
    async def test_missing_token_surfaces_auth_expired(
        self, client, canonical_doc_id, tmp_path
    ):
        # Point the token path at a nonexistent file: get_credentials fails fast.
        with patch(
            "verified_googledocs_mcp.auth._token_path",
            lambda: tmp_path / "no-such-token.json",
        ):
            r = await client.call_tool(
                "list_tabs", {"doc_id": canonical_doc_id}, raise_on_error=False
            )
        assert r.is_error and "AUTH_EXPIRED" in _err(r)


# ---------------------------------------------------------------------------
# Input validation + unknown tab
# ---------------------------------------------------------------------------


class TestInputValidationAndUnknownTab:
    async def test_contradictory_args_are_invalid_input(self, client, canonical_doc_id):
        # find == replace: no change would be made.
        r = await client.call_tool(
            "replace_text",
            {"doc_id": canonical_doc_id, "tab_id": "t.0", "find": "Soft", "replace": "Soft"},
            raise_on_error=False,
        )
        assert r.is_error and "INVALID_INPUT" in _err(r)

    async def test_unknown_tab_lists_available_tabs(self, client, canonical_doc_id):
        r = await client.call_tool(
            "replace_text",
            {"doc_id": canonical_doc_id, "tab_id": "t.bogus", "find": "Soft", "replace": "x"},
            raise_on_error=False,
        )
        assert r.is_error
        content = _err(r)
        assert "TAB_NOT_FOUND" in content
        assert "t.0" in content  # available tabs listed
