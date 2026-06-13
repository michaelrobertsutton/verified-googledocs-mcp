# Google API notes

Behaviors of the Docs and Drive APIs that shape this server's design. Items marked _(to verify)_ are pending confirmation against a live document during the first integration with real credentials.

## Confirmed

- **No official Google Docs MCP server exists.** Google's MCP lineup covers Gmail, Drive, Calendar, Chat, and People; Docs editing is available only as a Gemini CLI extension. (Checked June 2026.)
- **Drive `files.export` cannot scope to a single tab.** It exports the whole document, so reading one tab as markdown has to be done from the Docs JSON (`documents.get` with `includeTabsContent=true`), not via Drive export. This is why `markdown.py` converts from the document JSON.
- **`resolved` is read-only on Drive comments.** Patching `comments.update({resolved: true})` is silently ignored, so a naive resolve reports success while the comment stays open. The correct mechanism is `replies.create({action: "resolve"})` — a resolve is a reply with an action, not a field write. `resolve_comment` re-queries after the reply and treats a still-open comment as an error.
- **Docs indices are UTF-16 code units.** `startIndex`/`endIndex` count UTF-16 units, not code points, so an emoji or other astral character is width 2. The locator builds an explicit code-point → UTF-16 map rather than assuming `len(str)`.

## To verify (first live integration)

- **`writeControl.requiredRevisionId` semantics.** Docs revision IDs differ from Drive revision IDs. Confirm that passing the pre-read revision causes `batchUpdate` to reject a write when the document changed in between, and capture the exact error shape so it maps cleanly to `REVISION_CONFLICT`.
- **Comment anchoring.** API-created comments with `quotedFileContent` may render as document-level rather than anchored to the quoted text. If anchoring does not hold, `add_anchored_comment` becomes quote-validated but UI-unanchored, and is documented (and likely renamed) accordingly.
- **Suggestion view and indices.** Confirm that fetching with a suggestions-inclusive view does not shift the indices the locator and edits rely on; pin one `suggestionsViewMode` for all index math.
- **`insertTable` cell offsets.** The cell-index formula in `markdown_writer.py` is derived from the documented request pattern; the API reference does not state offsets explicitly, so validate it against a real table.
- **Nested-list indentation.** Confirm whether `createParagraphBullets` derives nesting from leading tabs or from `updateParagraphStyle` indentation.
