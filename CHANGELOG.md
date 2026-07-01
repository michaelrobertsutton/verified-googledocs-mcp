# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Markdown tables no longer 400 on write via `replace_tab_markdown` or
  `replace_range_markdown`. The compiler's cell-index formula was off by two
  (pinned against the live API by a new contract test), and content
  following a table now lands at the correct index instead of a stale one.
- `dry_run` is now authoritative for markdown writes: a new offline index
  simulator replays the exact assembled request list and is shared by both
  `dry_run` and the real write, so the two can no longer disagree. Failures
  raise the new `INDEX_SIMULATION_FAILED` error instead of a raw API 400.
- Table structural verification now compares cell contents, not just
  row/column counts, so a write that lands the right shape with wrong or
  missing cell text is correctly reported as a mismatch.
- `diff_tab_vs_file`'s allowed-file-root default changed from the server
  process's working directory to the user's home directory, so cross-repo
  diffs work without per-machine configuration — the common case is a
  server registered with `--directory` pinned to one repo while the diff
  target lives in another. A new unconditional denylist (`.ssh`, `.aws`,
  `.gnupg`, `.netrc`, `.git-credentials`, `.config/gh`,
  `.docker/config.json`, `.npmrc`) closes the resulting exposure to a
  document's own content tricking an agent into reading credentials.
  Rejected paths now name the exact environment variable to set.

### Changed
- **Breaking:** `list_open_items` now requires either `tab_id` or
  `include_all_tabs=true`; omitting both returns `INVALID_INPUT`. This contract
  change makes the next release `0.2.0` rather than a patch release.
- Tool errors are now surfaced as JSON error envelopes instead of Python
  dictionary representations, so clients can parse `error_code`,
  `diagnostics`, and `retryable` reliably.
- `read_document` gains `format="outline"`: headings only (level, text,
  position), for callers that only need geometry and don't want to pull a
  whole tab as markdown.

### Added
- Added `VERIFICATION_FAILED` for post-write verification failures.
- Added `INDEX_SIMULATION_FAILED` for markdown writes whose compiled
  requests would land at an invalid index.

## [0.1.0] - 2026-06-14

First public release.

### Added
- OAuth installed-app flow with a refreshable token cache, exposed as the
  `verified-googledocs-mcp auth` terminal command.
- Read and structure tools: `read_document`, `list_tabs`, `find_sections`
  (ranges stamped with the document revision).
- Verified text editing: `replace_text` with the normalization ladder, the
  `expected_matches` guard, UTF-16 span mapping, and before/after evidence,
  enforced by the evidence middleware.
- Verified markdown writes: `replace_range_markdown`, `replace_tab_markdown`,
  `append_markdown`, and `insert_image`, each with structural guardrails and
  post-write re-read evidence; all support `dry_run`.
- Comment and suggestion tools: `list_open_items` (open comments **and** pending
  suggestions), `get_comment_thread`, `add_anchored_comment`, `reply_to_comment`,
  and `resolve_comment` (verifies the comment actually closed).
- Sync: `diff_tab_vs_file`.
- Verification kernel: text locator, typed error envelope (12 error codes), and
  the best-effort local audit log with the
  `VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS` redaction toggle.
- Docs JSON ↔ markdown conversion for the supported subset.
- Live acceptance gate: all 14 tools and all 12 error codes exercised against the
  real Google Docs and Drive APIs (see `docs/acceptance-report.md`).
- Packaging and release: `uvx`/`pip` install, `python -m verified_googledocs_mcp`
  entry point, a PyPI publish workflow using trusted publishing (OIDC), a
  `server.json` for the MCP registry, and a migration guide (`docs/cutover.md`).
- CI: ruff, mypy, and pytest on every push and pull request.

[Unreleased]: https://github.com/michaelrobertsutton/verified-googledocs-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/michaelrobertsutton/verified-googledocs-mcp/releases/tag/v0.1.0
