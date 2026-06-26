# Cutover checklist

Moving a workflow from a general-purpose Google Workspace MCP server to
`verified-googledocs-mcp`. The verified server deliberately covers only the
document-editing slice of a large Workspace server — the tools an agent
actually reaches for when reading and writing Docs — and moves the safety
discipline you used to enforce by hand into the protocol.

## 1. Map the tools

The verified server is tab-scoped and intent-named. Most general servers expose
broad, whole-document operations; map them as follows.

| General Workspace tool (old) | Verified tool (new) | What changes |
|---|---|---|
| `readDocument`, export-to-markdown | `read_document` | Requires a `tab_id`; out-of-subset elements come back as labeled placeholders, not silent drops |
| `listTabs` | `list_tabs` | Same shape; call it first — every other tool needs the `tab_id` |
| `findAndReplace` | `replace_text` | Requires `tab_id` and `expected_matches` (default 1); normalization ladder + match guard; no whole-document replace |
| `insertText`, `appendText` | `append_markdown` (or `replace_text`) | Tab-scoped; appends before the trailing newline |
| `replaceDocumentWithMarkdown` | `replace_tab_markdown` | One call **per tab** by design; structural guardrail refuses silent loss of tables/images/chips |
| `replaceRangeWithMarkdown` | `replace_range_markdown` | Range comes from `find_sections` and is revision-stamped; a stale range is rejected (`STALE_RANGE`) |
| `appendMarkdown` | `append_markdown` | Tab-scoped |
| `insertImage` | `insert_image` | Public `http(s)` URL only (local paths rejected); confirms the inline object landed |
| `findSectionsByHeading` / heading search | `find_sections` | Returns ranges stamped with the document revision |
| `listComments` (comments only) | `list_open_items` | Returns open comments **and** pending suggested edits; pass `tab_id` or `include_all_tabs=true` |
| `getComment` / comment thread | `get_comment_thread` | Full reply chain, quoted text, resolved state |
| `addComment` | `add_anchored_comment` | Anchored against a quote that must exist in the tab |
| `replyToComment` | `reply_to_comment` | Re-queries and returns post-state |
| `resolveComment` | `resolve_comment` | Resolves via `replies.create(action='resolve')`, re-queries, and reports `COMMENT_STILL_OPEN` rather than false success |
| (compare doc against a file) | `diff_tab_vs_file` | Read-only structured + unified diff |

No equivalent (out of scope by design): creating documents, Sheets, Gmail,
Calendar, Drive file management, accepting/rejecting suggestions (not possible
through the Docs API — see the README's Limitations).

## 2. Switch the client config

1. Register `verified-googledocs-mcp` with your MCP client (see the README's
   Setup section) and run `verified-googledocs-mcp auth` once.
2. Confirm the new tools appear (e.g. `list_tabs`, `replace_text`) and a read
   against a known doc returns evidence.
3. **Remove the old Workspace server** from the same client config so the agent
   does not see two overlapping `findAndReplace`-style tools and pick the
   unverified one. If you still need the old server for Sheets/Gmail/Drive, keep
   it but be aware both expose document tools; prefer scoping by which client
   profile loads which server.

## 3. Retire the manual workarounds

These instructions are no longer needed once a workflow is on the verified
server — the protocol enforces them:

- "Always scope find/replace to a single tab." → `tab_id` is mandatory.
- "Retry the search with curly quotes / non-breaking spaces normalized." → the
  normalization ladder does this and reports which rung matched.
- "Re-read the document after every write to confirm it landed." → every
  mutating tool re-reads and returns before/after evidence.
- "Don't trust a resolve-comment success." → `resolve_comment` re-queries and
  fails loud with `COMMENT_STILL_OPEN`.
- "Set `expected_matches` so a repeated sentence isn't collapsed." → the match
  guard refuses the write and returns every location.

## 4. Verify the cutover

- A representative edit returns `applied: true` with matching `before`/`after`
  excerpts and a changed `revision_after`.
- A deliberately-wrong find string returns a typed `ZERO_MATCH` (or
  `MATCH_COUNT_MISMATCH`) envelope, not a silent no-op.
- The old server no longer appears in the client's tool list.
