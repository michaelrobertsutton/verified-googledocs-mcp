# Live acceptance report (issue #23)

The pre-release gate. Every one of the 14 tools, every one of the 12 error
codes, and both PRD acceptance workflows, exercised against the real Google
Docs and Drive APIs on a seeded multi-tab document — not against recorded
fixtures.

| | |
|---|---|
| **Fixture document** | `1Zm_6bAwA7UH1DKkGVL3kg9XcQ6rIZHmUFcQPTcoJb6Y` (seeded in #1, extended in #31 with a HEADING_1 and a nested tab) |
| **Suite** | `tests/live/` — driven by the in-memory FastMCP `Client(mcp)`, so tool registration and the evidence-enforcement middleware run on the path out to the live API |
| **How to run** | `pytest --run-live` (requires OAuth credentials; never runs in CI) |
| **Result** | **53 passed, 4 xfailed** |

Status legend: **live** (proven directly against the API) · **sim** (proven via
a controlled simulation in which the real API still produces the
rejection/re-query) · **diverged** (a filed defect; the assertion is quarantined
`xfail` and flips to passing once fixed).

## All 14 tools exercised live

| Tool | Status | Test(s) |
|---|---|---|
| `read_document` (markdown + structured) | live | `test_reads.py::TestReadDocument` |
| `list_tabs` (incl. the nested tab) | live | `test_reads.py::TestListTabs` |
| `find_sections` | live | `test_reads.py::TestFindSections` |
| `replace_text` | live | `test_replace_text.py` (whole module) |
| `replace_range_markdown` | live¹ | `test_markdown_writes.py::TestReplaceRangeMarkdown` |
| `replace_tab_markdown` | live¹ | `test_markdown_writes.py::TestReplaceTabMarkdown` |
| `append_markdown` | diverged (#37) | `test_markdown_writes.py::TestAppendMarkdown` |
| `insert_image` | live² | `test_markdown_writes.py::TestInsertImage` |
| `list_open_items` | live | `test_comments.py::TestListOpenItems` |
| `get_comment_thread` | live | `test_comments.py::TestGetCommentThread` |
| `add_anchored_comment` | live | `test_comments.py::TestAddAnchoredComment` |
| `reply_to_comment` | live | `test_comments.py::TestReplyToComment` |
| `resolve_comment` | live | `test_comments.py::TestResolveComment` |
| `diff_tab_vs_file` | live | `test_sync.py::TestDiffTabVsFile` |

¹ The *write* is correct and confirmed by re-read; the `structural_match` *evidence* false-negatives (#36).
² The image *inserts* correctly; the `inline_object_confirmed` *evidence* false-negatives (#38).

## §7 error-code matrix — all 12 codes triggered live

| Code | Status | How triggered | Test |
|---|---|---|---|
| `ZERO_MATCH` | live | find string absent; near-miss span returned | `test_replace_text.py::TestZeroMatch` |
| `MATCH_COUNT_MISMATCH` | live | duplicate sentence (2 matches), `expected_matches=1`, no edit, all spans returned | `test_replace_text.py::TestMatchCountGuard` |
| `REVISION_CONFLICT` | sim | stale `requiredRevisionId` after an out-of-band edit → real Docs 400 | `test_replace_text.py::TestRevisionConflict` |
| `STALE_RANGE` | live | `find_sections` range reused after the doc moved on | `test_markdown_writes.py::...test_stale_range_after_doc_moves_on` |
| `TAB_NOT_FOUND` | live | bad `tab_id`; available tabs listed | `test_replace_text.py::...test_unknown_tab_is_tab_not_found` |
| `STRUCTURAL_BOUNDARY` | live | find string crossing a paragraph boundary | `test_replace_text.py::...test_match_crossing_paragraph_boundary...` |
| `UNSUPPORTED_MARKDOWN` | live | blockquote (out of subset); construct named | `test_markdown_writes.py::TestUnsupportedMarkdown` |
| `QUOTE_NOT_FOUND` | live | absent anchor; nearest candidates returned | `test_comments.py::...test_missing_quote_is_quote_not_found` |
| `COMMENT_STILL_OPEN` | sim | resolve action stubbed to a no-op → real re-query sees it still open | `test_comments.py::...test_comment_still_open_failure_path` |
| `INVALID_INPUT` | live | empty find / find==replace / missing diff file | `test_replace_text.py`, `test_sync.py`, `test_cross_cutting.py` |
| `IMAGE_SOURCE_UNSUPPORTED` | live | local file path as image source | `test_markdown_writes.py::...test_local_path_is_image_source_unsupported` |
| `AUTH_EXPIRED` | live | missing token → typed envelope (fixed in #29) | `test_cross_cutting.py::TestAuth` |

**All 12 codes triggered live** (10 directly, 2 via controlled simulation).

## §1–§8 checklist

**§1 Reads & structure** — `read_document` markdown + structured spans line up with visible text; `list_tabs` ids/titles/index match **and report the nested tab** `t.22v4eg81pdjk`; `find_sections` resolves the `Text Hazards` HEADING_1 in `t.0` to range `[1, 14)`, stamped with a live `revisionId`.

**§2 Verified text edits** — all four normalization rungs reported correctly (`exact`, `curly_straight_quotes`, `nbsp_whitespace_runs`, `soft_hyphen_strip`); `ZERO_MATCH` + near-miss; the match-count guard refuses the duplicate and makes no edit (proven on both a copy and — since #28 — the canonical doc); tab scoping leaves the other tab byte-for-byte identical; an edit positioned after emoji / ZWJ / combining-mark / RTL text lands on the intended span with the hazard characters intact; dry-run predicts without writing (revision unchanged); `REVISION_CONFLICT` (sim); evidence shape verified (before/after are server re-reads, `revision_before` ≠ `revision_after`, `match_count` + `rung` present, `audit_logged: true`).

**§3 Markdown writes** — `replace_range_markdown` replaces the `Text Hazards` range, then `STALE_RANGE` on reuse; `replace_tab_markdown` whole-tab replace lands the new structure; `append_markdown` runs; `insert_image` succeeds at a quoted anchor and at the heading, local path → `IMAGE_SOURCE_UNSUPPORTED`; out-of-subset markdown → `UNSUPPORTED_MARKDOWN` naming the construct; `STRUCTURAL_BOUNDARY` covered in §2. *Verification/output divergences remain: #36 (structural_match), #37 (append fusion), #38 (image confirm).*

**§4 Comments & suggestions** — `list_open_items` returns open comments **and** pending suggestions in one call; `get_comment_thread` returns the full reply chain; `add_anchored_comment` is quote-validated (doc-level rendering per the #1 spike), absent quote → `QUOTE_NOT_FOUND` with candidates; `reply_to_comment` appears on re-query; **`resolve_comment` actually closes the comment** (re-query confirms `resolved: true`, closed via the `action: resolve` reply — the incumbent-bug regression), and the failure path → `COMMENT_STILL_OPEN` (sim).

**§5 Sync** — `diff_tab_vs_file` correct in both directions (file ahead → insert; doc ahead → delete), identical when matched, `INVALID_INPUT` on missing file, `TAB_NOT_FOUND` on bad tab.

**§6 Cross-cutting** — enforcement middleware accepts real evidence-bearing mutations and passes typed errors through (no misfire); audit log writes exactly one JSONL line per mutation, the file is `0600` under a `0700` dir; `audit_excerpts=false` redaction proven both directly and via the `VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS` env toggle (#30); a missing token fails fast as `AUTH_EXPIRED` (#29); `INVALID_INPUT` on contradictory args; `TAB_NOT_FOUND` lists available tabs.

**§8 Acceptance workflows (zero manual steps)** — full comment-resolution cycle (locate → comment → reply → resolve → confirm closed) and markdown sync round trip (read → diff → apply → re-read → confirm convergence), both green with no human verification step.

## Divergences

| # | Status | Summary |
|---|---|---|
| [#28](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/28) | ✅ fixed (on main) | `fetch_document` now pins `PREVIEW_WITHOUT_SUGGESTIONS`; the duplicate-collapse guard fires on docs with suggestions |
| [#29](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/29) | ✅ fixed (on main) | auth failures now surface as the typed `AUTH_EXPIRED` envelope |
| [#30](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/30) | ✅ fixed (on main) | `audit_excerpts` redaction reachable via `VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS` |
| [#31](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/31) | ✅ seeded | fixture now has a `Text Hazards` HEADING_1 and a nested tab `t.22v4eg81pdjk` (the Docs API *can* create tabs via `addTab`) |
| [#36](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/36) | open | `to_markdown` omits blank lines between blocks → `structural_match` false negatives (write is correct) |
| [#37](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/37) | open | `append_markdown` fuses the appended block into the trailing paragraph (garbled output) |
| [#38](https://github.com/michaelrobertsutton/verified-googledocs-mcp/issues/38) | open | `insert_image` `inline_object_confirmed` false negative (verifier checks the wrong paragraph) |

## Definition of done

- ✅ Every tool exercised live.
- ✅ All 12 error codes triggered live with their shape recorded.
- ✅ Both acceptance workflows green with zero manual steps.
- ✅ Results recorded (this report); every divergence filed; #28/#29/#30/#31 resolved.
- ⛔ **#6 not yet unblocked.** The three remaining divergences are markdown-write correctness/verification defects: #37 (garbled append output) and the false-negative evidence in #36/#38. Re-run `pytest --run-live` after they land; the gate is met when the four `xfail`s flip to passing.
