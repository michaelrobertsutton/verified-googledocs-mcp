"""Tests for the audit writer — append_audit(), XDG path, best-effort policy,
audit_excerpts redaction."""

import json
import os
import stat
from pathlib import Path

import pytest

from verified_googledocs_mcp.verify import append_audit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_audit_excerpts_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every audit test from an ambient redaction env var.

    append_audit now consults VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS; clearing it
    by default keeps the param-driven tests deterministic regardless of the
    developer's shell, and the env-var tests opt back in explicitly.
    """
    monkeypatch.delenv("VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS", raising=False)


def _read_audit(path: Path) -> list[dict]:
    records = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestAuditHappyPath:
    def test_creates_file_and_returns_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        ok, reason = append_audit(
            doc="doc123",
            tab="tab1",
            tool="replace_text",
            evidence={"before": "old text", "after": "new text"},
        )
        assert ok is True
        assert reason == ""
        audit_file = tmp_path / "verified-googledocs-mcp" / "audit.jsonl"
        assert audit_file.exists()

    def test_record_shape(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(
            doc="doc-abc",
            tab="tab-xyz",
            tool="replace_text",
            evidence={"before": "a", "after": "b", "match_count": 1},
        )
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        assert len(records) == 1
        rec = records[0]
        assert rec["doc"] == "doc-abc"
        assert rec["tab"] == "tab-xyz"
        assert rec["tool"] == "replace_text"
        assert "timestamp" in rec
        assert rec["evidence"]["before"] == "a"
        assert rec["evidence"]["after"] == "b"

    def test_timestamp_is_iso8601_utc(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(doc="d", tab="t", tool="x", evidence={})
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        ts = records[0]["timestamp"]
        # Should end with +00:00 or Z (Python datetime.isoformat with utc).
        assert ts.endswith("+00:00") or ts.endswith("Z")

    def test_append_only_multiple_calls(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        for i in range(3):
            append_audit(doc=f"d{i}", tab="t", tool="replace_text", evidence={"n": i})
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        assert len(records) == 3
        assert [r["doc"] for r in records] == ["d0", "d1", "d2"]

    def test_xdg_state_home_default_path(self, tmp_path, monkeypatch):
        """When XDG_STATE_HOME is unset, uses ~/.local/state."""
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        home = tmp_path / "fakehome"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        # Need to also patch Path.home() if it doesn't pick up $HOME.
        # On macOS, Path.home() reads /etc/passwd, not $HOME, so patch it directly.
        import verified_googledocs_mcp.verify as verify_mod

        def patched_state_dir():
            xdg = os.environ.get("XDG_STATE_HOME", "")
            if xdg:
                base = Path(xdg)
            else:
                base = home / ".local" / "state"
            return base / "verified-googledocs-mcp"

        monkeypatch.setattr(verify_mod, "_state_dir", patched_state_dir)
        ok, _ = append_audit(doc="d", tab="t", tool="t", evidence={})
        assert ok is True
        assert (home / ".local" / "state" / "verified-googledocs-mcp" / "audit.jsonl").exists()

    def test_creates_directory_on_demand(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "c"
        monkeypatch.setenv("XDG_STATE_HOME", str(nested))
        ok, _ = append_audit(doc="d", tab="t", tool="x", evidence={})
        assert ok is True
        assert (nested / "verified-googledocs-mcp" / "audit.jsonl").exists()


# ---------------------------------------------------------------------------
# audit_excerpts=False redaction
# ---------------------------------------------------------------------------


class TestAuditExcerptsRedaction:
    def test_excerpts_false_redacts_before_after(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(
            doc="d",
            tab="t",
            tool="replace_text",
            evidence={"before": "sensitive content here", "after": "also sensitive"},
            audit_excerpts=False,
        )
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        ev = records[0]["evidence"]
        assert "sensitive" not in ev["before"]
        assert "sensitive" not in ev["after"]
        assert "[redacted;" in ev["before"]
        assert "[redacted;" in ev["after"]

    def test_excerpts_false_keeps_other_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(
            doc="d",
            tab="t",
            tool="replace_text",
            evidence={
                "before": "text",
                "after": "text2",
                "match_count": 1,
                "rung": "exact",
            },
            audit_excerpts=False,
        )
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        ev = records[0]["evidence"]
        assert ev["match_count"] == 1
        assert ev["rung"] == "exact"

    def test_excerpts_true_keeps_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(
            doc="d",
            tab="t",
            tool="replace_text",
            evidence={"before": "keep this", "after": "and this"},
            audit_excerpts=True,
        )
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        ev = records[0]["evidence"]
        assert ev["before"] == "keep this"
        assert ev["after"] == "and this"

    def test_redaction_includes_length_hint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        content = "x" * 100
        append_audit(
            doc="d",
            tab="t",
            tool="t",
            evidence={"before": content},
            audit_excerpts=False,
        )
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        before_val = records[0]["evidence"]["before"]
        # Should mention the character count.
        assert "100" in before_val


# ---------------------------------------------------------------------------
# Env-var control surface: VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS
#
# Every tool call site passes audit_excerpts=True, so the env var is the only
# way to reach the redaction path end-to-end (issue #30).
# ---------------------------------------------------------------------------


class TestAuditExcerptsEnvVar:
    _ENV = "VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS"

    def _before(self, tmp_path: Path) -> str:
        records = _read_audit(tmp_path / "verified-googledocs-mcp" / "audit.jsonl")
        return records[0]["evidence"]["before"]

    def test_env_false_redacts_without_param(self, tmp_path, monkeypatch):
        """Default audit_excerpts=True (as every call site passes); env redacts."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv(self._ENV, "false")
        append_audit(
            doc="d",
            tab="t",
            tool="replace_text",
            evidence={"before": "sensitive", "after": "sensitive"},
        )
        assert "[redacted;" in self._before(tmp_path)
        assert "sensitive" not in self._before(tmp_path)

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " off ", ""])
    def test_env_falsey_values_redact(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv(self._ENV, value)
        append_audit(doc="d", tab="t", tool="t", evidence={"before": "sensitive"})
        assert "[redacted;" in self._before(tmp_path)

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_env_truthy_values_keep(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv(self._ENV, value)
        append_audit(doc="d", tab="t", tool="t", evidence={"before": "keep me"})
        assert self._before(tmp_path) == "keep me"

    def test_env_overrides_explicit_true(self, tmp_path, monkeypatch):
        """Operator config wins over an explicit audit_excerpts=True."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setenv(self._ENV, "off")
        append_audit(
            doc="d",
            tab="t",
            tool="t",
            evidence={"before": "sensitive"},
            audit_excerpts=True,
        )
        assert "[redacted;" in self._before(tmp_path)

    def test_unset_env_falls_back_to_param(self, tmp_path, monkeypatch):
        """With the env var cleared (autouse fixture), the param default keeps excerpts."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(doc="d", tab="t", tool="t", evidence={"before": "keep me"})
        assert self._before(tmp_path) == "keep me"


# ---------------------------------------------------------------------------
# Best-effort failure policy
# ---------------------------------------------------------------------------


class TestAuditFailurePolicy:
    def test_unwritable_dir_returns_false_not_raise(self, tmp_path, monkeypatch):
        # Point XDG_STATE_HOME at a path whose parent is a file (unwritable).
        bad_parent = tmp_path / "i_am_a_file"
        bad_parent.write_text("block")
        # The state dir would be bad_parent/verified-googledocs-mcp — impossible to create.
        monkeypatch.setenv("XDG_STATE_HOME", str(bad_parent))
        ok, reason = append_audit(doc="d", tab="t", tool="t", evidence={})
        assert ok is False
        assert len(reason) > 0

    def test_failure_does_not_raise(self, tmp_path, monkeypatch):
        """Ensure no exception escapes on append failure."""
        bad_parent = tmp_path / "blocker"
        bad_parent.write_text("block")
        monkeypatch.setenv("XDG_STATE_HOME", str(bad_parent))
        # Should not raise.
        result = append_audit(doc="d", tab="t", tool="t", evidence={})
        assert isinstance(result, tuple)
        assert result[0] is False

    def test_read_only_file_returns_false(self, tmp_path, monkeypatch):
        """If the audit.jsonl file exists but is read-only, return False."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        state_dir = tmp_path / "verified-googledocs-mcp"
        state_dir.mkdir(parents=True)
        audit_file = state_dir / "audit.jsonl"
        audit_file.write_text("")
        audit_file.chmod(stat.S_IREAD)  # read-only
        try:
            ok, reason = append_audit(doc="d", tab="t", tool="t", evidence={})
            assert ok is False
            assert len(reason) > 0
        finally:
            # Restore permissions so cleanup works.
            audit_file.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_return_value_embeddable_in_evidence(self, tmp_path, monkeypatch):
        """The tuple (ok, reason) should be usable as audit_logged + reason fields."""
        bad_parent = tmp_path / "blocker2"
        bad_parent.write_text("block")
        monkeypatch.setenv("XDG_STATE_HOME", str(bad_parent))
        ok, reason = append_audit(doc="d", tab="t", tool="t", evidence={})
        # Caller pattern: evidence["audit_logged"] = ok; evidence["audit_fail_reason"] = reason
        assert ok is False
        assert isinstance(reason, str)


# ---------------------------------------------------------------------------
# File permissions: the audit log records document excerpts; keep it owner-only
# ---------------------------------------------------------------------------


class TestAuditPermissions:
    def test_audit_file_is_owner_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(doc="d", tab="t", tool="replace_text", evidence={"before": "a", "after": "b"})
        audit_file = tmp_path / "verified-googledocs-mcp" / "audit.jsonl"
        mode = stat.S_IMODE(audit_file.stat().st_mode)
        assert mode == 0o600, f"audit log mode is {oct(mode)}, expected 0o600"

    def test_state_dir_is_owner_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        append_audit(doc="d", tab="t", tool="replace_text", evidence={"before": "a", "after": "b"})
        state_dir = tmp_path / "verified-googledocs-mcp"
        mode = stat.S_IMODE(state_dir.stat().st_mode)
        assert mode == 0o700, f"state dir mode is {oct(mode)}, expected 0o700"

    def test_tightens_preexisting_loose_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        state_dir = tmp_path / "verified-googledocs-mcp"
        state_dir.mkdir(parents=True)
        loose = state_dir / "audit.jsonl"
        loose.write_text("")
        os.chmod(loose, 0o644)
        append_audit(doc="d", tab="t", tool="replace_text", evidence={"before": "a", "after": "b"})
        assert stat.S_IMODE(loose.stat().st_mode) == 0o600
