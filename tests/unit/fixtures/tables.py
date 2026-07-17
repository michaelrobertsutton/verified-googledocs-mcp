"""Index-accurate table fixtures for testing verified_googledocs_mcp.tables.

Layout convention: cells are laid out in row-major order with a fixed 2-unit
gap between consecutive cells (mirroring the style of the existing
``_table_elem`` in tests/unit/fixtures/markdown_tools/__init__.py). This is
NOT the literal Google Docs insertTable geometry (see
markdown_writer.py._table_cell_index for that formula) — read-only and
replace_table_row-style tools consume whatever indices a real document
happens to have, so internal self-consistency (each paragraph's own
startIndex/endIndex matches its content) is all that matters here.
"""

from __future__ import annotations

from typing import Any

# A cell is either a single-paragraph string, or a list of strings — one
# entry per paragraph — for a multi-paragraph cell.
CellSpec = str | list[str]

_CELL_GAP = 2  # fixed index gap between consecutive cells / rows


def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def _para_elem(text: str, start: int, style: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = text + "\n"
    end = start + _utf16_len(raw)
    text_run: dict[str, Any] = {"content": raw}
    if style:
        text_run["textStyle"] = style
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{"startIndex": start, "endIndex": end, "textRun": text_run}],
        },
    }


def heading(level: int, text: str, start: int) -> tuple[dict[str, Any], int]:
    """A HEADING_<level> paragraph. Returns (element, next_cursor)."""
    raw = text + "\n"
    end = start + _utf16_len(raw)
    elem = {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "paragraphStyle": {"namedStyleType": f"HEADING_{level}"},
            "elements": [{"startIndex": start, "endIndex": end, "textRun": {"content": raw}}],
        },
    }
    return elem, end


def plain_paragraph(text: str, start: int) -> tuple[dict[str, Any], int]:
    """A NORMAL_TEXT paragraph. Returns (element, next_cursor)."""
    elem = _para_elem(text, start)
    return elem, elem["endIndex"]


def build_table(
    start: int,
    rows: list[list[CellSpec]],
    *,
    styles: dict[tuple[int, int], dict[str, Any]] | None = None,
    merges: dict[tuple[int, int], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    """Build an index-accurate table structural element.

    rows[r][c] is a plain string (single-paragraph cell) or a list of
    strings (one entry per paragraph, for a multi-paragraph cell); shorter
    rows are padded with empty cells so every row has the same column count.
    styles maps (r, c) to a textStyle dict applied to the first paragraph's
    first textRun. merges maps (r, c) to a tableCellStyle dict, e.g.
    {"rowSpan": 2}.

    Returns (table_element, end_index) — end_index is where the next
    structural element in the same body should start.
    """
    styles = styles or {}
    merges = merges or {}
    n_cols = max((len(row) for row in rows), default=0)

    cursor = start + _CELL_GAP
    table_rows: list[dict[str, Any]] = []
    for r, row in enumerate(rows):
        table_cells: list[dict[str, Any]] = []
        for c in range(n_cols):
            cell_spec: CellSpec = row[c] if c < len(row) else ""
            paragraphs = [cell_spec] if isinstance(cell_spec, str) else list(cell_spec)
            if not paragraphs:
                paragraphs = [""]
            para_elems: list[dict[str, Any]] = []
            for p_idx, text in enumerate(paragraphs):
                style = styles.get((r, c)) if p_idx == 0 else None
                para_elem = _para_elem(text, cursor, style=style)
                para_elems.append(para_elem)
                cursor = para_elem["endIndex"]
            cursor += _CELL_GAP
            cell: dict[str, Any] = {"content": para_elems}
            if (r, c) in merges:
                cell["tableCellStyle"] = merges[(r, c)]
            table_cells.append(cell)
        table_rows.append({"tableCells": table_cells})

    table_elem = {
        "startIndex": start,
        "endIndex": cursor,
        "table": {
            "rows": len(rows),
            "columns": n_cols,
            "tableRows": table_rows,
        },
    }
    return table_elem, cursor


def embed_nested_table(
    table_elem: dict[str, Any], r: int, c: int, inner_table_elem: dict[str, Any]
) -> None:
    """Mutate table_elem in place: cell (r, c)'s content becomes a nested table.

    Docs allows a table structural element to appear inside a cell's content
    list, exactly like a top-level content item — used to test that
    _top_level_tables does not recurse into cells.
    """
    table_elem["table"]["tableRows"][r]["tableCells"][c]["content"] = [inner_table_elem]


def doc_with_content(
    content: list[dict[str, Any]],
    *,
    doc_id: str = "doc-table-test",
    tab_id: str = "tab-1",
    revision: str = "rev-1",
) -> dict[str, Any]:
    """Wrap a list of structural elements into a minimal single-tab document."""
    return {
        "documentId": doc_id,
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": "Tab", "index": 0},
                "documentTab": {"body": {"content": content}},
                "childTabs": [],
            }
        ],
    }
