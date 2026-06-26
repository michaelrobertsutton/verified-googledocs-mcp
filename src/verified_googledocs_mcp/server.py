"""FastMCP server: tool registration and entry point.

Auth never runs here. MCP clients spawn this process headless. If no valid
token exists, the first tool call fails fast with a clear instruction to run
`verified-googledocs-mcp auth`.

Entry point dispatch
--------------------
  verified-googledocs-mcp            → start the stdio MCP server
  verified-googledocs-mcp auth       → run the OAuth flow (terminal, not headless)
"""

from __future__ import annotations

import json
import sys
from typing import Any, Literal, NoReturn

from fastmcp import FastMCP

from .auth import get_credentials
from .comments import (
    build_drive_service,
    execute_add_anchored_comment,
    execute_reply_to_comment,
    execute_resolve_comment,
    get_comment_thread,
    list_comments,
)
from .docs import (
    _available_tab_ids,
    build_docs_service,
    fetch_document,
    find_sections_in,
    list_tabs_from,
    read_tab,
)
from .middleware import EvidenceEnforcementMiddleware
from .suggestions import extract_suggestions
from .markdown_mutations import (
    execute_append_markdown,
    execute_diff_tab_vs_file,
    execute_insert_image,
    execute_replace_range_markdown,
    execute_replace_tab_markdown,
)
from .verify import ErrorCode, VerifyError, _make_error

mcp = FastMCP(
    "verified-googledocs-mcp",
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
    try:
        result = read_tab(doc, doc_id, tab_id, format=format)
    except ValueError as exc:
        _raise_tool_error(
            _make_error(
                ErrorCode.TAB_NOT_FOUND,
                str(exc),
                {"doc_id": doc_id, "tab_id": tab_id},
            )
        )

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
    try:
        matches = find_sections_in(doc, heading, tab_id)
    except ValueError as exc:
        _raise_tool_error(
            _make_error(
                ErrorCode.TAB_NOT_FOUND,
                str(exc),
                {"doc_id": doc_id, "tab_id": tab_id},
            )
        )
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
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: list_open_items
# ---------------------------------------------------------------------------


@mcp.tool()
def list_open_items(doc_id: str, tab_id: str = "", include_all_tabs: bool = False) -> dict[str, Any]:
    """List all open comments and pending suggested edits on a document.

    Use this tool when you need a single unified view of all open review
    items on a document.  Returns both Drive-level comments (labeled
    scope='document') and per-tab suggested edits in one response.

    Comments come from the Drive API and cannot be attributed to a specific
    tab — Drive comment anchors are opaque. If tab_id is provided it filters
    the suggestions returned to that tab only. To include suggestions from
    every tab, set include_all_tabs=true. Comments are always returned
    document-wide regardless of tab_id.

    Suggestions are extracted from the raw Docs JSON (suggestedInsertionIds /
    suggestedDeletionIds / suggestedTextStyleChanges) and are per-tab.  The
    document is fetched with suggestionsViewMode=SUGGESTIONS_INLINE so that
    suggestion fields are populated.
    """
    if not tab_id and not include_all_tabs:
        _raise_tool_error(
            _make_error(
                ErrorCode.INVALID_INPUT,
                "Pass tab_id for scoped suggestions, or include_all_tabs=true deliberately.",
                {"doc_id": doc_id, "tab_id": tab_id, "include_all_tabs": include_all_tabs},
            )
        )
    if tab_id and include_all_tabs:
        _raise_tool_error(
            _make_error(
                ErrorCode.INVALID_INPUT,
                "Pass either tab_id or include_all_tabs=true, not both.",
                {"doc_id": doc_id, "tab_id": tab_id, "include_all_tabs": include_all_tabs},
            )
        )

    credentials = _get_credentials()
    docs_service = build_docs_service(credentials)
    drive_service = build_drive_service(credentials)

    # Comments: doc-level from Drive.
    open_comments = list_comments(drive_service, doc_id)

    # Suggestions: per-tab from Docs JSON with SUGGESTIONS_INLINE.
    doc = (
        docs_service.documents()
        .get(documentId=doc_id, includeTabsContent=True, suggestionsViewMode="SUGGESTIONS_INLINE")
        .execute(num_retries=3)
    )

    if tab_id:
        try:
            suggestions = extract_suggestions(doc, tab_id)
        except ValueError as exc:
            _raise_tool_error(
                _make_error(
                    ErrorCode.TAB_NOT_FOUND,
                    str(exc),
                    {"doc_id": doc_id, "tab_id": tab_id},
                )
            )
    else:
        all_suggestions: list[dict[str, Any]] = []
        for tid in _available_tab_ids(doc):
            try:
                all_suggestions.extend(extract_suggestions(doc, tid))
            except ValueError:
                pass
        suggestions = all_suggestions

    return {
        "doc_id": doc_id,
        "open_comments": open_comments,
        "pending_suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# Tool: get_comment_thread
# ---------------------------------------------------------------------------


@mcp.tool(name="get_comment_thread")
def get_comment_thread_tool(doc_id: str, comment_id: str) -> dict[str, Any]:
    """Retrieve the full reply chain for a comment.

    Use this tool when you need to read a comment thread in full before
    deciding on a response or resolution.  Returns the comment content,
    all replies, quoted text, resolved status, and author.

    Requires both the doc_id (the Google Doc's file ID) and the comment_id
    from the Drive API.
    """
    credentials = _get_credentials()
    drive_service = build_drive_service(credentials)
    return get_comment_thread(drive_service, doc_id, comment_id)


# ---------------------------------------------------------------------------
# Tool: add_anchored_comment
# ---------------------------------------------------------------------------


@mcp.tool()
def add_anchored_comment(doc_id: str, tab_id: str, quote: str, body: str) -> dict[str, Any]:
    """Add a comment to a document, validated against a quoted passage.

    Use this tool when you need to create a comment on specific text in a
    document tab.  The quote must exist in the tab — the tool locates it via
    the same normalization ladder as replace_text and returns QUOTE_NOT_FOUND
    with nearest candidate anchors if the quote is absent.

    NOTE: The Drive API may render the created comment as document-level even
    when quotedFileContent is supplied.  This behaviour is pending confirmation
    from a live anchoring spike; for now the comment is created with the quote
    embedded in its content and the tool returns comment-state evidence.

    Returns comment-state evidence: applied, comment_id, resolved, reply_count,
    content, quoted_text, audit_logged.

    Errors:
      QUOTE_NOT_FOUND  – quote not found in the tab; nearest candidates listed
      INVALID_INPUT    – empty body or quote
      TAB_NOT_FOUND    – tab_id not in document
    """
    credentials = _get_credentials()
    docs_service = build_docs_service(credentials)
    drive_service = build_drive_service(credentials)
    try:
        return execute_add_anchored_comment(
            drive_service=drive_service,
            docs_service=docs_service,
            doc_id=doc_id,
            tab_id=tab_id,
            quote=quote,
            body=body,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: reply_to_comment
# ---------------------------------------------------------------------------


@mcp.tool()
def reply_to_comment(doc_id: str, comment_id: str, body: str) -> dict[str, Any]:
    """Add a reply to an existing comment thread.

    Use this tool when you need to respond to a reviewer comment without
    resolving it.  The reply is added to the thread and the tool re-queries
    the comment to return post-state evidence.

    Returns comment-state evidence: applied, comment_id, resolved, reply_count,
    content, quoted_text, audit_logged.

    Errors:
      INVALID_INPUT  – empty body or comment not found
    """
    credentials = _get_credentials()
    drive_service = build_drive_service(credentials)
    try:
        return execute_reply_to_comment(
            drive_service=drive_service,
            doc_id=doc_id,
            comment_id=comment_id,
            body=body,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: resolve_comment
# ---------------------------------------------------------------------------


@mcp.tool()
def resolve_comment(doc_id: str, comment_id: str) -> dict[str, Any]:
    """Resolve a comment on a document and verify the resolution landed.

    Use this tool when you need to mark a reviewer comment as resolved.
    Resolves via Drive replies.create(action='resolve') — the only mechanism
    that actually resolves comments in Drive API v3.  Using comments.update
    with resolved=true is silently ignored (resolved is a read-only field),
    which is the incumbent server's bug.

    After issuing the resolve the tool re-queries the comment and returns
    the actual final state.  A comment that is still open after the resolve
    attempt is reported as COMMENT_STILL_OPEN — never as success.

    Returns comment-state evidence: applied, comment_id, resolved, reply_count,
    content, quoted_text, audit_logged.

    Errors:
      COMMENT_STILL_OPEN  – comment did not resolve; post-state included
      INVALID_INPUT       – comment not found
    """
    credentials = _get_credentials()
    drive_service = build_drive_service(credentials)
    try:
        return execute_resolve_comment(
            drive_service=drive_service,
            doc_id=doc_id,
            comment_id=comment_id,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: replace_range_markdown
# ---------------------------------------------------------------------------


@mcp.tool()
def replace_range_markdown(
    doc_id: str,
    tab_id: str,
    start_index: int,
    end_index: int,
    computed_at_revision: str,
    markdown: str,
    allow_structural_loss: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace a document range with compiled markdown.

    Use this tool when you need to replace a section of a document with new
    markdown content. Obtain start_index, end_index, and computed_at_revision
    from find_sections. The range stamp is validated against the current
    document revision — a stale stamp raises STALE_RANGE ("re-run find_sections").

    The structural guardrail inventories tables, images, chips, and footnotes
    inside the target range before writing. If the replacement markdown does
    not account for them the write is refused unless allow_structural_loss=true.
    A blast-radius check compares structural element counts outside the edited
    range pre/post; any change there is a hard failure.

    Set dry_run=true to validate and preview without writing.

    Errors:
      STALE_RANGE          – range stamp is outdated; re-run find_sections
      UNSUPPORTED_MARKDOWN – markdown contains an unsupported construct
      INVALID_INPUT        – structural guardrail refused or blast-radius violation
      TAB_NOT_FOUND        – tab_id not in document
      REVISION_CONFLICT    – document changed mid-call; re-read and retry
    """
    service = _get_service()
    try:
        return execute_replace_range_markdown(
            service=service,
            doc_id=doc_id,
            tab_id=tab_id,
            start_index=start_index,
            end_index=end_index,
            computed_at_revision=computed_at_revision,
            markdown=markdown,
            allow_structural_loss=allow_structural_loss,
            dry_run=dry_run,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: replace_tab_markdown
# ---------------------------------------------------------------------------


@mcp.tool()
def replace_tab_markdown(
    doc_id: str,
    tab_id: str,
    markdown: str,
    allow_structural_loss: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace the entire content of a document tab with compiled markdown.

    Use this tool when you need to completely replace a tab's content with new
    markdown. tab_id is required and must identify an existing tab.

    The structural guardrail refuses writes that would silently lose tables,
    images, chips, or footnotes unless allow_structural_loss=true.

    Set dry_run=true to validate and preview without writing.

    Errors:
      UNSUPPORTED_MARKDOWN – markdown contains an unsupported construct
      INVALID_INPUT        – structural guardrail refused
      TAB_NOT_FOUND        – tab_id missing or not in document
      REVISION_CONFLICT    – document changed mid-call; re-read and retry
    """
    service = _get_service()
    try:
        return execute_replace_tab_markdown(
            service=service,
            doc_id=doc_id,
            tab_id=tab_id,
            markdown=markdown,
            allow_structural_loss=allow_structural_loss,
            dry_run=dry_run,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: append_markdown
# ---------------------------------------------------------------------------


@mcp.tool()
def append_markdown(
    doc_id: str,
    tab_id: str,
    markdown: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append compiled markdown at the end of a document tab.

    Use this tool when you need to add new content at the end of a tab without
    disturbing existing content. Inserts before the final trailing newline.

    Set dry_run=true to validate and preview without writing.

    Errors:
      UNSUPPORTED_MARKDOWN – markdown contains an unsupported construct
      TAB_NOT_FOUND        – tab_id not in document
      REVISION_CONFLICT    – document changed mid-call; re-read and retry
    """
    service = _get_service()
    try:
        return execute_append_markdown(
            service=service,
            doc_id=doc_id,
            tab_id=tab_id,
            markdown=markdown,
            dry_run=dry_run,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: insert_image
# ---------------------------------------------------------------------------


@mcp.tool()
def insert_image(
    doc_id: str,
    tab_id: str,
    anchor: str,
    source: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Insert an inline image after the paragraph containing anchor text.

    Use this tool when you need to add an image to a specific location in a
    document tab. The anchor resolves via the same normalization ladder as
    replace_text (exact → curly/straight quotes → NBSP/whitespace → soft-hyphen).
    The image is inserted as an inline object in a new paragraph immediately
    after the paragraph containing the resolved anchor.

    source must be a publicly fetchable URL (http/https). Local file paths are
    rejected with IMAGE_SOURCE_UNSUPPORTED — the Docs API fetches the image from
    the URL directly and cannot access local files.

    Set dry_run=true to preview the resolved anchor position without writing.

    Returns structural evidence: applied, revision_before/after,
    inline_object_confirmed (whether the post-read confirms an inline object
    near the anchor paragraph), audit_logged.

    Errors:
      QUOTE_NOT_FOUND          – anchor not found; nearest candidates listed
      IMAGE_SOURCE_UNSUPPORTED – source is a local path, not a URL
      TAB_NOT_FOUND            – tab_id not in document
      REVISION_CONFLICT        – document changed mid-call; re-read and retry
    """
    service = _get_service()
    try:
        return execute_insert_image(
            service=service,
            doc_id=doc_id,
            tab_id=tab_id,
            anchor=anchor,
            source=source,
            dry_run=dry_run,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Tool: diff_tab_vs_file
# ---------------------------------------------------------------------------


@mcp.tool()
def diff_tab_vs_file(
    doc_id: str,
    tab_id: str,
    file_path: str,
) -> dict[str, Any]:
    """Export a document tab as markdown and diff against a local file.

    Use this tool when you need to compare a Google Doc tab against a local
    markdown file. The server reads the file directly (it runs locally).
    Returns a structured diff with tagged hunks (equal/insert/delete/replace)
    and a unified diff string.

    This is a read-only tool — it makes no changes to the document or file.

    Returns:
      doc_id, tab_id, file_path, revision_id,
      identical (bool), hunks (list of tagged diff blocks),
      unified_diff (unified diff string)

    Errors:
      TAB_NOT_FOUND – tab_id not in document
      INVALID_INPUT – file not found at file_path
    """
    service = _get_service()
    try:
        return execute_diff_tab_vs_file(
            service=service,
            doc_id=doc_id,
            tab_id=tab_id,
            file_path=file_path,
        )
    except VerifyError as exc:
        _raise_tool_error(exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_credentials() -> Any:
    """Acquire OAuth credentials, surfacing auth failure as a typed envelope.

    ``get_credentials`` raises ``VerifyError(AUTH_EXPIRED)`` when no valid token
    exists. Convert it to a ``ToolError`` carrying the envelope dict — the exact
    surfacing path every other typed failure uses — so every tool, including the
    read tools that have no per-tool try/except, returns the AUTH_EXPIRED
    envelope rather than a masked internal error.
    """
    try:
        return get_credentials()
    except VerifyError as exc:
        _raise_tool_error(exc)


def _get_service() -> Any:
    """Return a Docs API service, failing fast if credentials are missing."""
    credentials = _get_credentials()
    return build_docs_service(credentials)


def _raise_tool_error(exc: VerifyError) -> NoReturn:
    """Raise a FastMCP ToolError carrying a JSON error envelope."""
    from fastmcp.exceptions import ToolError

    raise ToolError(json.dumps(exc.envelope.to_dict(), ensure_ascii=False)) from exc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the `verified-googledocs-mcp` command.

    `verified-googledocs-mcp auth`  — run the OAuth flow in the terminal.
    `verified-googledocs-mcp`       — start the stdio MCP server.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        from .auth import run_auth_flow

        run_auth_flow()
        return

    mcp.run()
