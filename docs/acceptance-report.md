# Live acceptance report (issue #23)

The pre-release gate. Every one of the 14 tools, every one of the 12 error
codes, and both PRD acceptance workflows, exercised against the real Google
Docs and Drive APIs on a seeded multi-tab document — not against recorded
fixtures.

| | |
|---|---|
| **Fixture document** | `1Zm_6bAwA7UH1DKkGVL3kg9XcQ6rIZHmUFcQPTcoJb6Y` (seeded in #1) |
| **Suite** | `tests/live/` — driven by the in-memory FastMCP `Client(mcp)`, so tool registration and the evidence-enforcement middleware run on the path out to the live API |
| **How to run** | `pytest --run-live` (requires OAuth credentials; never runs in CI) |
| **Result** | **49 passed, 1 skipped, 7 xfailed** |

Status legend: **live** (proven directly against the API) · **sim** (proven via
a controlled simulation in which the real API still produces the
rejection/re-query) · **diverged** (a filed defect; the assertion is quarantined
`xfail` and flips to passing once fixed) · **fixture-gap** (blocked on missing
fixture substrate, filed).

## All 14 tools exercised live

| Tool | Status | Test(s) |
|---|---|---|
| `read_document` (markdown + structured) | live | `test_reads.py::TestReadDocument` |
| `list_tabs` | live | `test_reads.py::TestListTabs` |
| `find_sections` | live¹ | `test_reads.py::TestFindSections` |
| `replace_text` | live | `test_replace_text.py` (whole module) |
| `replace_range_markdown` | live² | `test_markdown_writes.py::TestReplaceRangeMarkdown` |
| `replace_tab_markdown` | live² | `test_markdown_writes.py::TestReplaceTabMarkdown` |
| `append_markdown` | diverged (#37) | `test_markdown_writes.py::TestAppendMarkdown` |
| `insert_image` | live³ | `test_markdown_writes.py::TestInsertImage` |
| `list_open_items` | live | `test_comments.py::TestListOpenItems` |
| `get_comment_thread` | live | `test_comments.py::TestGetCommentThread` |
| `add_anchored_comment` | live | `test_comments.py::TestAddAnchoredComment` |
| `reply_to_comment` | live | `test_comments.py::TestReplyToComment` |
| `resolve_comment` | live | `test_comments.py::TestResolveComment` |
| `diff_tab_vs_file` | live | `test_sync.py::TestDiffTabVsFile` |

¹ Uses a seeded heading on a scratch copy — the canonical fixture has no headings (#31).
² The *write* is correct and confirmed by re-read; the `structural_match` *evidence* false-negatives (#36).
³ The image *inserts* correctly; the `inline_object_confirmed` *evidence* false-negatives (#38).

## §7 error-code matrix — all 12 codes

| Code | Status | How triggered | Test |
|---|---|---|---|
| `ZERO_MATCH` | live | find string absent; near-miss span returned | `test_replace_text.py::TestZeroMatch` |
| `MATCH_COUNT_MISMATCH` | live | duplicate sentence (2 matches), `expected_matches=1`, no edit, all spans returned | `test_replace_text.py::TestMatchCountGuard::test_duplicate_sentence_refused_on_copy` |
| `REVISION_CONFLICT` | sim | stale `requiredRevisionId` after an out-of-band edit → real Docs 400 | `test_replace_text.py::TestRevisionConflict` |
| `STALE_RANGE` | live | `find_sections` range reused after the doc moved on | `test_markdown_writes.py::...test_stale_range_after_doc_moves_on` |
| `TAB_NOT_FOUND` | live | bad `tab_id`; available tabs listed | `test_replace_text.py::...test_unknown_tab_is_tab_not_found` |
| `STRUCTURAL_BOUNDARY` | live | find string crossing a paragraph boundary | `test_replace_text.py::...test_match_crossing_paragraph_boundary...` |
| `UNSUPPORTED_MARKDOWN` | live | blockquote (out of subset); construct named | `test_markdown_writes.py::TestUnsupportedMarkdown` |
| `QUOTE_NOT_FOUND` | live | absent anchor; nearest candidates returned | `test_comments.py::...test_missing_quote_is_quote_not_found` |
| `COMMENT_STILL_OPEN` | sim | resolve action stubbed to a no-op → real re-query sees it still open | `test_comments.py::...test_comment_still_open_failure_path` |
| `INVALID_INPUT` | live | empty find / find==replace / missing diff file | `test_replace_text.py`, `test_sync.py`, `test_cross_cutting.py` |
| `IMAGE_SOURCE_UNSUPPORTED` | live | local file path as image source | `test_markdown_writes.py::...test_local_path_is_image_source_unsupported` |
| `AUTH_EXPIRED` | **diverged (#29)** | not constructable — auth fails with a bare `RuntimeError` | `test_cross_cutting.py::TestAuth` (xfail) |

**11 of 12 codes triggered live** (9 directly, 2 via controlled simulation).
`AUTH_EXPIRED` is the lone exception — it is defined but never raised (#29).

## §1–§8 checklist

**§1 Reads & structure** — `read_document` markdown + structured spans line up with visible text; `list_tabs` ids/titles/index match; `find_sections` stamps ranges with the live `revisionId`. *Nested-tab item skipped — fixture-gap #31.*

**§2 Verified text edits** — all four normalization rungs reported correctly (`exact`, `curly_straight_quotes`, `nbsp_whitespace_runs`, `soft_hyphen_strip`); `ZERO_MATCH` + near-miss; match-count guard refuses the duplicate and makes no edit; tab scoping leaves the other tab byte-for-byte identical; an edit positioned after emoji / ZWJ / combining-mark / RTL text lands on the intended span with the hazard characters intact; dry-run predicts without writing (revision unchanged); `REVISION_CONFLICT` (sim); evidence shape verified (before/after are server re-reads, `revision_before` ≠ `revision_after`, `match_count` + `rung` present, `audit_logged: true`).

**§3 Markdown writes** — `replace_range_markdown` replaces a `find_sections` range, then `STALE_RANGE` on reuse; `replace_tab_markdown` whole-tab replace lands the new structure; `append_markdown` runs; `insert_image` succeeds at a quoted anchor and at a heading, local path → `IMAGE_SOURCE_UNSUPPORTED`; out-of-subset markdown → `UNSUPPORTED_MARKDOWN` naming the construct; `STRUCTURAL_BOUNDARY` covered in §2. *Verification/output divergences: #36 (structural_match), #37 (append fusion), #38 (image confirm).*

**§4 Comments & suggestions** — `list_open_items` returns open comments **and** pending suggestions in one call; `get_comment_thread` returns the full reply chain; `add_anchored_comment` is quote-validated (doc-level rendering per the #1 spike), absent quote → `QUOTE_NOT_FOUND` with candidates; `reply_to_comment` appears on re-query; **`resolve_comment` actually closes the comment** (re-query confirms `resolved: true`, closed via the `action: resolve` reply — the incumbent-bug regression), and the failure path → `COMMENT_STILL_OPEN` (sim).

**§5 Sync** — `diff_tab_vs_file` correct in both directions (file ahead → insert; doc ahead → delete), identical when matched, `INVALID_INPUT` on missing file, `TAB_NOT_FOUND` on bad tab.

**§6 Cross-cutting** — enforcement middleware accepts real evidence-bearing mutations and passes typed errors through (no misfire); audit log writes exactly one JSONL line per mutation, the file is `0600` under a `0700` dir; `audit_excerpts=false` redaction logic verified directly (no live tool surface — #30); auth fails fast (as `AUTH_EXPIRED` — #29); `INVALID_INPUT` on contradictory args; `TAB_NOT_FOUND` lists available tabs.

**§8 Acceptance workflows (zero manual steps)** — full comment-resolution cycle (locate → comment → reply → resolve → confirm closed) and markdown sync round trip (read → diff → apply → re-read → confirm convergence), both green with no human verification step.

## Divergences filed (do not silently absorb)

| # | Severity | Summary |
|---|---|---|
| [#28](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/28) | **P0** | `fetch_document` doesn't pin `suggestionsViewMode`; on a doc with suggestions the duplicate-collapse guard resolves to 1 match and **would write** |
| [#29](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/29) | — | `AUTH_EXPIRED` defined but never raised; auth failure is a bare `RuntimeError` |
| [#30](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/30) | — | `audit_excerpts=false` redaction has no tool/env/config surface |
| [#31](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/31) | — | Fixture lacks heading-styled paragraphs and a nested tab (Docs API can't create tabs) |
| [#36](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/36) | — | `to_markdown` omits blank lines between blocks → `structural_match` false negatives (write is correct) |
| [#37](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/37) | — | `append_markdown` fuses the appended block into the trailing paragraph (garbled output) |
| [#38](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/38) | — | `insert_image` `inline_object_confirmed` false negative (verifier checks the wrong paragraph) |

## Definition of done

- ✅ Every tool exercised live.
- ✅ 11/12 error codes triggered live with their shape recorded; `AUTH_EXPIRED` recorded as a divergence (#29).
- ✅ Both acceptance workflows green with zero manual steps.
- ✅ Results recorded (this report) and every divergence filed as a follow-up issue.
- ⛔ **#6 not yet unblocked.** Three of the divergences are correctness defects: #28 (the flagship duplicate-collapse guarantee), #37 (garbled append output), and the verification false-negatives #36/#38 that make the markdown-write evidence untrustworthy. Re-run `pytest --run-live` after these land; the gate is met when the seven `xfail`s flip to passing and the nested-tab skip is resolved (#31).
