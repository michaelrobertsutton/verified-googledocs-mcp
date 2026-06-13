"""FastMCP server: tool registration and entry point.

Auth never runs here. MCP clients spawn this process headless. If no valid
token exists, the first tool call fails fast with a clear instruction to run
`googledocs-mcp auth`.

Entry point dispatch
--------------------
  googledocs-mcp            → start the stdio MCP server
  googledocs-mcp auth       → run the OAuth flow (terminal, not headless)
"""

from __future__ import annotations

import sys
from typing import Any, Literal

from fastmcp import FastMCP

from .auth import get_credentials
from .docs import (
    build_docs_service,
    fetch_document,
    find_sections_in,
    list_tabs_from,
    read_tab,
)
from .middleware import EvidenceEnforcementMiddleware
from .verify import ErrorCode, VerifyError

mcp = FastMCP(
    "googledocs-mcp",
    instructions=(
        "MCP server for Google Docs with tab-scoped reads and verified writes. "
        "Every tool requires an explicit tab_id obtained from list_tabs. "
        "Call list_tabs first when you do not know the tab structure of a document. "
        "Mutating tools (replace_text) re-read after every write and return "
        "before/after evidence so writes cannot report false success."
    ),
)

mcp.add_middleware(EvidenceEnforcementMiddleware())


# ---------------------------------------------------------------------------
# Tool: list_tabs
# ---------------------------------------------------------------------------

@mcp.tool()
def list_tabs(doc_id: str) -> dict[str, Any]:
    """List the tabs in a Google Doc.

    Use this tool first when you need to read or edit a document but do not
    yet know its tab structure. Returns tab IDs, titles, nesting level, and
    index. Required before calling read_document or find_sections because
    every tool in this server requires an explicit tab_id.

    For documents created before Google's tabbed-docs feature, returns a
    single synthetic tab with id "_body" that covers the whole document.
    """
    service = _get_service()
    doc = fetch_document(service, doc_id)
    tabs = list_tabs_from(doc)
    return {"tabs": [t.as_dict() for t in tabs]}


# ---------------------------------------------------------------------------
# Tool: read_document
# ---------------------------------------------------------------------------

@mcp.tool()
def read_document(
    doc_id: str,
    tab_id: str,
    format: Literal["markdown", "structured"] = "markdown",
) -> dict[str, Any]:
    """Read the content of a specific tab in a Google Doc.

    Use this tool when you need to read the text, headings, tables, or
    structure of a document tab. Call list_tabs first to get the tab_id.

    format="markdown" (default): returns markdown text. Out-of-subset elements
    (images, smart chips, footnotes) appear as stable placeholder tokens and
    are listed in lossy_elements.

    format="structured": returns paragraph positions and style runs from the
    raw Docs JSON, suitable for computing exact edit ranges.

    Drive's files.export cannot scope to a single tab, which is why this
    server uses its own Docs JSON converter for markdown output.
    """
    service = _get_service()
    doc = fetch_document(service, doc_id)
    result = read_tab(doc, doc_id, tab_id, format=format)

    response: dict[str, Any] = {
        "doc_id": result.doc_id,
        "tab_id": result.tab_id,
        "format": result.format,
        "revision_id": result.revision_id,
        "content": result.content,
    }
    if result.lossy_elements:
        response["lossy_elements"] = result.lossy_elements
    return response


# ---------------------------------------------------------------------------
# Tool: find_sections
# ---------------------------------------------------------------------------

@mcp.tool()
def find_sections(
    doc_id: str,
    heading: str,
    tab_id: str,
) -> dict[str, Any]:
    """Find headings in a document tab and return their document ranges.

    Use this tool when you need to locate a section by its heading text
    before performing a targeted edit on that section. The returned ranges
    carry a computed_at_revision stamp; range-editing tools in later milestones
    will refuse stale ranges (ranges computed against an older document
    revision). Call find_sections immediately before editing — do not cache
    returned ranges across separate edits.

    Matching is case-insensitive and substring-based: a query of "intro" will
    match a heading "Introduction".
    """
    service = _get_service()
    doc = fetch_document(service, doc_id)
    matches = find_sections_in(doc, heading, tab_id)
    return {
        "doc_id": doc_id,
        "tab_id": tab_id,
        "heading_query": heading,
        "matches": [
            {
                "matched_text": m.matched_text,
                "start_index": m.start_index,
                "end_index": m.end_index,
                "computed_at_revision": m.computed_at_revision,
            }
            for m in matches
        ],
    }


# ---------------------------------------------------------------------------
# Tool: replace_text
# ---------------------------------------------------------------------------

@mcp.tool()
def replace_text(
    doc_id: str,
    tab_id: str,
    find: str,
    replace: str,
    expected_matches: int = 1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace occurrences of a text string in a Google Doc tab.

    Use this tool when you need to make an exact-text substitution in a
    document tab.  Call list_tabs first to get the tab_id, then read_document
    to confirm the text you want to replace is present as-is.

    The tool locates every occurrence of ``find`` using a normalization ladder
    (exact → curly/straight quote equivalence → NBSP/whitespace collapse →
    soft-hyphen strip) and refuses the write if the match count does not equal
    ``expected_matches``.  This prevents accidental multi-replacement and
    duplicate-sentence collapse.

    Set ``dry_run=True`` to preview the operation without writing; the response
    carries ``applied: false`` and the matched span information but makes no
    API call.

    On success the response carries before/after excerpts (±200 chars), the
    normalization rung used, pre/post revision IDs, and ``audit_logged``.

    Errors are returned as typed envelopes with ``error_code``, ``message``,
    ``diagnostics``, and ``retryable`` so the caller can act on them precisely:
      ZERO_MATCH          – find string not found; near-miss span included
      MATCH_COUNT_MISMATCH – wrong number of matches; all locations listed
      REVISION_CONFLICT   – document changed mid-call; re-read and retry
      STRUCTURAL_BOUNDARY – match crosses a paragraph boundary
      INVALID_INPUT       – empty find, or find equals replace
      TAB_NOT_FOUND       – tab_id not in document; available tabs listed
    """
    from .mutations import execute_replace_text

    service = _get_service()
    try:
        return execute_replace_text(
            service=service,
            doc_id=doc_id,
            tab_id=tab_id,
            find=find,
            replace=replace,
            expected_matches=expected_matches,
            dry_run=dry_run,
        )
    except VerifyError as exc:
        # Surface the typed envelope as the MCP tool error payload so the
        # caller receives structured diagnostics rather than a bare string.
        from fastmcp.exceptions import ToolError

        raise ToolError(str(exc.envelope.to_dict())) from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_service() -> Any:
    """Return a Docs API service, failing fast if credentials are missing."""
    credentials = get_credentials()
    return build_docs_service(credentials)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the `googledocs-mcp` command.

    `googledocs-mcp auth`  — run the OAuth flow in the terminal.
    `googledocs-mcp`       — start the stdio MCP server.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from .auth import run_auth_flow
        run_auth_flow()
        return

    mcp.run()
