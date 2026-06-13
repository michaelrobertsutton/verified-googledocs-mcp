# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/) once it reaches a release.

## [Unreleased]

### Added
- OAuth installed-app flow with a refreshable token cache, exposed as the
  `googledocs-mcp auth` terminal command.
- Read tools: `read_document`, `list_tabs`, `find_sections` (ranges stamped with
  the document revision).
- Docs JSON to markdown converter (read direction) with placeholder tokens for
  out-of-subset elements.
- Verification kernel: the text locator with its normalization ladder and
  UTF-16 span mapping, the typed error envelope, and the best-effort audit log.
- markdown to `batchUpdate` compiler (write direction) for the supported subset.
- Suggested-edit extraction from document JSON.
- CI: ruff and pytest on every push and pull request.

[Unreleased]: https://github.com/michaelrobertsutton/googledocs-mcp
