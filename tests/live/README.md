# Live acceptance suite

The pre-release gate (issue #23). Exercises all 14 tools, all 12 error codes,
and both PRD acceptance workflows against the **real** Google Docs and Drive
APIs — not against recorded fixtures. The offline unit suite proves the logic;
this suite proves the verified-write contract survives contact with real
revision IDs, real index arithmetic, and real Drive comment semantics.

## Running it

```bash
pytest --run-live            # whole live suite
pytest tests/live --run-live -q
```

Requirements:

- OAuth credentials at `~/.config/verified-googledocs-mcp/{credentials.json,token.json}`
  (run `verified-googledocs-mcp auth` first — see the project README).
- Network access to Google.

Without `--run-live` every test is **skipped**, so a plain `pytest` (and CI)
never touches the network. Without credentials, the suite skips itself even
with `--run-live`.

Override the fixture document with `VERIFIED_GOOGLEDOCS_MCP_TEST_DOC=<doc_id>`.

## How it stays safe

- **The canonical fixture is never mutated.** Read-only checks (suggestions,
  the seeded comment thread) run against it directly. Every mutating test runs
  against a fresh `files.copy` of it (the `scratch_doc` fixture), which is
  hard-deleted on teardown. A copy preserves tab structure and the hazard text
  but not comments/suggestions, so comment-mutation tests create their own.
- **The audit log is isolated.** An autouse fixture points `XDG_STATE_HOME` at a
  per-test tmp dir, so the suite never writes to the real audit log and "one
  line per mutation" can be asserted cleanly.
- **Three error codes can't occur naturally**, so they use controlled
  simulations where the real API still produces the rejection/re-query:
  `REVISION_CONFLICT` (stale `requiredRevisionId`), `COMMENT_STILL_OPEN`
  (stubbed resolve action), and the `AUTH_EXPIRED` path.

## Layout

| File | Section |
|---|---|
| `conftest.py` | quarantine flag, credential guard, scratch copies, audit isolation |
| `test_reads.py` | §1 read_document, list_tabs, find_sections |
| `test_replace_text.py` | §2 normalization ladder, match guard, UTF-16, dry-run, revision precondition, evidence |
| `test_markdown_writes.py` | §3 range/tab/append markdown, insert_image, UNSUPPORTED_MARKDOWN, STALE_RANGE |
| `test_comments.py` | §4 list_open_items, thread, anchored comment, reply, resolve |
| `test_sync.py` | §5 diff_tab_vs_file |
| `test_cross_cutting.py` | §6 middleware, audit log, auth, input validation, unknown tab |
| `test_workflows.py` | §8 comment-resolution cycle, markdown sync round trip |

## Known divergences

Some assertions are `xfail` against filed follow-up issues — they document
defects found by this pass and flip to passing once fixed: **#28** (suggestions
defeat the duplicate-collapse guard), **#29** (AUTH_EXPIRED unwired), **#30**
(audit_excerpts has no surface), **#36/#37/#38** (markdown-write
verification/output), **#31** (fixture lacks a heading + nested tab). See
`docs/acceptance-report.md` for the full matrix.
