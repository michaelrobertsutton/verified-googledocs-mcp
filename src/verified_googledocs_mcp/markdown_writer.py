"""Markdown -> Google Docs batchUpdate request compiler (write direction).

Compiles a markdown string into an ordered list of Google Docs API
``batchUpdate`` request dicts (e.g. ``insertText``, ``updateParagraphStyle``,
``updateTextStyle``, ``createParagraphBullets``, ``insertTable``).  The caller
is responsible for submitting those requests to the API; this module is pure
(no network, no Google credentials).

UTF-16 indices
--------------
The Docs API measures all positions in UTF-16 code units, NOT Python
``len()`` (which counts Unicode code points).  Astral-plane characters such as
emoji consume *two* UTF-16 code units.  Every cursor advance and range length
in this module is computed via :func:`_utf16_len`, never via ``len()``.

Integration point
-----------------
A later pass wires this compiler behind the verified ``replace_*_markdown``
tools in ``server.py`` / ``docs.py``.  At that point the ``UnsupportedMarkdown``
exception raised here is translated to the kernel's ``UNSUPPORTED_MARKDOWN``
error envelope (defined in ``verify.py``, which does not yet exist on main).
Do **not** import ``verify.py`` here.

Supported subset
----------------
- Headings (ATX-style, levels 1–6)
- Bold, italic, bold+italic inline spans
- Hyperlinks
- Unordered lists (``-`` / ``*``) and ordered lists (``1.``), nested
- GFM pipe tables (header row + separator + body rows)

Anything outside this subset — images, code fences, inline code, blockquotes,
horizontal rules, raw HTML, strikethrough — raises :class:`UnsupportedMarkdown`
with the offending construct type and its source line range.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


@dataclass
class UnsupportedMarkdown(Exception):
    """Raised when the markdown contains a construct outside the supported subset.

    Attributes
    ----------
    construct:
        Human-readable name of the offending construct (e.g. ``"image"``,
        ``"code_block"``, ``"blockquote"``).
    source_map:
        ``(start_line, end_line)`` from the markdown-it node's ``.map``
        attribute, or ``None`` if the node has no position information.
    """

    construct: str
    source_map: tuple[int, int] | None = None

    def __str__(self) -> str:
        loc = f" at lines {self.source_map[0] + 1}-{self.source_map[1]}" if self.source_map else ""
        return f"Unsupported markdown construct: {self.construct!r}{loc}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_markdown(
    source: str,
    *,
    start_index: int = 1,
) -> list[dict[str, Any]]:
    """Compile *source* markdown to an ordered list of batchUpdate request dicts.

    Parameters
    ----------
    source:
        Markdown text to compile.  Must contain only the supported subset;
        anything else raises :class:`UnsupportedMarkdown`.
    start_index:
        The Docs API index at which the compiled content will be inserted.
        Defaults to ``1`` (start of a document body).  Pass the index returned
        by ``find_sections`` or ``replace_range_markdown`` when inserting into
        an existing document.

    Returns
    -------
    list[dict]
        Ordered sequence of batchUpdate request dicts ready to be passed to
        ``docs.batchUpdate(body={"requests": result})``.  The list is empty for
        empty or whitespace-only input.

    Raises
    ------
    UnsupportedMarkdown
        If *source* contains any construct outside the supported subset.

    Notes
    -----
    All index values in the returned requests are expressed in UTF-16 code
    units, consistent with the Google Docs API contract.
    """
    if not source or not source.strip():
        return []

    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(source)
    tree = SyntaxTreeNode(tokens)

    compiler = _Compiler(start_index=start_index)
    compiler.visit_children(tree)
    return compiler.build_requests()


# ---------------------------------------------------------------------------
# UTF-16 helpers
# ---------------------------------------------------------------------------


def _utf16_len(text: str) -> int:
    """Return the number of UTF-16 code units in *text*.

    This is what the Google Docs API counts, not ``len(text)``.
    """
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# Table geometry (shared with index_sim.py — single source of truth)
# ---------------------------------------------------------------------------
#
# Pinned by the live contract test
# tests/live/test_markdown_writes.py::TestTableGeometryProbe, which inserts
# raw empty insertTable requests and reads back the real per-cell indices.
# index_sim.py imports these instead of re-deriving the formula, so the
# simulator and the compiler can never silently drift apart.


def _table_stride(n_cols: int) -> int:
    """Index distance between the start of one table row and the next."""
    return n_cols * 2 + 1


def _table_structural_size(n_rows: int, n_cols: int) -> int:
    """Total index span of a freshly-created *empty* table, from its
    ``insertTable`` location index to just past its table-end marker.

      1  for the leading newline / paragraph the API inserts
      1  for the table-start marker
      n_rows * stride for the row/cell structure
      1  for the table-end marker
    """
    return 1 + 1 + n_rows * _table_stride(n_cols) + 1


def _table_cell_index(table_start: int, n_cols: int, r_idx: int, c_idx: int) -> int:
    """The paragraph start index of cell (r_idx, c_idx) in a freshly-created
    table whose structural start (T = insertTable location index + 1) is
    *table_start*."""
    return table_start + 3 + r_idx * _table_stride(n_cols) + c_idx * 2


# ---------------------------------------------------------------------------
# Internal compiler
# ---------------------------------------------------------------------------

# Block-level node types that this compiler handles explicitly.
_SUPPORTED_BLOCK_TYPES = frozenset(
    {
        "root",
        "heading",
        "paragraph",
        "bullet_list",
        "ordered_list",
        "list_item",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        # Inline types encountered while walking block nodes:
        "inline",
        "text",
        "softbreak",
        "hardbreak",
        "strong",
        "em",
        "link",
        "html_inline",
        # table content wrappers
        "fence",  # will be rejected below
    }
)

# Node types that must be explicitly rejected (outside the supported subset).
_REJECTED_NODE_TYPES = frozenset(
    {
        "fence",
        "code_block",
        "code_inline",
        "blockquote",
        "hr",
        "image",
        "html_block",
        "html_inline",
        "math_block",
        "math_inline",
        "s",  # strikethrough (markdown-it GFM extension)
    }
)


@dataclass
class _StyleSpan:
    """A pending style request to apply after all text has been inserted."""

    start: int
    end: int
    bold: bool = False
    italic: bool = False
    link_url: str = ""


@dataclass
class _ParagraphStyle:
    """A pending paragraph-level style request."""

    start: int
    end: int
    kind: str  # "heading", "bullets"
    level: int = 0  # 1-6 for headings; nesting depth for bullets
    ordered: bool = False


class _Compiler:
    """Stateful walker that accumulates batchUpdate requests."""

    def __init__(self, start_index: int) -> None:
        self._cursor: int = start_index
        # Accumulated insertText requests (in order).
        self._inserts: list[dict[str, Any]] = []
        # Style requests to emit after all inserts.
        self._style_spans: list[_StyleSpan] = []
        self._para_styles: list[_ParagraphStyle] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def build_requests(self) -> list[dict[str, Any]]:
        """Return the fully assembled request list."""
        requests: list[dict[str, Any]] = []
        requests.extend(self._inserts)
        for ps in self._para_styles:
            requests.extend(self._make_para_style_requests(ps))
        for ss in self._style_spans:
            requests.extend(self._make_text_style_requests(ss))
        return requests

    # ------------------------------------------------------------------
    # Tree walker
    # ------------------------------------------------------------------

    def visit_children(self, node: SyntaxTreeNode) -> None:
        for child in node.children:
            self._visit(child)

    def _visit(self, node: SyntaxTreeNode) -> None:
        t = node.type

        if t in _REJECTED_NODE_TYPES:
            raise UnsupportedMarkdown(construct=t, source_map=node.map)

        if t == "heading":
            self._visit_heading(node)
        elif t == "paragraph":
            self._visit_paragraph(node)
        elif t in ("bullet_list", "ordered_list"):
            self._visit_list(node, ordered=(t == "ordered_list"), nesting=0)
        elif t == "table":
            self._visit_table(node)
        elif t == "root":
            self.visit_children(node)
        elif t in (
            "inline",
            "text",
            "softbreak",
            "hardbreak",
            "strong",
            "em",
            "link",
            "html_inline",
            "thead",
            "tbody",
            "tr",
            "td",
            "th",
            "list_item",
        ):
            # These are handled by their parent visitors; seeing them at the
            # top level is unexpected but harmless — recurse.
            self.visit_children(node)
        else:
            raise UnsupportedMarkdown(construct=t, source_map=node.map)

    # ------------------------------------------------------------------
    # Heading
    # ------------------------------------------------------------------

    def _visit_heading(self, node: SyntaxTreeNode) -> None:
        # node.markup is "#", "##", etc.
        level = len(node.markup)  # number of # characters
        para_start = self._cursor
        text = self._collect_inline_text(node)
        self._insert_text(text + "\n")
        para_end = self._cursor
        self._para_styles.append(
            _ParagraphStyle(start=para_start, end=para_end, kind="heading", level=level)
        )

    # ------------------------------------------------------------------
    # Paragraph
    # ------------------------------------------------------------------

    def _visit_paragraph(self, node: SyntaxTreeNode) -> None:
        para_start = self._cursor
        self._visit_inline_content(node, para_start)
        self._insert_text("\n")

    # ------------------------------------------------------------------
    # Inline content
    # ------------------------------------------------------------------

    def _visit_inline_content(self, node: SyntaxTreeNode, para_start: int) -> None:
        """Walk inline children, emitting insertText and recording style spans."""
        for child in node.children:
            self._visit_inline_node(child)

    def _visit_inline_node(self, node: SyntaxTreeNode) -> None:
        t = node.type

        if t in _REJECTED_NODE_TYPES:
            raise UnsupportedMarkdown(construct=t, source_map=node.map)

        if t in ("text", "softbreak", "hardbreak"):
            if t == "softbreak":
                self._insert_text(" ")
            elif t == "hardbreak":
                self._insert_text("\n")
            else:
                self._insert_text(node.content)
        elif t == "strong":
            span_start = self._cursor
            for child in node.children:
                self._visit_inline_node(child)
            span_end = self._cursor
            if span_end > span_start:
                self._style_spans.append(_StyleSpan(start=span_start, end=span_end, bold=True))
        elif t == "em":
            span_start = self._cursor
            for child in node.children:
                self._visit_inline_node(child)
            span_end = self._cursor
            if span_end > span_start:
                self._style_spans.append(_StyleSpan(start=span_start, end=span_end, italic=True))
        elif t == "link":
            url = str(node.attrGet("href") or "")
            span_start = self._cursor
            for child in node.children:
                self._visit_inline_node(child)
            span_end = self._cursor
            if span_end > span_start:
                self._style_spans.append(_StyleSpan(start=span_start, end=span_end, link_url=url))
        elif t == "code_inline":
            raise UnsupportedMarkdown(construct="code_inline", source_map=node.map)
        elif t == "image":
            raise UnsupportedMarkdown(construct="image", source_map=node.map)
        elif t == "html_inline":
            raise UnsupportedMarkdown(construct="html_inline", source_map=node.map)
        elif t in ("s",):
            raise UnsupportedMarkdown(construct=t, source_map=node.map)
        elif t == "inline":
            for child in node.children:
                self._visit_inline_node(child)
        else:
            # Any unrecognised inline type is rejected.
            raise UnsupportedMarkdown(construct=t, source_map=node.map)

    def _collect_inline_text(self, node: SyntaxTreeNode) -> str:
        """Collect the inline text content of *node* and emit insertText + style spans.

        Returns the plain text (used for computing lengths after insertion).
        The actual inserts are side-effected into self._inserts; style spans
        into self._style_spans.
        """
        self._visit_inline_content(node, self._cursor)
        return ""  # side-effects already applied

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------

    def _visit_list(self, node: SyntaxTreeNode, ordered: bool, nesting: int) -> None:
        for item in node.children:
            if item.type != "list_item":
                raise UnsupportedMarkdown(construct=item.type, source_map=item.map)
            self._visit_list_item(item, ordered=ordered, nesting=nesting)

    def _visit_list_item(self, item: SyntaxTreeNode, ordered: bool, nesting: int) -> None:
        for child in item.children:
            if child.type == "paragraph":
                para_start = self._cursor
                self._visit_inline_content(child, para_start)
                self._insert_text("\n")
                para_end = self._cursor
                self._para_styles.append(
                    _ParagraphStyle(
                        start=para_start,
                        end=para_end,
                        kind="bullets",
                        level=nesting,
                        ordered=ordered,
                    )
                )
            elif child.type in ("bullet_list", "ordered_list"):
                self._visit_list(
                    child,
                    ordered=(child.type == "ordered_list"),
                    nesting=nesting + 1,
                )
            else:
                raise UnsupportedMarkdown(construct=child.type, source_map=child.map)

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def _visit_table(self, node: SyntaxTreeNode) -> None:
        """Compile a GFM table into insertTable + insertText requests.

        Strategy:
        - Determine the table dimensions (rows x cols) from the AST.
        - Emit ``insertTable`` at the current cursor.  The Docs API inserts a
          leading newline, so the table's structural start index is cursor+1.
        - After the table is created the API lays out the table in row-major
          order.  Each cell contains exactly one empty paragraph.
        - We then populate cell text in **reverse** (highest index first) so
          that earlier insertions do not shift later indices.

        Docs API table index layout — pinned by the live contract test
        ``tests/live/test_markdown_writes.py::TestTableGeometryProbe``, which
        inserts raw empty tables and reads back the real per-cell indices:
          table_start (T) = insert_index + 1        (leading \\n)
          stride          = cols * 2 + 1
          row_start(r)    = T + 1 + r * stride
          cell(r, c) paragraph index = T + 3 + r * stride + c * 2
          table_end (empty table)    = T + rows * stride + 2

        Because every cell-text ``insertText`` lands in the *same*
        batchUpdate as ``insertTable`` and Docs applies requests
        sequentially, two further adjustments are needed beyond the
        per-cell index itself:

        1. Post-table cursor: content that follows the table in this batch
           executes *after* every cell insertion, so its index must include
           the total UTF-16 length of all inserted cell text — not just the
           empty table's structural size.
        2. Intra-cell style spans: cells are populated highest-index first so
           that inserting into one cell never shifts a not-yet-inserted
           cell. But by the same token, once a lower-index cell's text is
           inserted, it shifts every already-inserted higher-index cell's
           text forward. Style spans (bold/italic/link) are applied via
           ``updateTextStyle`` *after* every insert in the batch, so each
           cell's spans must be shifted forward by the cumulative length of
           every lower-index cell's text.
        """
        # Collect all rows in document order.
        rows: list[list[SyntaxTreeNode]] = []
        for section in node.children:
            if section.type in ("thead", "tbody"):
                for tr in section.children:
                    if tr.type == "tr":
                        rows.append(tr.children)
        if not rows:
            return

        n_rows = len(rows)
        n_cols = max(len(row) for row in rows)

        # Normalise all rows to n_cols cells.
        rows_normalised: list[list[SyntaxTreeNode | None]] = [
            list(row) + [None] * (n_cols - len(row)) for row in rows
        ]

        insert_at = self._cursor

        # Emit insertTable. The API inserts a paragraph before the table,
        # advancing the cursor by 1, then the table body itself.
        self._inserts.append(
            {
                "insertTable": {
                    "rows": n_rows,
                    "columns": n_cols,
                    "location": {"index": insert_at},
                }
            }
        )

        table_start = insert_at + 1  # T: the leading \n bumps actual table start by 1
        table_structural_size = _table_structural_size(n_rows, n_cols)

        # Cell text insertions, collected in ascending (row-major) index
        # order first, then inserted in reverse (highest index first) so
        # that inserting into one cell never shifts a not-yet-inserted cell.
        cell_insertions: list[tuple[int, str, list[_StyleSpan]]] = []

        for r_idx, row in enumerate(rows_normalised):
            for c_idx, cell_node in enumerate(row):
                cell_para_index = _table_cell_index(table_start, n_cols, r_idx, c_idx)
                if cell_node is None:
                    continue
                # Collect inline text for this cell.
                cell_text, cell_spans = self._compile_cell(cell_node, cell_para_index)
                if cell_text:
                    cell_insertions.append((cell_para_index, cell_text, cell_spans))

        # Cumulative UTF-16 length of every cell already inserted, in
        # ascending index order. A cell's already-placed text is shifted
        # forward by this amount once every lower-index cell (which is
        # inserted *after* it, since we insert highest-index first) has
        # landed. Style spans must be adjusted by this shift before being
        # queued, because updateTextStyle requests run after every insert.
        cumulative_shift = 0
        shifted_style_spans: list[_StyleSpan] = []
        for _cell_idx, cell_text, cell_spans in cell_insertions:
            for span in cell_spans:
                shifted_style_spans.append(
                    _StyleSpan(
                        start=span.start + cumulative_shift,
                        end=span.end + cumulative_shift,
                        bold=span.bold,
                        italic=span.italic,
                        link_url=span.link_url,
                    )
                )
            cumulative_shift += _utf16_len(cell_text)
        total_cell_text_length = cumulative_shift

        # Reverse so we insert from the highest index downward.
        for cell_idx, cell_text, _cell_spans in reversed(cell_insertions):
            self._inserts.append(
                {
                    "insertText": {
                        "text": cell_text,
                        "location": {"index": cell_idx},
                    }
                }
            )
        self._style_spans.extend(shifted_style_spans)

        # Advance the main cursor past the whole table *and* every cell's
        # inserted text (see docstring point 1).
        self._cursor = insert_at + table_structural_size + total_cell_text_length

    def _compile_cell(
        self, cell_node: SyntaxTreeNode, base_index: int
    ) -> tuple[str, list[_StyleSpan]]:
        """Compile the inline content of a table cell.

        Returns the plain text content (without trailing newline) and any style
        spans with indices anchored to *base_index*.
        """
        # Temporarily fork the cursor to collect cell content.
        saved_cursor = self._cursor
        saved_inserts = self._inserts
        saved_spans = self._style_spans
        saved_para_styles = self._para_styles

        self._cursor = base_index
        self._inserts = []
        self._style_spans = []
        self._para_styles = []

        # Cell nodes may be <td> or <th>; their children are inline nodes.
        for child in cell_node.children:
            if child.type == "inline":
                for inline_child in child.children:
                    self._visit_inline_node(inline_child)
            elif child.type == "paragraph":
                self._visit_inline_content(child, self._cursor)
            else:
                # Tables within tables and other block elements are not
                # supported in the write-direction subset.
                raise UnsupportedMarkdown(construct=child.type, source_map=child.map)

        cell_text = "".join(
            req["insertText"]["text"] for req in self._inserts if "insertText" in req
        )
        cell_spans = list(self._style_spans)

        # Restore state.
        self._cursor = saved_cursor
        self._inserts = saved_inserts
        self._style_spans = saved_spans
        self._para_styles = saved_para_styles

        return cell_text, cell_spans

    # ------------------------------------------------------------------
    # Low-level insert
    # ------------------------------------------------------------------

    def _insert_text(self, text: str) -> None:
        if not text:
            return
        self._inserts.append(
            {
                "insertText": {
                    "text": text,
                    "location": {"index": self._cursor},
                }
            }
        )
        self._cursor += _utf16_len(text)

    # ------------------------------------------------------------------
    # Request builders
    # ------------------------------------------------------------------

    def _make_para_style_requests(self, ps: _ParagraphStyle) -> list[dict[str, Any]]:
        if ps.kind == "heading":
            named_style = f"HEADING_{ps.level}"
            return [
                {
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": ps.start,
                            "endIndex": ps.end,
                        },
                        "paragraphStyle": {"namedStyleType": named_style},
                        "fields": "namedStyleType",
                    }
                }
            ]
        if ps.kind == "bullets":
            return [
                {
                    "createParagraphBullets": {
                        "range": {
                            "startIndex": ps.start,
                            "endIndex": ps.end,
                        },
                        "bulletPreset": (
                            "NUMBERED_DECIMAL_ALPHA_ROMAN"
                            if ps.ordered
                            else "BULLET_DISC_CIRCLE_SQUARE"
                        ),
                    }
                }
            ]
        return []

    def _make_text_style_requests(self, ss: _StyleSpan) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        range_dict = {"startIndex": ss.start, "endIndex": ss.end}

        if ss.bold or ss.italic:
            style: dict[str, Any] = {}
            fields_parts: list[str] = []
            if ss.bold:
                style["bold"] = True
                fields_parts.append("bold")
            if ss.italic:
                style["italic"] = True
                fields_parts.append("italic")
            requests.append(
                {
                    "updateTextStyle": {
                        "range": range_dict,
                        "textStyle": style,
                        "fields": ",".join(fields_parts),
                    }
                }
            )

        if ss.link_url:
            requests.append(
                {
                    "updateTextStyle": {
                        "range": range_dict,
                        "textStyle": {"link": {"url": ss.link_url}},
                        "fields": "link",
                    }
                }
            )

        return requests
