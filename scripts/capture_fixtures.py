#!/usr/bin/env python
"""Capture a real Google Doc's JSON as a test fixture.

Run this once a scratch document and OAuth credentials exist (see
docs/fixture-session.md). It fetches the document with full tab content and
inline suggestions and writes the raw API response to
tests/unit/fixtures/live_capture/<name>.json, which the unit suite then uses
as a deterministic, offline fixture.

Prerequisites:
    - A Google Cloud project with the Docs + Drive APIs enabled and a Desktop
      OAuth client secret at ~/.config/googledocs-mcp/credentials.json
    - A cached token: run `googledocs-mcp auth` first.

Usage:
    uv run python scripts/capture_fixtures.py <DOCUMENT_ID> --name scratch_multitab
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from googledocs_mcp.auth import get_credentials
from googledocs_mcp.docs import build_docs_service

# Pin one suggestions view so captured indices match what the locator expects.
SUGGESTIONS_VIEW_MODE = "SUGGESTIONS_INLINE"

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "unit" / "fixtures" / "live_capture"


def fetch_with_suggestions(service, document_id: str) -> dict:
    """Fetch the document with tab content and inline suggestions."""
    return (
        service.documents()
        .get(
            documentId=document_id,
            includeTabsContent=True,
            suggestionsViewMode=SUGGESTIONS_VIEW_MODE,
        )
        .execute(num_retries=3)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document_id", help="The Google Doc ID to capture")
    parser.add_argument(
        "--name",
        required=True,
        help="Fixture base name, e.g. scratch_multitab (written as <name>.json)",
    )
    args = parser.parse_args()

    service = build_docs_service(get_credentials())
    document = fetch_with_suggestions(service, args.document_id)

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FIXTURE_DIR / f"{args.name}.json"
    out_path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")

    revision = document.get("revisionId", "(none)")
    tabs = document.get("tabs", [])
    print(f"Wrote {out_path}")
    print(f"  revisionId: {revision}")
    print(f"  tabs: {len(tabs)}")


if __name__ == "__main__":
    main()
