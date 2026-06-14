# Google API notes

Behaviors of the Docs and Drive APIs that shape this server's design. Items marked _(to verify)_ are pending confirmation against a live document during the first integration with real credentials.

## Confirmed

- **No official Google Docs MCP server exists.** Google's MCP lineup covers Gmail, Drive, Calendar, Chat, and People; Docs editing is available only as a Gemini CLI extension. (Checked June 2026.)
- **Drive `files.export` cannot scope to a single tab.** It exports the whole document, so reading one tab as markdown has to be done from the Docs JSON (`documents.get` with `includeTabsContent=true`), not via Drive export. This is why `markdown.py` converts from the document JSON.
- **`resolved` is read-only on Drive comments.** Patching `comments.update({resolved: true})` is silently ignored, so a naive resolve reports success while the comment stays open. The correct mechanism is `replies.create({action: "resolve"})` — a resolve is a reply with an action, not a field write. `resolve_comment` re-queries after the reply and treats a still-open comment as an error.
- **Docs indices are UTF-16 code units.** `startIndex`/`endIndex` count UTF-16 units, not code points, so an emoji or other astral character is width 2. The locator builds an explicit code-point → UTF-16 map rather than assuming `len(str)`.

## Confirmed (live integration, June 2026)

- **`writeControl.requiredRevisionId` rejects stale revisions with HTTP 400 `INVALID_ARGUMENT`.** The error message names the stale ID explicitly: `"The required revision ID '...' does not match the latest revision."` This is not a 409 ABORTED — map it to `REVISION_CONFLICT` by status string, not HTTP code. Capture the `error.status` field, not just `error.code`.
- **API-created comments with `quotedFileContent` render document-level, not anchored.** Drive `comments.create` with `quotedFileContent` returns `anchor: null` — the quoted text is stored but does not resolve to a positional anchor in the document UI. `add_anchored_comment` is therefore a misnomer: the comment is quote-validated (the text is recorded) but UI-unanchored. See #4 for renaming.
- **Indices are stable across `suggestionsViewMode`.** `PREVIEW_WITHOUT_SUGGESTIONS` and `SUGGESTIONS_INLINE` return identical `endIndex` values on a document without pending suggestions. Pin `PREVIEW_WITHOUT_SUGGESTIONS` for all index math — consistent, and not inflated by suggestion spans.
- **Suggested text insertions cannot be created via the REST API.** `InsertTextRequest` does not accept `suggestedInsertionIds` — the field is silently rejected (HTTP 400). Suggested edits must be created in the Docs UI. The document model exposes `suggestedInsertionIds`/`suggestedDeletionIds` as read fields on `TextRun` and `Paragraph` elements, but there is no write path via `batchUpdate`.
- **Every write `Location`/`Range` must carry a `tabId` on a tabbed doc (#48).** Indices are per-tab (each tab body is its own segment indexed from 1). A `batchUpdate` request whose `location`/`range` omits `tabId` resolves against the document's *first* tab segment — so a write aimed at a secondary tab either errors (`"Index N must be less than the end index of the referenced segment, M"` where M is the first tab's end) or lands in the wrong tab, leaving the target empty. Writes to the first tab work by accident because it is the default segment. `compile_markdown` emitted `location`/`range` without a `tabId`; `_stamp_tab_id` now stamps the target tab onto every location/range before `batchUpdate`. Confirmed live (2026-06-14) on a natively-present secondary tab — so this affects *all* secondary tabs, not only externally-created ones. The read path was verified correct (it already resolves the requested `tab_id`).

## To verify

- **`insertTable` cell offsets.** The cell-index formula in `markdown_writer.py` is derived from the documented request pattern; the API reference does not state offsets explicitly, so validate it against a real table.
- **Nested-list indentation.** Confirm whether `createParagraphBullets` derives nesting from leading tabs or from `updateParagraphStyle` indentation.
