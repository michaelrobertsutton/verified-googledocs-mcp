# Contributing

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run --extra dev pytest        # full unit suite, offline
uv run --extra dev ruff check .  # lint
```

Unit tests use synthetic Docs API fixtures and the FastMCP in-memory client, so they need no network and no Google credentials. The live smoke suite under `tests/live/` exercises a real scratch document and is the only part that needs auth; it is not run in CI.

## The one rule for new tools

Every mutating tool goes through the verification kernel in `verify.py` and returns an evidence payload. No tool reports success from the API response alone. Concretely:

1. Read the tab; capture the revision.
2. Use `locate()` (or the appropriate evidence family) to find the target and run the guards.
3. Apply the write under `writeControl.requiredRevisionId`.
4. Re-read and build evidence from the second read.
5. Append to the audit log (best-effort).
6. Raise a typed `VerifyError` for any failure so the client gets `error_code`, `message`, `diagnostics`, `retryable`.

The enforcement middleware rejects any mutating tool that returns without evidence, so this is checked, not just convention. See [`docs/architecture.md`](docs/architecture.md).

## Conventions

- Tool descriptions state *when* to use the tool, not only what it does — MCP clients pick tools from descriptions.
- Tab-scoped by default: editing tools require an explicit `tab_id`.
- Markdown support is a fixed subset. Add to the subset deliberately; reject the rest with `UNSUPPORTED_MARKDOWN` rather than approximating.
- Full type hints; keep `verify.py` free of network calls so it stays unit-testable.
- Match the existing module layout (see the map in the architecture doc).

## Commits and PRs

- Plain, imperative commit messages ("Add verified resolve_comment").
- One PR per logical unit; keep the suite green and ruff clean.
- Reference the issue the work belongs to (`Closes #N` / `Part of #N`).
