# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **Breaking:** `list_open_items` now requires either `tab_id` or
  `include_all_tabs=true`; omitting both returns `INVALID_INPUT`. This contract
  change makes the next release `0.2.0` rather than a patch release.
- Tool errors are now surfaced as JSON error envelopes instead of Python
  dictionary representations, so clients can parse `error_code`,
  `diagnostics`, and `retryable` reliably.

### Added
- Added `VERIFICATION_FAILED` for post-write verification failures.

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
