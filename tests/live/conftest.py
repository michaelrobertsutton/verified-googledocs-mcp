"""Shared fixtures and quarantine machinery for the live acceptance suite.

This suite is the pre-release gate (issue #23): it exercises every tool and
every error code against the *real* Google Docs and Drive APIs, not against
recorded fixtures. It therefore needs working OAuth credentials and a seeded
fixture document, and it must never run in CI.

Quarantine
----------
Every test here is marked ``live`` (applied automatically by the package-level
``pytestmark`` in each module, and reinforced by ``--run-live``):

  * ``pytest`` with no flag  → live tests are skipped (this is what CI runs).
  * ``pytest --run-live``    → live tests run, *if* credentials are present.

A missing token also skips the whole suite, so even ``--run-live`` is safe on a
machine without credentials.

Fixture document
----------------
Defaults to the seeded scratch doc from issue #1; override with the env var
``VERIFIED_GOOGLEDOCS_MCP_TEST_DOC``. Read-only checks (suggestions, the seeded
comment thread) run against this canonical doc. Mutating checks run against a
disposable ``files.copy`` of it that is hard-deleted on teardown, so the
canonical fixture is never modified.

Audit-log isolation
--------------------
``_state_dir()`` honours ``XDG_STATE_HOME``. An autouse fixture points it at a
per-test tmp dir, so (a) the suite never pollutes the real audit log and
(b) "exactly one JSONL line per mutation" can be asserted in isolation.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Canonical seeded fixture (issue #1, extended in #31). Override via env.
DEFAULT_DOC_ID = "1Zm_6bAwA7UH1DKkGVL3kg9XcQ6rIZHmUFcQPTcoJb6Y"

# Real substrate seeded into the canonical fixture (and inherited by every
# files.copy of it): a HEADING_1 paragraph and a nested tab. See #31.
CANONICAL_HEADING_TEXT = "Text Hazards"  # HEADING_1 in t.0, resolves to range [1, 14)
CANONICAL_NESTED_TAB_ID = "t.22v4eg81pdjk"  # "Nested Tab", child of t.0


# ---------------------------------------------------------------------------
# Quarantine: --run-live flag + credential guard
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run the live acceptance suite against the real Google APIs.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(
        reason="live acceptance suite — pass --run-live (and have credentials) to run"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# ---------------------------------------------------------------------------
# Credentials + API services (session-scoped; built once)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_services() -> tuple[Any, Any]:
    """Return (docs_service, drive_service), or skip if credentials are absent."""
    from verified_googledocs_mcp.auth import get_credentials
    from verified_googledocs_mcp.comments import build_drive_service
    from verified_googledocs_mcp.docs import build_docs_service

    try:
        creds = get_credentials()
    except Exception as exc:  # noqa: BLE001 — any auth failure means "can't run live"
        pytest.skip(f"no live credentials available: {exc}")
    return build_docs_service(creds), build_drive_service(creds)


@pytest.fixture(scope="session")
def canonical_doc_id(live_services: tuple[Any, Any]) -> str:
    """The seeded fixture doc id; verified reachable before the suite runs."""
    from verified_googledocs_mcp.docs import fetch_document

    docs, _ = live_services
    doc_id = os.environ.get("VERIFIED_GOOGLEDOCS_MCP_TEST_DOC", DEFAULT_DOC_ID)
    try:
        fetch_document(docs, doc_id)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"fixture doc {doc_id!r} not reachable: {exc}")
    return doc_id


# ---------------------------------------------------------------------------
# In-memory MCP client (exercises tool registration + enforcement middleware)
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():  # type: ignore[no-untyped-def]
    """A connected in-memory FastMCP client bound to the real server app.

    Calls go through tool registration and the EvidenceEnforcementMiddleware,
    then out to the live Google APIs — the full end-to-end path.
    """
    from fastmcp import Client

    from verified_googledocs_mcp.server import mcp

    async with Client(mcp) as c:
        yield c


# ---------------------------------------------------------------------------
# Disposable scratch copies (canonical fixture is never mutated)
# ---------------------------------------------------------------------------


def _make_scratch_copy(docs: Any, drive: Any, src_id: str) -> SimpleNamespace:
    from verified_googledocs_mcp.docs import fetch_document, list_tabs_from

    copy = (
        drive.files()
        .copy(
            fileId=src_id,
            body={"name": "verified-gdocs-mcp LIVE-TEST scratch (auto-delete)"},
        )
        .execute()
    )
    cid = copy["id"]
    doc = fetch_document(docs, cid)
    tab_ids = [t.tab_id for t in list_tabs_from(doc)]
    return SimpleNamespace(doc_id=cid, tab_ids=tab_ids, primary_tab=tab_ids[0])


@pytest.fixture
def scratch_doc(live_services: tuple[Any, Any], canonical_doc_id: str):  # type: ignore[no-untyped-def]
    """A fresh, disposable copy of the fixture for one mutating test.

    Preserves tab structure and the hazard text (curly quotes, NBSP, soft
    hyphen, UTF-16 set); does NOT preserve comments or suggestions (Drive does
    not copy those). Hard-deleted on teardown.
    """
    docs, drive = live_services
    scratch = _make_scratch_copy(docs, drive, canonical_doc_id)
    try:
        yield scratch
    finally:
        try:
            drive.files().delete(fileId=scratch.doc_id).execute()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


# ---------------------------------------------------------------------------
# Audit-log isolation (autouse: protects the real log, enables clean asserts)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the audit log at a per-test tmp dir via XDG_STATE_HOME.

    Returns the path to the audit.jsonl this test's mutations will write to.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "verified-googledocs-mcp" / "audit.jsonl"
