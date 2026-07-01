# Architecture

`verified-googledocs-mcp` is a single-user, stdio MCP server. It is deliberately small: a verification kernel that every mutating tool passes through, a thin layer of Google API access, and a markdown converter in each direction.

## Module map

```
src/verified_googledocs_mcp/
  server.py          FastMCP app, tool registration, enforcement middleware
  auth.py            OAuth installed-app flow + token cache (verified-googledocs-mcp auth)
  docs.py            Docs API: read a tab, list tabs, find sections, write
  comments.py        Drive API: comments, replies, verified resolve
  suggestions.py     Extract pending suggested edits from document JSON
  markdown.py        Docs JSON -> markdown (read direction)
  markdown_writer.py markdown -> batchUpdate requests (write direction)
  verify.py          The kernel: locator, error envelope, audit writer
```

A read goes `server -> docs -> markdown`. A verified mutation goes `server -> verify (pre-read, locate, guards) -> docs (batchUpdate) -> verify (post-read, evidence, audit)`.

## The verification kernel

`verify.py` holds the logic that makes a write trustworthy, with no Google API calls of its own so it stays pure and fully unit-testable.

- **`locate(needle, tab_json, expected_matches)`** flattens a tab's JSON into text with an explicit UTF-16 index map, then walks a normalization ladder: exact, curly/straight quotes, non-breaking-space and whitespace runs, soft-hyphen stripping. It reports which rung matched, enforces the match-count guard, refuses matches that cross a paragraph or table-cell boundary, and on zero matches runs a bounded near-miss scan so the caller gets a "did you mean" span.
- **The error envelope** (`ErrorCode`, `ErrorEnvelope`, `VerifyError`) is one typed shape for every failure: `error_code`, `message`, `diagnostics`, `retryable`. Tools surface it to the client so an agent can correct in one round trip instead of parsing prose.
- **The audit writer** appends one JSON line per mutation to `~/.local/state/verified-googledocs-mcp/audit.jsonl`. It is best-effort: it never fails a write, and a failed append is reported in the evidence as `audit_logged: false`.

## The verified-mutation pipeline

```
 args ──► validate ──────────────► INVALID_INPUT / TAB_NOT_FOUND
   │
   ▼
 pre-read tab JSON (revision R1 captured; reused below)
   │
   ▼
 locate(): exact → quotes → NBSP/whitespace → soft-hyphen
   │  ├─ 0 matches ───────────────► ZERO_MATCH + near-miss span
   │  ├─ n ≠ expected_matches ────► MATCH_COUNT_MISMATCH + all spans
   │  └─ crosses paragraph/cell ──► STRUCTURAL_BOUNDARY + spans
   ▼
 dry_run? ──yes──► predicted diff, applied: false, no write
   │ no
   ▼
 batchUpdate(writeControl.requiredRevisionId = R1)
   │  └─ doc moved since R1 ──────► REVISION_CONFLICT
   ▼
 post-read (fresh) ──► evidence { before, after, match_count, rung, R1→R2 }
   │
   ▼
 audit append (best-effort; evidence carries audit_logged: false on failure)
```

## Evidence families

The evidence shape depends on the kind of mutation, but all run through the same pipeline:

- **Text-edit** (`replace_text`): match count, rung, before/after excerpts, revisions.
- **Range/markdown** (`replace_*_markdown`, `append_markdown`): excerpts can't catch a dropped table elsewhere in the range, so verification re-exports the affected range to markdown and structurally diffs it against the input.
- **Structural** (`insert_image`): post-read confirms an inline object now exists at the resolved anchor.
- **Comment-state** (`resolve_comment`, `reply_to_comment`, `add_anchored_comment`): post-write re-query returns the comment's actual final state; a comment still open after a resolve is an error.

## Evidence is the confirmation — don't re-read after a write

Every mutating tool's return payload is itself the proof the write landed: `applied`, `revision_before`/`revision_after`, and for range/markdown writes `structural_match` plus `input_blocks`/`post_blocks` (table equality now includes cell contents, not just row/column counts — see `_blocks_structurally_equal` in `verify.py`). A caller that does a full `read_document` after every write to double-check is paying for a redundant round-trip; the evidence payload already re-read the document and diffed it against the input as part of producing that response. Only re-read when you need the *content* for a subsequent step, not to confirm the write succeeded.

## Enforcement middleware

A FastMCP middleware checks that any tool registered as mutating returns an evidence payload. A mutating tool that returns without evidence is rejected at the boundary, so the "no unverified success" guarantee survives a future tool that forgets to follow the pattern.

## Why these layers are separate

The Docs API addresses content in UTF-16 code units and exposes tab structure, suggestions, and revisions in awkward shapes; the Drive API owns comments. Keeping `docs.py` and `comments.py` thin and pushing every correctness rule into `verify.py` means the hard logic is tested without a network, and the API layers stay easy to reason about. See [`api-notes.md`](api-notes.md) for the specific API behaviors this design works around.
