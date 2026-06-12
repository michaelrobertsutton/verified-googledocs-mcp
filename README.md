# googledocs-mcp

An MCP server for Google Docs that provides verified write operations. Every mutating tool re-reads its result from the server and returns evidence — before/after excerpts, match counts, revision IDs — so you always know exactly what changed.

> **Status:** Under active development. Not yet ready for general use.

## Why

Existing Google Docs MCP servers have critical failure modes: silent cross-tab edits, zero-match searches with no diagnostics, duplicate-sentence collapse, and garbled markdown conversion. This server addresses those issues at the protocol level with built-in guards rather than fragile prompt instructions.

## Design Principles

- **Tab-scoped by default** — all document operations require an explicit `tab_id`
- **Verified writes** — every mutation returns server-confirmed before/after state
- **Normalization ladder** — search handles curly quotes, non-breaking spaces, and Unicode variants automatically
- **Match-count guards** — prevents replacing more (or fewer) matches than intended
- **Small surface area** — 14 focused tools instead of 150

## Installation

_Coming soon._

## License

MIT
