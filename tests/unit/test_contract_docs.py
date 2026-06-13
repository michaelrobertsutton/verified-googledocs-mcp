"""Contract guards that keep the README in sync with the code.

Documentation rots silently in two ways this project has to defend against:

  - a tool is renamed in code but the README's tool table still lists the old
    name (the ``get_comment_thread`` rename is the cautionary tale);
  - an error code is added to or removed from the kernel but the README's
    error-code table is not updated.

These tests parse the README tables and compare them to the live tool registry
and the ``ErrorCode`` enum. A third test guards against a dead error code —
one that is defined (and perhaps documented) but never referenced anywhere in
the package. All offline; no network, no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastmcp import Client

from verified_googledocs_mcp.server import mcp
from verified_googledocs_mcp.verify import ErrorCode

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"
_SRC = _REPO_ROOT / "src" / "verified_googledocs_mcp"


def _section(markdown: str, heading: str) -> str:
    """Return the body of a ``## heading`` section, up to the next ``## ``.

    ``### `` subsections are kept as part of the body (``"### x".startswith("## ")``
    is False), so multi-table sections like *Tools* are captured whole.
    """
    out: list[str] = []
    capturing = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            if capturing:
                break
            capturing = line.strip() == f"## {heading}"
            continue
        if capturing:
            out.append(line)
    if not out:
        raise AssertionError(f"README section not found: ## {heading}")
    return "\n".join(out)


def _first_backtick_per_table_row(section: str) -> set[str]:
    """The first backticked token of every Markdown table row in a section.

    For the tool and error-code tables the first column holds the identifier
    in backticks; header and separator rows carry none and are skipped.
    """
    tokens: set[str] = set()
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        match = re.search(r"`([^`]+)`", line)
        if match:
            tokens.add(match.group(1))
    return tokens


async def _registered_tool_names() -> set[str]:
    async with Client(mcp) as client:
        return {tool.name for tool in await client.list_tools()}


class TestReadmeToolTable:
    @pytest.mark.asyncio
    async def test_readme_tools_match_the_registry(self) -> None:
        readme = _README.read_text(encoding="utf-8")
        documented = _first_backtick_per_table_row(_section(readme, "Tools"))
        registered = await _registered_tool_names()
        assert documented == registered


class TestReadmeErrorCodeTable:
    def test_readme_error_codes_match_the_enum(self) -> None:
        readme = _README.read_text(encoding="utf-8")
        documented = _first_backtick_per_table_row(_section(readme, "Error codes"))
        enum_codes = {code.value for code in ErrorCode}
        assert documented == enum_codes


class TestNoDeadErrorCodes:
    def test_every_error_code_is_referenced_in_src(self) -> None:
        sources = "\n".join(p.read_text(encoding="utf-8") for p in _SRC.rglob("*.py"))
        for code in ErrorCode:
            assert f"ErrorCode.{code.name}" in sources, (
                f"{code.name} is defined but never referenced in src — dead code?"
            )
