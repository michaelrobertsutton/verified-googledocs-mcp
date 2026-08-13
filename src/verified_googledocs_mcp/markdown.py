"""Docs JSON → markdown converter (read direction only).

Drive's files.export cannot scope to a single tab, which is why this converter
exists instead of using the native export. Every tool that returns markdown for
a specific tab comes through here.

Supported subset: headings, bold, italic, lists (bulleted and ordered), tables,
hyperlinks.

Out-of-subset elements (inline images, smart chips, footnotes) are emitted as
stable placeholder tokens so callers can round-trip the content without silent
data loss:
  [image:objectId]
  [chip:person:email]      (person chip)
  [chip:smart:type]        (other smart chip)
  [footnote:footnoteId]

Each call returns the rendered markdown string and a list of lossy_elements
describing what was replaced by placeholders. The lossy_elements list follows
the markdown-it-py token conventions (type/tag keys) so the downstream AST
diff in M4 can match placeholders against their originals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LossyElement:
    """A document element that cannot be represented in markdown."""

    kind: str  # "image", "chip", "footnote"
    placeholder: str  # the token that appears in the markdown
    context: dict[str, Any] = field(default_factory=dict)


def to_markdown(
    tab_body: dict[str, Any], lists: dict[str, Any] | None = None
) -> tuple[str, list[LossyElement]]:
    """Convert a Docs API tab body dict to (markdown_string, lossy_elements).

    Use this instead of Drive files.export whenever you need markdown for a
    specific tab — Drive export has no tab parameter.

    tab_body: the value of document['documentTab']['body'] (or the top-level
    'body' for tabless docs treated as a single implicit tab).
    lists: the tab's ``lists`` map (listId -> List), from
    ``docs._find_tab_lists``. A paragraph's ``bullet`` never carries the
    glyph type that distinguishes ordered from unordered (confirmed live —
    see ``tests/live/test_markdown_writes.py::TestBulletNestingProbe``); that
    lives here. Omitting it (the default) means every list renders as
    unordered — the pre-#65 behaviour — which is why every mutation
    pipeline that calls this for real content now passes it explicitly.
    """
    converter = _Converter(lists=lists)
    md = converter.convert(tab_body)
    return md, converter.lossy_elements


# ---------------------------------------------------------------------------
# Internal converter
# ---------------------------------------------------------------------------

# glyphType values the Docs API uses for a numbered list level. An unordered
# level instead carries a glyphSymbol and no glyphType key at all (confirmed
# live). Membership in this set — never a fallback-to-unordered default — is
# how an ordered list level is detected.
_ORDERED_GLYPH_TYPES = frozenset(
    {"DECIMAL", "ALPHA", "ROMAN", "UPPER_ALPHA", "UPPER_ROMAN", "ZERO_DECIMAL"}
)


@dataclass
class _ListFrame:
    """Sequential-numbering state for one active nesting level while
    converting a run of list-item paragraphs back to markdown."""

    list_id: str
    level: int
    ordered: bool
    counter: int
    indent: int  # leading-space count for THIS level's own marker


class _Converter:
    def __init__(self, lists: dict[str, Any] | None = None) -> None:
        self.lossy_elements: list[LossyElement] = []
        self._lists = lists or {}
        # Stack of active _ListFrame, shallowest first. Keyed on (list_id,
        # level) rather than reset on every non-list block: this lets a list
        # that Docs' own "continue numbering" feature split across other
        # content resume its counter, matching Docs' own semantics, while a
        # *different* list_id at the same level still starts fresh.
        self._list_stack: list[_ListFrame] = []

    def convert(self, body: dict[str, Any]) -> str:
        # Each rendered block is tracked with whether it is a list item so that
        # consecutive list items stay tight (one newline) while distinct
        # block-level elements are separated by a blank line. A single newline
        # between two paragraphs is a soft break in markdown and would be
        # re-parsed as one paragraph — that collapse is the #36 false negative.
        rendered: list[tuple[bool, str]] = []
        for content in body.get("content", []):
            chunk = self._structural_element(content)
            if not chunk:  # skip None and blank paragraphs (spacing only)
                continue
            is_list_item = "paragraph" in content and content["paragraph"].get("bullet") is not None
            rendered.append((is_list_item, chunk))

        if not rendered:
            return ""

        out: list[str] = [rendered[0][1]]
        for idx in range(1, len(rendered)):
            prev_is_list_item = rendered[idx - 1][0]
            cur_is_list_item, chunk = rendered[idx]
            separator = "\n" if (prev_is_list_item and cur_is_list_item) else "\n\n"
            out.append(separator)
            out.append(chunk)
        return "".join(out).strip() + "\n"

    # ------------------------------------------------------------------
    # Structural elements
    # ------------------------------------------------------------------

    def _structural_element(self, elem: dict[str, Any]) -> str | None:
        if "paragraph" in elem:
            return self._paragraph(elem["paragraph"])
        if "table" in elem:
            return self._table(elem["table"])
        if "tableOfContents" in elem:
            # Treat a table of contents as an ignored structural element; it
            # renders dynamically in Docs and cannot be faithfully expressed.
            return None
        if "sectionBreak" in elem:
            return None
        return None

    # ------------------------------------------------------------------
    # Paragraphs
    # ------------------------------------------------------------------

    def _paragraph(self, para: dict[str, Any]) -> str:
        style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        elements = para.get("elements", [])
        inline = self._inline_elements(elements)

        # Blank paragraph — emit empty line for paragraph break
        if not inline.strip():
            return ""

        # Headings
        if style.startswith("HEADING_"):
            level = int(style.split("_")[1])
            return "#" * level + " " + inline

        # List items
        bullet = para.get("bullet")
        if bullet is not None:
            return self._list_item(para, bullet, inline)

        return inline

    def _list_item(self, para: dict[str, Any], bullet: dict[str, Any], inline: str) -> str:
        """Render a paragraph's bullet as a markdown list marker.

        Ordered vs unordered is read from ``self._lists[listId]`` — never
        from ``bullet`` itself, which the Docs API never populates with a
        glyph type (issue #65; a ``Bullet`` carries only ``listId`` /
        ``nestingLevel`` / ``textStyle``). Missing from ``self._lists``
        (e.g. no lists map was supplied) falls back to unordered, matching
        the pre-#65 behaviour rather than guessing.

        Numbering is reconstructed sequentially — see ``_advance_list_item``
        — and does not reproduce a list's ``startNumber`` if it isn't 1:
        markdown has no portable way to carry that through the
        block-structural comparison used to verify writes, so a list seeded
        to start at a non-default number reads back renumbered from 1. This
        is a documented limitation, not a round-trip bug this issue covers.
        """
        list_id = bullet.get("listId", "")
        nesting = bullet.get("nestingLevel", 0)
        ordered = self._is_ordered_level(list_id, nesting)
        indent, counter = self._advance_list_item(list_id, nesting, ordered)

        if ordered:
            return f"{indent}{counter}. {inline}"
        return f"{indent}- {inline}"

    def _is_ordered_level(self, list_id: str, nesting: int) -> bool:
        nesting_levels = (
            self._lists.get(list_id, {}).get("listProperties", {}).get("nestingLevels", [])
        )
        if nesting >= len(nesting_levels):
            return False
        return nesting_levels[nesting].get("glyphType") in _ORDERED_GLYPH_TYPES

    def _advance_list_item(self, list_id: str, level: int, ordered: bool) -> tuple[str, int]:
        """Advance (or start) the counter for this (list_id, level) and
        return (leading-space indent, 1-based counter) for the current item.

        Indent is the sum of every ancestor's own marker width (e.g. after
        "1. " a child indents by 3, after "- " by 2) — not a fixed
        ``"  " * nesting`` — because CommonMark nesting is determined by
        whether a child's indent reaches the column where its parent's own
        content starts. A fixed 2-space indent under a "1. " parent (content
        starts at column 3) does not re-parse as nested.
        """
        stack = self._list_stack
        # Pop every frame deeper than this level, and — if the frame *at*
        # this level belongs to a different list — pop that too, so a new
        # list at the same nesting depth starts its own counter at 1 rather
        # than continuing a prior, unrelated list's count.
        while stack and stack[-1].level >= level:
            if stack[-1].level == level and stack[-1].list_id == list_id:
                break
            stack.pop()

        if stack and stack[-1].level == level:
            frame = stack[-1]
            frame.counter += 1
            frame.ordered = ordered
        else:
            parent_indent = 0
            if stack:
                parent = stack[-1]
                marker = f"{parent.counter}. " if parent.ordered else "- "
                parent_indent = parent.indent + len(marker)
            frame = _ListFrame(
                list_id=list_id, level=level, ordered=ordered, counter=1, indent=parent_indent
            )
            stack.append(frame)

        return " " * frame.indent, frame.counter

    # ------------------------------------------------------------------
    # Inline elements within a paragraph
    # ------------------------------------------------------------------

    def _inline_elements(self, elements: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for elem in elements:
            chunk = self._inline_element(elem)
            if chunk is not None:
                parts.append(chunk)
        return "".join(parts)

    def _inline_element(self, elem: dict[str, Any]) -> str | None:
        if "textRun" in elem:
            return self._text_run(elem["textRun"])
        if "inlineObjectElement" in elem:
            return self._inline_object(elem["inlineObjectElement"])
        if "person" in elem:
            return self._person_chip(elem["person"])
        if "richLink" in elem:
            return self._rich_link(elem["richLink"])
        if "footnoteReference" in elem:
            return self._footnote_ref(elem["footnoteReference"])
        if "autoText" in elem:
            # Page numbers, section numbers — skip silently.
            return None
        return None

    def _text_run(self, run: dict[str, Any]) -> str:
        text = run.get("content", "")
        # Strip the trailing newline that terminates every paragraph element.
        if text.endswith("\n"):
            text = text[:-1]
        if not text:
            return ""

        style = run.get("textStyle", {})
        link = style.get("link", {}).get("url", "")

        # Apply bold/italic markers around the text before optional link wrapping.
        bold = style.get("bold", False)
        italic = style.get("italic", False)

        # Escape markdown special characters in plain text to avoid accidental
        # formatting. Only escape when the text is not being wrapped in markers.
        escaped = _escape_markdown(text)

        if bold and italic:
            inner = f"***{escaped}***"
        elif bold:
            inner = f"**{escaped}**"
        elif italic:
            inner = f"*{escaped}*"
        else:
            inner = escaped

        if link:
            return f"[{inner}]({link})"
        return inner

    def _inline_object(self, obj_elem: dict[str, Any]) -> str:
        obj_id = obj_elem.get("inlineObjectId", "unknown")
        placeholder = f"[image:{obj_id}]"
        self.lossy_elements.append(
            LossyElement(kind="image", placeholder=placeholder, context={"objectId": obj_id})
        )
        return placeholder

    def _person_chip(self, person: dict[str, Any]) -> str:
        email = person.get("personProperties", {}).get("email", "unknown")
        placeholder = f"[chip:person:{email}]"
        self.lossy_elements.append(
            LossyElement(kind="chip", placeholder=placeholder, context={"email": email})
        )
        return placeholder

    def _rich_link(self, link: dict[str, Any]) -> str:
        props = link.get("richLinkProperties", {})
        uri = props.get("uri", "")
        title = props.get("title", uri)
        # Rich links (Drive file chips, etc.) that are not plain URLs are
        # rendered as smart chips.
        if uri:
            return f"[{_escape_markdown(title)}]({uri})"
        chip_type = props.get("mimeType", "smart")
        placeholder = f"[chip:smart:{chip_type}]"
        self.lossy_elements.append(
            LossyElement(kind="chip", placeholder=placeholder, context={"props": props})
        )
        return placeholder

    def _footnote_ref(self, ref: dict[str, Any]) -> str:
        fn_id = ref.get("footnoteId", "unknown")
        placeholder = f"[footnote:{fn_id}]"
        self.lossy_elements.append(
            LossyElement(kind="footnote", placeholder=placeholder, context={"footnoteId": fn_id})
        )
        return placeholder

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def _table(self, table: dict[str, Any]) -> str:
        rows = table.get("tableRows", [])
        if not rows:
            return ""

        md_rows: list[list[str]] = []
        for row in rows:
            cells = row.get("tableCells", [])
            md_cells: list[str] = []
            for cell in cells:
                # Each cell has its own content list of structural elements.
                cell_body = {"content": cell.get("content", [])}
                cell_converter = _Converter(lists=self._lists)
                cell_md = cell_converter.convert(cell_body).strip()
                # Propagate lossy elements from the nested conversion.
                self.lossy_elements.extend(cell_converter.lossy_elements)
                # Inline newlines in cell content to keep table rows on one line.
                cell_md = cell_md.replace("\n", " ")
                md_cells.append(cell_md)
            md_rows.append(md_cells)

        if not md_rows:
            return ""

        # Normalise column count across all rows.
        col_count = max(len(r) for r in md_rows)
        normalised = [r + [""] * (col_count - len(r)) for r in md_rows]

        # Build pipe table. First row is treated as header.
        header = normalised[0]
        separator = ["---"] * col_count
        body = normalised[1:]

        lines = [_pipe_row(header), _pipe_row(separator)]
        for row in body:
            lines.append(_pipe_row(row))
        return "\n".join(lines)


def _pipe_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


# Characters that must be escaped inside plain markdown text to prevent
# unintended formatting. We only escape the minimal set that could cause
# ambiguity; inside bold/italic spans the delimiters are already explicit.
_ESCAPE_CHARS = r"\`*_{}[]<>#+-.!|"


def _escape_markdown(text: str) -> str:
    result: list[str] = []
    for ch in text:
        if ch in _ESCAPE_CHARS:
            result.append("\\" + ch)
        else:
            result.append(ch)
    return "".join(result)
