# Live acceptance suite

The pre-release gate (issue #23). Exercises the tool surface, error codes, and
both PRD acceptance workflows against the **real** Google Docs and Drive APIs —
not against recorded fixtures. The offline unit suite proves the logic; this
suite proves the verified-write contract survives contact with real revision
IDs, real index arithmetic, and real Drive comment semantics.

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
- **Local file reads are isolated.** The same fixture points
  `VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS` at the per-test tmp dir, so sync
  tests can diff temporary markdown files without exposing broader local paths.
- **Two error codes can't occur naturally**, so they use controlled
  simulations where the real API still produces the rejection/re-query:
  `REVISION_CONFLICT` (stale `requiredRevisionId`) and `COMMENT_STILL_OPEN`
  (stubbed resolve action). `AUTH_EXPIRED` is triggered for real by pointing the
  token path at a missing file.

## Layout

| File | Section |
|---|---|
| `conftest.py` | quarantine flag, credential guard, scratch copies, audit isolation |
| `test_reads.py` | §1 read_document, list_tabs, find_sections |
| `test_replace_text.py` | §2 normalization ladder, match guard, UTF-16, dry-run, revision precondition, evidence |
| `test_format_text.py` | style edit: bold applied + structured-run confirmation, idempotent re-run, zero/multi-match refusal, dry-run, merged-cell table, comment-driven revision bump, INVALID_INPUT/TAB_NOT_FOUND |
| `test_markdown_writes.py` | §3 range/tab/append markdown, insert_image, UNSUPPORTED_MARKDOWN, STALE_RANGE |
| `test_comments.py` | §4 list_open_items, thread, anchored comment, reply, resolve |
| `test_sync.py` | §5 diff_tab_vs_file |
| `test_cross_cutting.py` | §6 middleware, audit log, auth, input validation, unknown tab |
| `test_workflows.py` | §8 comment-resolution cycle, markdown sync round trip |

## Status

**Initial-release gate met:** `pytest --run-live` → 57 passed, 0 xfailed,
0 skipped. Every divergence this pass found (#28, #29, #30, #31, #36, #37,
#38, #43) has been fixed. See `docs/acceptance-report.md` for the baseline
matrix; rerun this suite before release when the tool surface or error-code
matrix changes.

**2026-08-09**, after adding `format_text` (#63): full run → 89 passed,
2 failed. Both failures were pre-existing fixture drift on the canonical doc,
unrelated to `format_text` — the NBSP rung test found `exact` instead of
`nbsp_whitespace_runs`, and the suggestions test could not find the seeded
suggestions. All 9 `test_format_text.py` tests passed.

**2026-08-09, both addressed before the v0.2.0 release.** The two failure
modes are not the same kind of problem, so they got different fixes:

- **NBSP** — an NBSP *is* writable through the REST API, so the suite no
  longer depends on the character surviving. `conftest.py`'s
  `_ensure_nbsp_hazard` checks the hazard paragraph when the session starts
  and restores U+00A0 if it has been flattened. Verified by deliberately
  re-flattening the character and confirming a run repaired it and passed.
  This drift had already happened twice (2026-07-22, 2026-08-09).
- **Suggestions** — genuinely not automatable. `documents.batchUpdate` takes
  only `requests` + `writeControl`, and none of its 40 request types create a
  suggestion; every `Suggested*` / `*SuggestionState` schema is a read shape
  (checked against discovery revision 20260803). Seeding stays manual, so the
  test now **skips with an actionable message** when the fixture carries no
  suggestions, instead of failing the release gate for a fixture defect. If
  suggestions *are* present but the seeded ids are missing, it still fails —
  that case is a real regression or stale constants. Parsing is covered
  offline by `tests/unit/test_suggestions.py`.

Baseline after both fixes: **90 passed, 1 skipped** (the skip is the
suggestions test above, and stays until the fixture is re-seeded by hand).
That is the expected shape of a green run — a *failure* here is a real signal.

**Invocation note:** run this suite as `pytest tests/live --run-live`. A bare
`pytest --run-live` fails with "unrecognized arguments" — the flag is
registered in `tests/live/conftest.py`, which pytest loads too late for
argument parsing unless the path is passed explicitly.
