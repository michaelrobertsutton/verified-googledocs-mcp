"""Shared assertion pinning EvidenceEnforcementMiddleware's structural
assumption: it reads evidence keys off the TOP LEVEL of
``result.structured_content`` (middleware.py:74-76). FastMCP only preserves
that shape while a tool's inferred output schema is object-typed; a non-object
schema gets wrapped under a single ``result`` key instead (see
tests/unit/test_middleware.py::TestStructuredContentShapeAssumption for the
FastMCP-side pin of that behaviour, and
tests/unit/test_tool_manifest.py::TestOutputSchemaContract for the
manifest-level guard).

Used at every offline-callable mutating tool's happy-path/middleware call site
so the wrap failure mode can't hide behind a single tool (e.g. only the table
tools) while another (e.g. replace_text) silently regresses.
"""

from __future__ import annotations

from typing import Any

from verified_googledocs_mcp.middleware import _EVIDENCE_KEYS


def assert_top_level_evidence(result: Any) -> None:
    """Assert a mutating tool's result carries evidence the way the
    middleware actually reads it: as top-level keys of structured_content,
    not nested under a FastMCP-inserted ``result`` wrapper key."""
    sc = result.structured_content
    assert isinstance(sc, dict), f"structured_content is not a dict: {sc!r}"
    assert _EVIDENCE_KEYS & sc.keys(), (
        f"no evidence key ({sorted(_EVIDENCE_KEYS)}) found at the top level of "
        f"structured_content={sc!r} — if this is a FastMCP result-wrapping "
        "regression, keys will be nested under 'result' instead"
    )
    assert set(sc.keys()) != {"result"}, (
        f"structured_content={sc!r} looks wrapped under a single 'result' key — "
        "EvidenceEnforcementMiddleware reads top-level keys only and would miss this"
    )
