# PRD: verified-docs-mcp

A Google Docs MCP server with verified writes.

| | |
|---|---|
| **Author** | Michael Sutton |
| **Status** | Draft |
| **Date** | 2026-06-12 |
| **Working name** | `verified-docs-mcp` (final name TBD) |

## 1. Summary

`verified-docs-mcp` is a small, single-user MCP (Model Context Protocol) server for Google Docs, built for agent-driven document workflows: resolving reviewer comments, syncing a Doc against local markdown, and applying targeted edits to multi-tab documents. It replaces a large third-party Google Workspace MCP server whose write and comment operations cannot be trusted without manual re-verification.

The core idea is a **verified-write contract**: every mutating tool re-reads the affected content server-side and returns post-state evidence (match counts, before/after excerpts, confirmed comment state). The agent never has to follow up a write with a read to find out whether it worked, and a tool can never report success for an operation that did not land.

## 2. Background and problem

My document workflows run through an MCP server that wraps the full Google Workspace surface (~150 tools across Docs, Drive, Sheets, Gmail, and Calendar). In months of daily use, a consistent set of failure modes emerged:

1. **`findAndReplace` spans all tabs by default.** On multi-tab documents, an edit intended for one tab silently lands in every tab unless the caller remembers to scope it.
2. **Zero-match failures on invisible character differences.** Searches fail because the document contains curly quotes, non-breaking spaces, or other Unicode the caller's plain-text query does not, and the tool returns "0 matches" with no diagnosis.
3. **Duplicate-sentence collapse.** A replace intended for one occurrence can hit several, deleting legitimately repeated sentences with no warning.
4. **`resolveComment` returns success while the comment stays open.** The return value cannot be trusted; the only reliable check is re-querying the comment or looking at the document UI.
5. **`listComments` omits suggested edits.** Suggested insertions and deletions are only visible by parsing the raw document JSON (`suggestedInsertionIds` / `suggestedDeletionIds`), so "how many open items are on this doc" requires two different access paths.
6. **Unverified writes produce garbled artifacts.** Merge operations have injected corrupted text (fused words, mangled phrases) that was only caught later by a human reading the document.
7. **Tool sprawl.** Of ~150 tools loaded into every agent session, my workflows use 14. The rest are context overhead and a source of wrong-tool selection.

Every one of these currently has a *procedural* mitigation: prompt instructions telling the agent to scope to a tab ID, retry with normalized quotes, re-read after every replace, and never trust a resolve result. Those instructions work but live in the wrong layer. They have to be repeated across workflow definitions, they drift, and a single forgotten step reintroduces silent corruption. This project moves the discipline into the server, where it is deterministic.

## 3. Goals

1. **Complete replacement** of the current server for my document workflows. Every tool those workflows call today has an equivalent here (parity map in §6), and the old server can be removed from client configuration at cutover.
2. **Verified mutations.** No mutating tool returns a bare success flag. Every write returns evidence read back from the document after the operation.
3. **Tab-scoped by default.** Operations on multi-tab documents require an explicit tab target. There is no "all tabs" default.
4. **Small, prescriptive tool surface.** Roughly 14 tools, each with a description that states *when* to use it, not just what it does.
5. **Testable.** Every documented failure mode of the old server has a named regression test that reproduces it and proves this server handles it.

## 4. Non-goals

- **Gmail, Calendar, and Sheets coverage.** Unused by the target workflows. Out of scope permanently; this is a Docs server.
- **Accepting or rejecting suggested edits.** The Google Docs API does not support resolving suggestions programmatically. This server lists them (closing the visibility gap); acting on them remains a human step in the Docs UI. Documented as a known limitation, not a planned feature.
- **Multi-user or hosted deployment.** Single user, local process, stdio transport. No HTTP transport, no shared auth.
- **Document creation and formatting authoring** (tables, styles, page setup) beyond what markdown conversion requires. The workflows edit existing documents; they do not build documents from scratch.

## 5. Design principles

- **Evidence over acknowledgment.** A return value is a claim about the document's state after the call, backed by a server-side re-read, never an echo of the API response.
- **Fail loud, fail diagnosed.** A zero-match search returns what it tried (each normalization pass) and the nearest near-miss text it found, so the caller can correct in one round trip instead of guessing.
- **One tool per intent.** No general-purpose batch escape hatch. If a workflow needs a new operation, it gets a new named, verified tool.
- **Descriptions carry trigger conditions.** Each tool description says when to reach for it ("use this instead of a plain find/replace whenever the document has more than one tab"), because agent clients select tools from descriptions.

## 6. Tool catalog and parity map

Fourteen tools replace the fourteen in active use. Names are working names.

### Reading and structure

| Tool | Replaces | Notes |
|---|---|---|
| `read_document(doc_id, tab_id?, format)` | `readDocument` | `format`: `markdown` (default) or `structured` (positions, style runs) |
| `list_tabs(doc_id)` | `listTabs` | Returns tab IDs, titles, nesting |
| `find_sections(doc_id, heading, tab_id?)` | `findSectionsByHeading` | Returns heading matches with ranges for scoped edits |

### Editing (all verified, all tab-scoped)

| Tool | Replaces | Notes |
|---|---|---|
| `replace_text(doc_id, tab_id, find, replace, expected_matches?)` | `findAndReplace` | See verified-write contract below |
| `replace_range_markdown(doc_id, tab_id, range, markdown)` | `replaceRangeWithMarkdown` | Range from `find_sections` or explicit indices |
| `replace_tab_markdown(doc_id, tab_id, markdown)` | `replaceDocumentWithMarkdown` | Whole-tab replacement; refuses to run without a tab ID |
| `append_markdown(doc_id, tab_id, markdown)` | `appendMarkdown` | |
| `insert_image(doc_id, tab_id, anchor, source)` | `insertImage` | Anchor is quoted text or a section heading |

### Comments and suggestions

| Tool | Replaces | Notes |
|---|---|---|
| `list_open_items(doc_id, tab_id?, include_all_tabs?)` | `listComments` + manual doc-JSON parsing | One call returns open comments **and** suggested edits; pass `tab_id` to scope suggestions to one tab, or `include_all_tabs=true` for every tab |
| `get_comment_thread(comment_id)` | `getComment` | Full reply chain |
| `add_anchored_comment(doc_id, tab_id, quote, body)` | `addComment` | If the quote is not found, errors with the nearest candidate anchors |
| `reply_to_comment(comment_id, body)` | `replyToComment` | |
| `resolve_comment(comment_id)` | `resolveComment` | Resolves, re-queries, returns the comment's actual final state; an unresolved comment is an **error**, never a success |

### Sync

| Tool | Replaces | Notes |
|---|---|---|
| `diff_tab_vs_file(doc_id, tab_id, file_path)` | (new; currently done ad hoc by the agent) | Exports the tab as markdown and returns a structured diff against a local file. Server runs locally, so it reads the file directly |

### The verified-write contract

Every mutating tool returns:

```json
{
  "applied": true,
  "match_count": 1,
  "before": "...±200 chars of pre-edit context...",
  "after": "...the same span, re-read from the document after the edit...",
  "revision_id": "..."
}
```

Specific behaviors for `replace_text`, since it covers the worst historical failures:

- **Normalization ladder on zero matches.** Exact match first; then curly/straight quote equivalence; then non-breaking-space and whitespace-run equivalence; then soft-hyphen stripping. The response reports which rung matched. If none match, the error includes the closest near-miss span found.
- **`expected_matches` guard.** Defaults to 1. If the actual match count differs, the tool makes **no edit** and returns the locations of all matches, preventing the duplicate-sentence collapse failure.
- **Mandatory `tab_id`.** There is no whole-document replace. Editing every tab means calling the tool once per tab, deliberately.
- **Server-side re-read.** The `after` excerpt is fetched fresh, post-write. A garbled merge shows up in the return value immediately, not in a human review days later.

## 7. Architecture

- **Language / framework:** Python 3.12+, [FastMCP 3.x](https://gofastmcp.com) (`fastmcp` on PyPI). FastMCP 3 is GA (Feb 2026), maintained by the Prefect team, and provides the decorator-based tool API, schema generation from type hints, an in-memory test client, hot reload (`fastmcp dev`), and optional OpenTelemetry instrumentation.
- **Google APIs:** `google-api-python-client` + `google-auth-oauthlib`. Document content and edits via the Docs API (with `includeTabsContent` for multi-tab documents; suggestions read via `suggestionsViewMode`). Comments and replies via the Drive API v3 (`comments`, `replies`; resolution via a reply with `action: "resolve"`).
- **Transport:** stdio, registered in the MCP client's configuration as a local server. Single user.
- **Auth:** OAuth installed-app flow on first run; token cached at `~/.config/verified-docs-mcp/token.json` with automatic refresh. Scopes: `documents` and `drive` (comments require Drive scope). Personal OAuth client in an unverified-app state is acceptable for single-user use.
- **Markdown conversion:** the highest-risk component. Reads can lean on Drive's native markdown export where fidelity suffices, with a structured Docs-JSON-to-markdown fallback for cases it mishandles. Writes (markdown to Docs requests) are a custom layer with a fixed supported subset: headings, bold/italic, lists, tables, links. Anything outside the subset is rejected loudly rather than approximated silently.
- **Audit log:** every mutation appends a line to a local JSONL log (timestamp, doc, tab, tool, before/after excerpts). Cheap insurance and a debugging trail.

```
verified-docs-mcp/
  src/verified_docs_mcp/
    server.py          # FastMCP app, tool registration
    auth.py            # OAuth flow + token cache
    docs.py            # Docs API: read, structure, edits
    comments.py        # Drive API: comments, replies, resolve
    suggestions.py     # doc-JSON suggested-edit extraction
    markdown.py        # conversion layer (both directions)
    verify.py          # re-read + excerpt machinery, normalization ladder
  tests/
    unit/              # in-memory FastMCP client, fixture-driven
    live/              # smoke suite against a scratch document
  PRD.md
```

## 8. Testing

- **Unit tests** run against the in-memory FastMCP client with recorded Google API fixtures. No network.
- **A live smoke suite** runs against a dedicated scratch document seeded with the historical failure cases: multiple tabs, curly-quoted text, a deliberately duplicated sentence, an open comment thread, and a pending suggested edit.
- **Named regression tests**, one per documented failure mode from §2 (e.g., `test_resolve_reports_failure_when_comment_stays_open`, `test_replace_refuses_on_unexpected_match_count`, `test_list_open_items_includes_suggestions`). The old server's bugs are this server's acceptance criteria.

## 9. Migration and cutover

1. **Side-by-side.** Register this server under its own name alongside the incumbent. Nothing breaks while it is incomplete.
2. **Per-workflow cutover.** Update each workflow definition to the new tool names as its tools reach parity, using the §6 map. The comment-resolution loop and the doc-sync loop are the two acceptance workflows.
3. **Acceptance gate.** One full comment-resolution cycle and one full markdown-sync round trip on a real multi-tab document, completed with zero manual verification steps and zero discrepancies on after-the-fact human review.
4. **Decommission.** Remove the old server from client configuration. Tool count loaded per session drops from ~150 to 14.

## 10. Success metrics

- Zero unverified mutations: every write in the audit log carries post-state evidence.
- Zero false resolves: no comment reported resolved that a re-query shows open.
- Zero-match incidents requiring human diagnosis drop to zero (the normalization ladder or the near-miss diagnostic resolves them in one round trip).
- Suggested edits visible in the same call as comments, always.
- Old server fully removed from configuration.

## 11. Risks and open questions

| Risk | Mitigation |
|---|---|
| Markdown round-trip fidelity (tables, nested lists, inline styles) | Fixed supported subset, loud rejection outside it; Drive native export as the read path where possible |
| Docs API tab addressing (segment IDs) is fiddly | Isolate in `docs.py`; cover with structured fixtures early (M1) |
| Suggestions cannot be accepted via API | Documented non-goal; `list_open_items` closes the visibility gap, action stays in the UI |
| Google API rate limits during agent bursts | Per-tool retry with backoff; mutations are low-volume by nature |
| OAuth consent screen friction (unverified app) | Acceptable for single-user; document the setup in the README |

Open: final project name; whether `diff_tab_vs_file` should also emit a patch format the agent can apply back with `replace_range_markdown`.

## 12. Milestones

| # | Milestone | Definition of done |
|---|---|---|
| M1 | Auth + reads | OAuth flow works; `read_document`, `list_tabs`, `find_sections` round-trip a real multi-tab doc |
| M2 | Verified replace | `replace_text` passes all §8 regression tests including normalization ladder and match guard |
| M3 | Comments | `list_open_items` (incl. suggestions), thread read, anchored add, reply, verified resolve |
| M4 | Markdown writes + diff | `replace_range_markdown`, `replace_tab_markdown`, `append_markdown`, `insert_image`, `diff_tab_vs_file` |
| M5 | Cutover | §9 acceptance gate passed; old server removed |
