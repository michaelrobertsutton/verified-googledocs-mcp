# Fixture session runbook

A one-time session, run once OAuth credentials exist, that produces the
canonical test fixtures and answers the open Google API behavior questions in
[`api-notes.md`](api-notes.md). Everything downstream (the regression tests,
the live smoke suite) depends on the artifacts this session produces.

Prerequisite: the Google Cloud + OAuth setup (repo issue #7). Then `verified-googledocs-mcp auth`.

## 1. Seed the scratch document

Create one Google Doc with **multiple tabs** and seed it with the historical
failure cases so a single fixture exercises them all:

- [ ] Tab 1 with normal prose, plus a sentence that is **deliberately duplicated** (appears twice verbatim) for the match-count guard.
- [ ] A second tab, so tab-scoping is exercised (a unique string also present in tab 1 proves edits don't leak across tabs).
- [ ] **Curly quotes**, a **non-breaking space**, and a **soft hyphen** somewhere in the text, for the normalization ladder.
- [ ] The **UTF-16 hazard set**: a plain emoji, a ZWJ emoji sequence (for example a family emoji), a combining-mark character, and a short right-to-left phrase. These pin the UTF-16 index arithmetic.
- [ ] An **open comment thread** with at least one reply.
- [ ] A **pending suggested edit** (turn on Suggesting mode, make one insertion and one deletion).

Note the document ID from its URL.

## 2. Capture the JSON fixture

```bash
uv run python scripts/capture_fixtures.py <DOCUMENT_ID> --name scratch_multitab
```

This writes `tests/unit/fixtures/live_capture/scratch_multitab.json` (full tab
content, inline suggestions) and prints the revision ID and tab count. Commit
the JSON; it becomes a deterministic offline fixture.

## 3. Run the three spikes

Record findings in [`api-notes.md`](api-notes.md) under "To verify," moving each
item to "Confirmed" with the answer.

**a. Comment anchoring.** Create a comment through the Drive API with
`quotedFileContent` set to a phrase in the doc. Open the doc UI and check
whether the comment anchors to that phrase or renders document-level. If it
does not anchor, `add_anchored_comment` becomes quote-validated but
UI-unanchored, and is renamed `add_comment`.

**b. Revision precondition.** Capture the document's `revisionId`. Edit the doc
in the UI. Then send a trivial `batchUpdate` with
`writeControl.requiredRevisionId` set to the captured (now stale) revision.
Confirm the API rejects it, and record the exact error shape so it maps cleanly
to `REVISION_CONFLICT`.

**c. Suggestion view and indices.** Compare element `startIndex` values for the
same text with `suggestionsViewMode=SUGGESTIONS_INLINE` versus
`PREVIEW_WITHOUT_SUGGESTIONS`. Confirm the pinned view used by the locator does
not shift indices out from under an edit.

## 4. Validate the markdown-writer assumptions

Two `markdown_writer.py` formulas were derived from the API docs and need a real
table to confirm (see `api-notes.md`):

- [ ] The `insertTable` cell-index offset formula, against a doc with a table.
- [ ] Whether `createParagraphBullets` nesting comes from leading tabs or from `updateParagraphStyle` indentation.

## 5. Wire the live smoke suite

With the fixture committed and the spikes resolved, add the `tests/live/` smoke
tests: one full comment-resolution cycle and one markdown sync round trip
against the scratch doc, each asserting zero manual verification steps. These
are the acceptance workflows from the PRD; they run locally with credentials,
not in CI.
