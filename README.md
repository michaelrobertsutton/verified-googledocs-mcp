# verified-googledocs-mcp

<!-- mcp-name: io.github.michaelrobertsutton/verified-googledocs-mcp -->

[![CI](https://github.com/michaelrobertsutton/verified-googledocs-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/michaelrobertsutton/verified-googledocs-mcp/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-91%25-brightgreen.svg)](pyproject.toml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An MCP server for Google Docs whose writes carry proof. Every mutating tool re-reads the affected content from the document after it writes and returns evidence of what actually changed: before/after excerpts, the match count, and the document revision before and after. A tool never reports success for an edit that did not land.

> **Status:** all 14 tools are implemented, covered by an offline unit suite, and exercised against the real Google Docs and Drive APIs — every tool and every error code passes the [live acceptance gate](docs/acceptance-report.md). Install with `uvx verified-googledocs-mcp`. See [Status](#status).

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

### Evidence by family

The guarantee is not one universal payload — it is a per-family invariant. Each
family re-reads the document after the write and proves the property that family
is responsible for. Every mutating tool also carries `revision_before`,
`revision_after`, and `audit_logged`.

| Family | Tools | Proves |
|--------|-------|--------|
| **Text edit** | `replace_text` | `match_count` equals `expected_matches`; `rung` names the normalization pass that matched; `before`/`after` are ±200-char excerpts of the edited span, the `after` re-read post-write |
| **Markdown range** | `replace_range_markdown`, `replace_tab_markdown`, `append_markdown` | `structural_match` (the written markdown round-trips), `input_blocks` vs `post_blocks` counts, and a `structural_diff` list naming any mismatch |
| **Structural** | `insert_image` | `inline_object_confirmed` — a post-write scan found the inline object at the anchor paragraph |
| **Comment state** | `add_anchored_comment`, `reply_to_comment`, `resolve_comment` | the re-queried `resolved` flag, `reply_count`, `content`, `quoted_text`, and `author` — a resolve that didn't land returns `COMMENT_STILL_OPEN`, never success |

The read and sync tools (`read_document`, `list_tabs`, `find_sections`,
`list_open_items`, `get_comment_thread`, `diff_tab_vs_file`) make no changes and
carry no `applied`/evidence payload.

### Dry run

The five mutating tools (`replace_text`, `replace_range_markdown`,
`replace_tab_markdown`, `append_markdown`, `insert_image`) accept `dry_run=true`.
No API write is issued; the response carries `applied: false`, an empty
`revision_after` (no write, so no new revision), `audit_logged: false`, and —
for `replace_text` — a predicted `after` excerpt computed by splicing the
replacement into the pre-read. Use it to confirm a locate resolves to the right
span before committing the edit.

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
| `list_open_items` | Open comments **and** pending suggested edits; pass `tab_id` or `include_all_tabs=true` |
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
| Live acceptance gate | done for the initial release — [report](docs/acceptance-report.md); rerun before release |
| PyPI packaging + publish workflow | done; first release `v0.1.0` |
| MCP registry listing | published with `v0.1.0` |

## Install

The server talks to Google with **your own** OAuth credentials, so setup is a
one-time Google Cloud step, then registering the server with your MCP client.

### 1. Google Cloud project (OAuth credentials)

1. Create a Google Cloud project and enable the **Google Docs API** and **Google Drive API** (APIs & Services → Library).
2. Configure the **OAuth consent screen**: User type **External**, publishing status **Testing**, and add your own Google account under **Test users**. (Testing mode is the point — the app stays private to the test users you list; you never submit it for Google verification.)
3. Create an **OAuth client ID** of type **Desktop app** and download the client secret JSON to `~/.config/verified-googledocs-mcp/credentials.json`. (Override the location with `VERIFIED_GOOGLEDOCS_MCP_CREDENTIALS`.)

### 2. Authorize once, in a terminal

```bash
uvx verified-googledocs-mcp auth
```

This opens a browser and completes consent. Because the app is unverified and in
Testing, Google shows a **"Google hasn't verified this app"** screen — click
**Advanced → Go to verified-googledocs-mcp (unsafe)** and continue. This is
expected for a personal Desktop client; you are granting access to your own app,
running locally as you. It then caches a refreshable token at
`~/.config/verified-googledocs-mcp/token.json`. Auth runs only here, never inside
the server, because MCP clients start the server headless.

### 3. Run it

```bash
uvx verified-googledocs-mcp           # downloads + runs in one step
# or: pip install verified-googledocs-mcp
```

Then register the server with your MCP client.

**From source.** To run from a local clone instead:

```bash
git clone https://github.com/michaelrobertsutton/verified-googledocs-mcp
cd verified-googledocs-mcp
uv run verified-googledocs-mcp
```

### Claude Code

A project-local `.mcp.json` is included in the repo. Clone and open the project and Claude Code picks it up automatically — no manual config required:

```bash
git clone https://github.com/michaelrobertsutton/verified-googledocs-mcp
cd verified-googledocs-mcp
claude  # .mcp.json is loaded automatically
```

**Use it across all your projects (user scope).** Register it once at user scope:

```bash
claude mcp add verified-googledocs-mcp --scope user -- uvx verified-googledocs-mcp
```

This writes to `~/.claude.json` and makes the server available in every Claude Code session on this machine. If `uvx` is not on Claude Code's PATH, use the full path (find it with `which uvx`).

### Claude Desktop and other clients

Most clients use the standard `mcpServers` config block. Add the following to your client's config file:

```jsonc
{
  "mcpServers": {
    "verified-googledocs-mcp": {
      "command": "uvx",
      "args": ["verified-googledocs-mcp"]
    }
  }
}
```

**PATH note for headless clients:** Claude Desktop and similar clients launch the server as a subprocess with a minimal `PATH` that may not include Homebrew or user-local bins. If `uvx` is not found, use its full path (`"command": "/opt/homebrew/bin/uvx"`). Find it with `which uvx`. On Apple Silicon the Homebrew prefix is `/opt/homebrew`; on Intel Mac it is `/usr/local`.

**Startup-timeout note.** The first `uvx` launch downloads the package and its dependencies, which can exceed a client's MCP startup timeout and surface as a failed connection. Pre-warm the cache once in a terminal by running the `auth` command (`uvx verified-googledocs-mcp auth`) — you do this anyway, and it installs the package into the `uvx` cache so the client's launch is fast.

**From source.** If you prefer to run from a local clone instead of PyPI:

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

**Logs / stderr.** The server logs to stderr, which MCP clients capture rather than show inline. If a connection or a tool call fails, check the client's MCP logs — for Claude Desktop on macOS, `~/Library/Logs/Claude/mcp*.log`. An `AUTH_EXPIRED` envelope there means the token is missing or expired; re-run the `auth` command.

The server uses the `documents` and `drive` scopes (comments require Drive). The credentials path is overridable with `VERIFIED_GOOGLEDOCS_MCP_CREDENTIALS`.

## Security and permissions

This is a single-user, local server. It runs as you, over stdio, launched by your MCP client; there is no network listener, no hosted service, and no shared credentials. It acts entirely with your own Google authority.

- **Scopes.** It requests `documents` and `drive`. The full `drive` scope is broader than editing alone needs, but the comment and suggestion tools (listing, replying to, and resolving comments on documents you already have) operate through the Drive API on arbitrary existing files, which the narrower `drive.file` scope cannot reach. `drive` is the minimum that covers the full tool set; if you don't need the comment tools, a fork could drop to a narrower scope.
- **Credentials at rest.** The OAuth client secret lives at `~/.config/verified-googledocs-mcp/credentials.json`; the cached token (including the refresh token) is written to `~/.config/verified-googledocs-mcp/token.json` with owner-only permissions (`0600`, under a `0700` directory). Treat both as secrets: a leaked refresh token grants your full `drive`+`documents` access until you revoke it in your Google Account's security settings. Neither file is ever committed (both are gitignored).
- **Audit log.** Every mutation appends to `~/.local/state/verified-googledocs-mcp/audit.jsonl` (also `0600`). Each line records the timestamp, document ID, tab ID, tool name, and the evidence payload — which includes before/after **content excerpts**. To log the metadata without the excerpts, set the environment variable `VERIFIED_GOOGLEDOCS_MCP_AUDIT_EXCERPTS` to a falsey value (`0`, `false`, `no`, or `off`); the `before`/`after` fields are then replaced with `"[redacted; N chars]"` and every other field is kept. Override the log location with `XDG_STATE_HOME`.
- **Local file diffs.** `diff_tab_vs_file` reads a local file so it can compare a Doc tab with markdown on disk. It resolves symlinks before reading and only allows paths under `VERIFIED_GOOGLEDOCS_MCP_ALLOWED_FILE_ROOTS` (a platform path-list; defaults to the server process working directory). It also refuses files larger than `VERIFIED_GOOGLEDOCS_MCP_MAX_DIFF_FILE_BYTES` (default `1000000`).

## Error codes

Failures return a typed envelope (`error_code`, `message`, `diagnostics`, `retryable`):

| Code | Meaning |
|------|---------|
| `ZERO_MATCH` | Target not found after the full normalization ladder; diagnostics include the nearest near-miss |
| `MATCH_COUNT_MISMATCH` | Found a different count than `expected_matches`; no edit made; all locations returned |
| `REVISION_CONFLICT` | Document changed between read and write; retry after re-reading |
| `VERIFICATION_FAILED` | A write was issued, but the post-write re-read did not verify the expected final state |
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

Unit tests run against synthetic Docs API fixtures and an in-memory MCP client, so the full suite is offline (it never runs the live tests). The live acceptance suite (under `tests/live/`) runs with `pytest --run-live` against a real scratch document and needs OAuth credentials; it is the pre-release gate and never runs in CI — see [`docs/acceptance-report.md`](docs/acceptance-report.md).

See [`docs/architecture.md`](docs/architecture.md) for the module map and the verification pipeline, [`PRD.md`](docs/PRD.md) for the full specification, [`docs/cutover.md`](docs/cutover.md) to migrate off a general Workspace MCP server, and [`CONTRIBUTING.md`](CONTRIBUTING.md) to build on it.

## Limitations

- **Accepting or rejecting suggested edits** is not possible through the Google Docs API. This server makes suggestions *visible* alongside comments; acting on them stays a manual step in the Docs UI.
- **Single user, local.** stdio transport, one cached token, no hosted or multi-user mode.
- **Docs only.** Gmail, Calendar, and Sheets are out of scope by design.
- **Markdown is a fixed subset** (headings, bold/italic, lists, tables, links). Anything outside it is rejected with a clear error rather than approximated.

## License

MIT, © 2026 Michael Sutton.
