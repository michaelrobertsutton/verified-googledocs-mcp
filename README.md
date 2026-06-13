# verified-googledocs-mcp

[![CI](https://github.com/michaelrobertsutton/verified-googledocs-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/michaelrobertsutton/verified-googledocs-mcp/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-91%25-brightgreen.svg)](pyproject.toml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP server for Google Docs whose writes carry proof. Every mutating tool re-reads the affected content from the document after it writes and returns evidence of what actually changed: before/after excerpts, the match count, and the document revision before and after. A tool never reports success for an edit that did not land.

> **Status:** early, built in the open. All 14 tools are implemented and covered by an offline unit suite. What's left before a release: validating a handful of Google Docs API behaviors against a live document, a live smoke suite, and PyPI packaging. See [Status](#status). Not yet on PyPI.

## The problem

Driving Google Docs from an agent through a general Workspace MCP server tends to fail in quiet, expensive ways:

- A `findAndReplace` meant for one tab silently edits every tab in the document.
- A search returns "0 matches" because the document has curly quotes or a non-breaking space the query doesn't, with no hint why.
- A replace meant for one occurrence hits a repeated sentence and collapses both.
- A "resolve comment" call returns success while the comment stays open.
- Listing comments misses suggested edits entirely.
- A markdown merge injects garbled text that a human only catches days later.

Each of these has a procedural workaround: tell the agent to scope to a tab, retry with normalized quotes, re-read after every write, never trust a resolve result. Those instructions work until someone forgets one. This server moves the discipline into the protocol, where it is deterministic.

## The verified-write contract

Every mutating tool runs the same pipeline: read the tab, locate the target, apply the edit under a revision precondition, read the tab again, and return evidence built from the second read. The return value is a claim about the document's state *after* the call, backed by a server-side re-read, not an echo of the API response.

```jsonc
// replace_text(doc_id, tab_id, find="teh", replace="the", expected_matches=1)
{
  "applied": true,
  "match_count": 1,
  "rung": "exact",                 // which normalization rung matched
  "before": "...±200 chars around the edit, pre-write...",
  "after":  "...the same span, re-read after the write...",
  "revision_before": "ALm37BX...",
  "revision_after":  "ALm37Cy...",
  "audit_logged": true
}
```

When something is wrong, the tool fails loud and *diagnosed*, with a typed error the agent can act on in one round trip rather than guessing:

```jsonc
{
  "error_code": "MATCH_COUNT_MISMATCH",
  "message": "expected 1 match(es) but found 3 at rung 'exact'",
  "diagnostics": { "expected": 1, "actual": 3, "spans": [ /* every location */ ] },
  "retryable": false
}
```

### What backs the guarantee

- **Tab-scoped by default.** Editing tools require an explicit `tab_id`. There is no whole-document replace, so a one-tab edit can never leak into a cover letter or an appendix tab.
- **Normalization ladder.** A search tries exact match, then curly/straight quote equivalence, then non-breaking-space and whitespace-run equivalence, then soft-hyphen stripping, and reports which rung matched. A zero-match result includes the nearest near-miss span it found.
- **Match-count guard.** `expected_matches` defaults to 1. If the real count differs, the tool makes no edit and returns every match location.
- **Revision preconditions.** Writes carry `writeControl.requiredRevisionId` from the pre-read, so a document that changed underneath the operation is rejected by the API rather than edited blind. Section ranges from `find_sections` are stamped with the revision they were computed at and refuse to apply once stale.
- **UTF-16 correct.** Match spans are mapped to the UTF-16 code units the Docs API indexes by, so emoji, combining marks, and other astral characters don't shift an edit onto the wrong text.
- **Audit trail.** Every mutation appends a line to a local JSONL log. The append is best-effort: it never fails a write, and if it can't be written the evidence says so (`audit_logged: false`).

## Tools

Fourteen focused tools, each described by *when* to reach for it, replace the slice of a 150-tool Workspace server that document workflows actually use.

### Reading and structure
| Tool | What it does |
|------|--------------|
| `read_document` | Read a tab as markdown or as structured positions and style runs |
| `list_tabs` | List tab IDs, titles, and nesting |
| `find_sections` | Find headings and return their ranges, stamped with the document revision |

### Editing (verified, tab-scoped)
| Tool | What it does |
|------|--------------|
| `replace_text` | Find/replace within a tab, with the normalization ladder and match guard |
| `replace_range_markdown` | Replace a section range with markdown |
| `replace_tab_markdown` | Replace a whole tab's content with markdown |
| `append_markdown` | Append markdown to a tab |
| `insert_image` | Insert an image at a quoted anchor or heading |

### Comments and suggestions
| Tool | What it does |
|------|--------------|
| `list_open_items` | Open comments **and** pending suggested edits in one call |
| `get_comment_thread` | Read a comment's full reply chain |
| `add_anchored_comment` | Add a comment anchored to quoted text |
| `reply_to_comment` | Reply to a comment |
| `resolve_comment` | Resolve a comment, re-query it, and confirm it actually closed |

### Sync
| Tool | What it does |
|------|--------------|
| `diff_tab_vs_file` | Diff a tab's markdown against a local file |

## Status

Built incrementally; each tool ships with its verification and tests rather than as a stub.

| Area | State |
|------|-------|
| OAuth (`verified-googledocs-mcp auth`), token cache | done |
| `read_document`, `list_tabs`, `find_sections` | done |
| Verification kernel (locator, error envelope, audit) | done |
| `replace_text` (verified) + enforcement middleware | done |
| Comment tools + `list_open_items` | done |
| Markdown write tools + `diff_tab_vs_file` | done |
| Live API validation + smoke suite | needs credentials |
| PyPI packaging, MCP registry listing | planned |

## Setup

> Packaging is in progress; until it lands, run from a clone.

The server talks to Google with your own OAuth credentials. One-time setup:

1. Create a Google Cloud project and enable the **Google Docs API** and **Google Drive API**.
2. Configure the OAuth consent screen (External, Testing) and add your Google account as a test user.
3. Create an OAuth client ID of type **Desktop app** and download the client secret to `~/.config/verified-googledocs-mcp/credentials.json`.
4. Authorize once, in a terminal:

   ```bash
   uv run verified-googledocs-mcp auth
   ```

   This opens a browser, completes consent, and caches a refreshable token at `~/.config/verified-googledocs-mcp/token.json`. Auth runs only here, never inside the server, because MCP clients start the server headless.

Then register the server with your MCP client.

### Claude Code

A project-local `.mcp.json` is included in the repo. Clone and open the project and Claude Code picks it up automatically — no manual config required:

```bash
git clone https://github.com/michaelrobertsutton/verified-googledocs-mcp
cd verified-googledocs-mcp
claude  # .mcp.json is loaded automatically
```

**Use it across all your projects (user scope).** To make the server available in every Claude Code session on this machine — not just inside the cloned repo — register it at user scope with a pinned directory:

```bash
claude mcp add verified-googledocs-mcp --scope user -- \
  /opt/homebrew/bin/uv run --directory /path/to/verified-googledocs-mcp verified-googledocs-mcp
```

This writes to `~/.claude.json` (the user-scope store). The `--directory` flag pins `uv` to resolve the project from the repo path, so the command works regardless of which directory your Claude Code session starts from. Replace `/path/to/verified-googledocs-mcp` with your actual clone path.

The full `uv` path (`/opt/homebrew/bin/uv`) avoids PATH gaps in headless launches — see the PATH note below for details.

### Claude Desktop and other clients

Most clients use the standard `mcpServers` config block. Add the following to your client's config file:

```jsonc
{
  "mcpServers": {
    "verified-googledocs-mcp": {
      "command": "uv",
      "args": ["run", "verified-googledocs-mcp"],
      "cwd": "/path/to/verified-googledocs-mcp"
    }
  }
}
```

**PATH note for headless clients:** Claude Desktop and similar clients launch the server as a subprocess with a minimal `PATH` that may not include Homebrew or user-local bins. If `uv` is not found, use its full path:

```jsonc
{
  "mcpServers": {
    "verified-googledocs-mcp": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "verified-googledocs-mcp"],
      "cwd": "/path/to/verified-googledocs-mcp"
    }
  }
}
```

Find the full path with `which uv`. On Apple Silicon the Homebrew prefix is `/opt/homebrew`; on Intel Mac it is `/usr/local`.

The server uses the `documents` and `drive` scopes (comments require Drive). The credentials path is overridable with `VERIFIED_GOOGLEDOCS_MCP_CREDENTIALS`.

## Security and permissions

This is a single-user, local server. It runs as you, over stdio, launched by your MCP client; there is no network listener, no hosted service, and no shared credentials. It acts entirely with your own Google authority.

- **Scopes.** It requests `documents` and `drive`. The full `drive` scope is broader than editing alone needs, but the comment and suggestion tools (listing, replying to, and resolving comments on documents you already have) operate through the Drive API on arbitrary existing files, which the narrower `drive.file` scope cannot reach. `drive` is the minimum that covers the full tool set; if you don't need the comment tools, a fork could drop to a narrower scope.
- **Credentials at rest.** The OAuth client secret lives at `~/.config/verified-googledocs-mcp/credentials.json`; the cached token (including the refresh token) is written to `~/.config/verified-googledocs-mcp/token.json` with owner-only permissions (`0600`, under a `0700` directory). Treat both as secrets: a leaked refresh token grants your full `drive`+`documents` access until you revoke it in your Google Account's security settings. Neither file is ever committed (both are gitignored).
- **Audit log.** Every mutation appends to `~/.local/state/verified-googledocs-mcp/audit.jsonl`, also `0600`. It records document content excerpts; pass `audit_excerpts=false` to log evidence metadata without the content.

## Error codes

Failures return a typed envelope (`error_code`, `message`, `diagnostics`, `retryable`):

| Code | Meaning |
|------|---------|
| `ZERO_MATCH` | Target not found after the full normalization ladder; diagnostics include the nearest near-miss |
| `MATCH_COUNT_MISMATCH` | Found a different count than `expected_matches`; no edit made; all locations returned |
| `REVISION_CONFLICT` | Document changed between read and write; retry after re-reading |
| `STALE_RANGE` | A `find_sections` range was used after the document moved on; re-run `find_sections` |
| `TAB_NOT_FOUND` | Unknown `tab_id`; available tabs listed |
| `STRUCTURAL_BOUNDARY` | Match crosses a paragraph or table-cell boundary |
| `UNSUPPORTED_MARKDOWN` | Markdown outside the supported subset; the offending construct is named |
| `QUOTE_NOT_FOUND` | Comment anchor text not found; nearest candidates returned |
| `COMMENT_STILL_OPEN` | A resolve was requested but re-query shows the comment open |
| `INVALID_INPUT` | Empty or contradictory arguments |
| `IMAGE_SOURCE_UNSUPPORTED` | Image source is a local path; a fetchable URL is required |
| `AUTH_EXPIRED` | No valid token; run `verified-googledocs-mcp auth` |

## Development

```bash
uv run --extra dev pytest                 # unit tests (offline) + coverage
uv run --extra dev ruff check src tests   # lint
uv run --extra dev ruff format src tests  # format
uv run --extra dev mypy src               # type check
```

Unit tests run against synthetic Docs API fixtures and an in-memory MCP client, so the full suite is offline. A small live smoke suite (under `tests/live/`) exercises a real scratch document and needs credentials.

See [`docs/architecture.md`](docs/architecture.md) for the module map and the verification pipeline, [`PRD.md`](docs/PRD.md) for the full specification, and [`CONTRIBUTING.md`](CONTRIBUTING.md) to build on it.

## Limitations

- **Accepting or rejecting suggested edits** is not possible through the Google Docs API. This server makes suggestions *visible* alongside comments; acting on them stays a manual step in the Docs UI.
- **Single user, local.** stdio transport, one cached token, no hosted or multi-user mode.
- **Docs only.** Gmail, Calendar, and Sheets are out of scope by design.
- **Markdown is a fixed subset** (headings, bold/italic, lists, tables, links). Anything outside it is rejected with a clear error rather than approximated.

## License

MIT, © 2026 Michael Sutton.
