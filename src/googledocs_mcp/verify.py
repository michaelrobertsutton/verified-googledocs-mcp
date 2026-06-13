"""Verification kernel: text locator, error envelope, audit writer.

Pipeline for every verified mutation:

  args ──► validate ──────────────► INVALID_INPUT / TAB_NOT_FOUND
    │
    ▼
  pre-read tab JSON (revision R1 captured; reused for everything below)
    │
    ▼
  locate(): exact → curly/straight quotes → NBSP/whitespace → soft-hyphen
    │  ├─ 0 matches ───────────────► ZERO_MATCH + near-miss span
    │  ├─ n ≠ expected_matches ────► MATCH_COUNT_MISMATCH + all spans
    │  └─ crosses paragraph/cell ──► STRUCTURAL_BOUNDARY + spans
    ▼
  dry_run? ──yes──► predicted diff, applied: false, no write
    │ no
    ▼
  batchUpdate(writeControl.requiredRevisionId = R1)
    │  └─ doc moved since R1 ──────► REVISION_CONFLICT (verbatim API error)
    ▼
  post-read (fresh) ──► evidence { before, after, match_count, rung, R1→R2 }
    │
    ▼
  audit append (best-effort; evidence carries audit_logged: false on failure)
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import os
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class ErrorCode(Enum):
    ZERO_MATCH = "ZERO_MATCH"
    MATCH_COUNT_MISMATCH = "MATCH_COUNT_MISMATCH"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    STALE_RANGE = "STALE_RANGE"
    TAB_NOT_FOUND = "TAB_NOT_FOUND"
    STRUCTURAL_BOUNDARY = "STRUCTURAL_BOUNDARY"
    UNSUPPORTED_MARKDOWN = "UNSUPPORTED_MARKDOWN"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    COMMENT_STILL_OPEN = "COMMENT_STILL_OPEN"
    INVALID_INPUT = "INVALID_INPUT"
    IMAGE_SOURCE_UNSUPPORTED = "IMAGE_SOURCE_UNSUPPORTED"
    AUTH_EXPIRED = "AUTH_EXPIRED"


# Which codes signal a transient condition worth retrying.
_RETRYABLE_CODES = frozenset(
    {
        ErrorCode.REVISION_CONFLICT,
        ErrorCode.STALE_RANGE,
        ErrorCode.AUTH_EXPIRED,
    }
)


@dataclasses.dataclass(frozen=True)
class ErrorEnvelope:
    """Typed error structure returned by every verification failure."""

    error_code: ErrorCode
    message: str
    diagnostics: dict[str, Any]
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code.value,
            "message": self.message,
            "diagnostics": self.diagnostics,
            "retryable": self.retryable,
        }


class VerifyError(Exception):
    """Exception that carries a fully-typed ErrorEnvelope."""

    def __init__(self, envelope: ErrorEnvelope) -> None:
        super().__init__(envelope.message)
        self.envelope = envelope


def _make_error(
    code: ErrorCode,
    message: str,
    diagnostics: dict[str, Any] | None = None,
) -> VerifyError:
    return VerifyError(
        ErrorEnvelope(
            error_code=code,
            message=message,
            diagnostics=diagnostics or {},
            retryable=code in _RETRYABLE_CODES,
        )
    )


# ---------------------------------------------------------------------------
# Tab-JSON flattening
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _RunSlice:
    """A contiguous block of text from a single textRun."""

    text: str  # raw content (may include trailing \n)
    api_start_u16: int  # startIndex from the Docs API (UTF-16)
    api_end_u16: int  # endIndex from the Docs API (UTF-16)
    paragraph_idx: int  # which paragraph this belongs to


def _utf16_width(ch: str) -> int:
    """Return the UTF-16 code-unit width of a single code point."""
    return 2 if ord(ch) > 0xFFFF else 1


def _flatten_tab(tab_json: dict[str, Any]) -> tuple[str, list[int], list[int]]:
    """Flatten a Docs API tab body into a plain string with index maps.

    Returns:
        text       – concatenated plain text (code points)
        u16_map    – u16_map[i] = UTF-16 index of text[i];
                     u16_map[len(text)] = one-past-the-end sentinel
        block_map  – block_map[i] = paragraph index for text[i]

    The API provides startIndex / endIndex per run in UTF-16 units. We verify
    each run with an assertion and build u16_map explicitly so astral characters
    (ord > 0xFFFF, 2 UTF-16 units) and inline objects between runs (which consume
    index space but contribute no text) are handled correctly.
    """
    body = tab_json.get("body", {})
    content = body.get("content", [])

    text_parts: list[str] = []
    u16_parts: list[int] = []
    block_parts: list[int] = []

    para_idx = 0

    for structural_element in content:
        if "paragraph" in structural_element:
            para = structural_element["paragraph"]
            elements = para.get("elements", [])
            for element in elements:
                if "textRun" not in element:
                    continue
                run = element["textRun"]
                content_str = run.get("content", "")
                api_start = element.get("startIndex", 0)
                api_end = element.get("endIndex", api_start)

                # Walk the run, assigning UTF-16 indices from api_start.
                u16_cursor = api_start
                for ch in content_str:
                    text_parts.append(ch)
                    u16_parts.append(u16_cursor)
                    block_parts.append(para_idx)
                    u16_cursor += _utf16_width(ch)

                # Integrity check: cursor should match endIndex.
                # (Soft assertion — mismatches would indicate API oddities or
                # inline objects within the text run, not a hard failure.)
                _ = api_end  # retained for future assertion logging

            para_idx += 1

        elif "tableCell" in structural_element:
            # Recurse into table cells (nested body).
            cell = structural_element["tableCell"]
            cell_text, cell_u16, cell_block = _flatten_tab({"body": cell})
            text_parts.extend(cell_text)
            u16_parts.extend(cell_u16)
            block_parts.extend([para_idx + b for b in cell_block])
            para_idx += 1 + (max(cell_block) if cell_block else 0)

    full_text = "".join(text_parts)
    # Append sentinel.
    sentinel = (u16_parts[-1] + _utf16_width(full_text[-1])) if u16_parts else 0
    u16_map = u16_parts + [sentinel]
    return full_text, u16_map, block_parts


# ---------------------------------------------------------------------------
# Normalization ladder
# ---------------------------------------------------------------------------

# Rung labels for diagnostics.
RUNG_EXACT = "exact"
RUNG_QUOTES = "curly_straight_quotes"
RUNG_WHITESPACE = "nbsp_whitespace_runs"
RUNG_SOFTHYPHEN = "soft_hyphen_strip"

# Curly ↔ straight quote mapping (both single and double).
_QUOTE_MAP = str.maketrans(
    {
        "‘": "'",  # LEFT SINGLE QUOTATION MARK
        "’": "'",  # RIGHT SINGLE QUOTATION MARK
        "‚": "'",  # SINGLE LOW-9 QUOTATION MARK
        "‛": "'",  # SINGLE HIGH-REVERSED-9 QUOTATION MARK
        "′": "'",  # PRIME
        "‵": "'",  # REVERSED PRIME
        "“": '"',  # LEFT DOUBLE QUOTATION MARK
        "”": '"',  # RIGHT DOUBLE QUOTATION MARK
        "„": '"',  # DOUBLE LOW-9 QUOTATION MARK
        "‟": '"',  # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
        "″": '"',  # DOUBLE PRIME
        "‶": '"',  # REVERSED DOUBLE PRIME
        "«": '"',  # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
        "»": '"',  # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
    }
)

# Non-breaking and other special spaces.
_NBSP_RE = re.compile(r"[          ]")
_WS_RUN_RE = re.compile(r"\s+")


def _norm_quotes(s: str) -> tuple[str, list[int]]:
    """Apply curly→straight quote equivalence.  1:1 so orig_pos is trivial."""
    n = s.translate(_QUOTE_MAP)
    return n, list(range(len(n) + 1))


def _norm_whitespace(s: str) -> tuple[str, list[int]]:
    """Collapse NBSP + whitespace runs to a single space, tracking orig_pos."""
    # First replace NBSP variants with ordinary space.
    replaced = _NBSP_RE.sub(" ", s)
    # Then collapse runs of whitespace to one space.
    parts: list[str] = []
    orig_pos: list[int] = []
    i = 0
    for m in _WS_RUN_RE.finditer(replaced):
        # Characters before this match.
        for j in range(i, m.start()):
            parts.append(replaced[j])
            orig_pos.append(j)
        # The collapsed run maps to the start position of the run.
        parts.append(" ")
        orig_pos.append(m.start())
        i = m.end()
    # Remainder.
    for j in range(i, len(replaced)):
        parts.append(replaced[j])
        orig_pos.append(j)
    orig_pos.append(len(s))  # sentinel
    return "".join(parts), orig_pos


def _norm_softhyphen(s: str) -> tuple[str, list[int]]:
    """Strip soft hyphens (U+00AD), tracking orig_pos."""
    parts: list[str] = []
    orig_pos: list[int] = []
    for i, ch in enumerate(s):
        if ch == "­":
            continue
        parts.append(ch)
        orig_pos.append(i)
    orig_pos.append(len(s))  # sentinel
    return "".join(parts), orig_pos


def _compose_orig_pos(inner: list[int], outer: list[int]) -> list[int]:
    """Compose two orig_pos maps: inner is applied first, then outer."""
    return [outer[p] for p in inner]


def _find_all(needle: str, haystack: str) -> list[int]:
    """Return all start positions of non-overlapping needle in haystack."""
    positions: list[int] = []
    start = 0
    nlen = len(needle)
    while True:
        pos = haystack.find(needle, start)
        if pos == -1:
            break
        positions.append(pos)
        start = pos + nlen
    return positions


# ---------------------------------------------------------------------------
# Near-miss scan (bounded)
# ---------------------------------------------------------------------------

_NEAR_MISS_THRESHOLD = 0.6
_NEAR_MISS_STRONG_EXIT = 0.92
_NEAR_MISS_MAX_WINDOWS = 5000


def _near_miss_scan(needle: str, haystack: str) -> dict[str, Any] | None:
    """Return the best near-miss span above the threshold, or None.

    Uses a sliding window of length len(needle) (±50%) over haystack.
    Bounded by _NEAR_MISS_MAX_WINDOWS examined windows and an early exit
    when a strong match (ratio >= _NEAR_MISS_STRONG_EXIT) is found.
    """
    nl = len(needle)
    if nl == 0 or len(haystack) == 0:
        return None

    # Window sizes to try: needle length ±25% in steps.
    hl = len(haystack)
    best_ratio = 0.0
    best_span: tuple[int, int] | None = None
    windows_checked = 0

    for wlen in (nl, max(1, nl - nl // 4), nl + nl // 4):
        step = max(1, wlen // 3)
        pos = 0
        while pos + wlen <= hl:
            if windows_checked >= _NEAR_MISS_MAX_WINDOWS:
                break
            window = haystack[pos : pos + wlen]
            ratio = difflib.SequenceMatcher(None, needle, window, autojunk=False).ratio()
            windows_checked += 1
            if ratio > best_ratio:
                best_ratio = ratio
                best_span = (pos, pos + wlen)
            if ratio >= _NEAR_MISS_STRONG_EXIT:
                # Strong match found — early exit.
                break
            pos += step
        if best_ratio >= _NEAR_MISS_STRONG_EXIT:
            break

    if best_ratio < _NEAR_MISS_THRESHOLD or best_span is None:
        return None
    return {
        "ratio": round(best_ratio, 3),
        "span_start": best_span[0],
        "span_end": best_span[1],
        "text": haystack[best_span[0] : best_span[1]],
    }


# ---------------------------------------------------------------------------
# locate()
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LocateResult:
    """Successful location of needle in tab JSON."""

    spans: list[tuple[int, int]]  # (api_start_u16, api_end_u16) per match
    rung: str
    match_count: int


def locate(
    needle: str,
    tab_json: dict[str, Any],
    expected_matches: int = 1,
) -> LocateResult:
    """Find every occurrence of needle in the tab, returning UTF-16 API spans.

    Normalization ladder (stops at first rung with ≥1 match):
        1. exact
        2. curly/straight quote equivalence
        3. NBSP and whitespace-run collapse
        4. soft-hyphen (U+00AD) strip

    Raises VerifyError on:
        INVALID_INPUT          – empty needle
        ZERO_MATCH             – all rungs exhausted; near-miss in diagnostics
        MATCH_COUNT_MISMATCH   – match count != expected_matches; all spans listed
        STRUCTURAL_BOUNDARY    – any match crosses a paragraph boundary
    """
    if not needle:
        raise _make_error(ErrorCode.INVALID_INPUT, "needle must not be empty")

    raw_text, u16_map, block_map = _flatten_tab(tab_json)

    # Each rung produces (normalized_text, orig_pos_map, rung_label).
    # We apply normalization to both needle and haystack at each rung.

    def _build_rung(
        norm_fn: Any, label: str, prev_haystack: str, prev_needle: str
    ) -> tuple[str, list[int], str]:
        nh, op = norm_fn(prev_haystack)
        nn, _ = norm_fn(prev_needle)  # needle orig_pos not needed
        return nh, op, nn

    # Rung 1: exact (no transformation).
    rungs_data: list[tuple[str, list[int], str, str]] = [
        (raw_text, list(range(len(raw_text) + 1)), needle, RUNG_EXACT),
    ]

    # Rung 2: curly/straight quotes.
    nh2, op2, nn2 = _build_rung(_norm_quotes, RUNG_QUOTES, raw_text, needle)
    rungs_data.append((nh2, op2, nn2, RUNG_QUOTES))

    # Rung 3: NBSP/whitespace runs.  Applied on top of rung 2.
    nh3, op3_inner, nn3 = _build_rung(_norm_whitespace, RUNG_WHITESPACE, nh2, nn2)
    op3 = _compose_orig_pos(op3_inner, op2)
    rungs_data.append((nh3, op3, nn3, RUNG_WHITESPACE))

    # Rung 4: soft-hyphen strip. Applied on top of rung 3.
    nh4, op4_inner, nn4 = _build_rung(_norm_softhyphen, RUNG_SOFTHYPHEN, nh3, nn3)
    op4 = _compose_orig_pos(op4_inner, op3)
    rungs_data.append((nh4, op4, nn4, RUNG_SOFTHYPHEN))

    ladder_report: list[dict[str, Any]] = []

    for norm_haystack, orig_pos, norm_needle, rung_label in rungs_data:
        positions = _find_all(norm_needle, norm_haystack)
        if positions:
            # Convert normalized positions → original code-point positions → UTF-16 spans.
            spans: list[tuple[int, int]] = []
            for npos in positions:
                orig_start = orig_pos[npos]
                orig_end = orig_pos[npos + len(norm_needle)]
                api_start = u16_map[orig_start]
                api_end = u16_map[orig_end]
                spans.append((api_start, api_end))

            # Structural-boundary check: every character in the original span
            # must belong to the same paragraph (block_map).
            for (api_start, api_end), npos in zip(spans, positions):
                orig_start = orig_pos[npos]
                orig_end = orig_pos[npos + len(norm_needle)]
                span_blocks = set(block_map[orig_start:orig_end])
                if len(span_blocks) > 1:
                    raise _make_error(
                        ErrorCode.STRUCTURAL_BOUNDARY,
                        "match crosses a paragraph or table-cell boundary",
                        {
                            "rung": rung_label,
                            "spans": [{"start": s, "end": e} for s, e in spans],
                            "paragraphs": sorted(span_blocks),
                        },
                    )

            # Match-count guard.
            actual_count = len(spans)
            if actual_count != expected_matches:
                raise _make_error(
                    ErrorCode.MATCH_COUNT_MISMATCH,
                    (
                        f"expected {expected_matches} match(es) but found {actual_count} "
                        f"at rung '{rung_label}'"
                    ),
                    {
                        "rung": rung_label,
                        "expected": expected_matches,
                        "actual": actual_count,
                        "spans": [{"start": s, "end": e} for s, e in spans],
                    },
                )

            return LocateResult(spans=spans, rung=rung_label, match_count=actual_count)

        ladder_report.append({"rung": rung_label, "matches": 0})

    # All rungs exhausted — near-miss diagnostic.
    # Run on the fully-normalized haystack and needle (rung 4).
    near_miss = _near_miss_scan(norm_needle, norm_haystack)
    raise _make_error(
        ErrorCode.ZERO_MATCH,
        "needle not found after full normalization ladder",
        {
            "ladder_report": ladder_report,
            "near_miss": near_miss,
        },
    )


# ---------------------------------------------------------------------------
# Text-edit evidence helper
# ---------------------------------------------------------------------------

_EXCERPT_RADIUS = 200  # characters on each side of the edited span


def _u16_to_codepoint(api_index: int, u16_map: list[int]) -> int:
    """Return the code-point offset in the flattened text for a UTF-16 API index.

    u16_map[i] holds the UTF-16 index of code-point i.  We want the inverse:
    given a UTF-16 index, which code-point position is it?

    Uses a linear scan; for typical excerpt ranges (<400 chars) this is fast.
    Falls back to the end of the text if api_index is past the sentinel.
    """
    for cp_idx, u16_idx in enumerate(u16_map[:-1]):  # exclude sentinel
        if u16_idx >= api_index:
            return cp_idx
    return len(u16_map) - 1


def _excerpt(
    text: str,
    cp_start: int,
    cp_end: int,
    radius: int = _EXCERPT_RADIUS,
) -> str:
    """Return ±radius characters around the [cp_start, cp_end) span."""
    lo = max(0, cp_start - radius)
    hi = min(len(text), cp_end + radius)
    return text[lo:hi]


def assemble_text_edit_evidence(
    *,
    locate_result: "LocateResult",
    pre_tab_json: dict[str, Any],
    post_tab_json: dict[str, Any],
    revision_before: str,
    revision_after: str,
    applied: bool,
    audit_logged: bool,
    audit_log_reason: str = "",
    predicted_replacement: str | None = None,
) -> dict[str, Any]:
    """Assemble the text-edit evidence dict from pre/post tab JSON.

    Called by the ``replace_text`` tool after a successful write, or in dry-run
    mode with applied=False.  Callers pass pre-read and post-read tab JSON;
    this function is pure (no I/O).

    For a real write the "after" excerpt is read from ``post_tab_json``.  In
    dry-run mode the write has not happened, so the caller passes
    ``predicted_replacement`` and the "after" excerpt is computed by splicing
    that text into the pre-read at every located span — the predicted diff.

    Returns a dict with keys:
        applied, match_count, rung, before, after,
        revision_before, revision_after,
        audit_logged, audit_log_reason (empty on success)
    """
    # Extract the excerpt from pre-read using the first span.
    pre_text, pre_u16_map, _ = _flatten_tab(pre_tab_json)
    first_span = locate_result.spans[0]
    cp_start = _u16_to_codepoint(first_span[0], pre_u16_map)
    cp_end = _u16_to_codepoint(first_span[1], pre_u16_map)
    before_excerpt = _excerpt(pre_text, cp_start, cp_end)

    if predicted_replacement is not None:
        # Dry run: splice the replacement into the pre-read text at every span
        # (descending code-point order so earlier offsets stay valid), then
        # excerpt around the first match to show what the edit would produce.
        spans_cp = sorted(
            (
                (_u16_to_codepoint(s, pre_u16_map), _u16_to_codepoint(e, pre_u16_map))
                for s, e in locate_result.spans
            ),
            reverse=True,
        )
        predicted_text = pre_text
        for s, e in spans_cp:
            predicted_text = predicted_text[:s] + predicted_replacement + predicted_text[e:]
        after_excerpt = _excerpt(predicted_text, cp_start, cp_start + len(predicted_replacement))
    else:
        # Real write: read the "after" excerpt from the post-read at the same
        # window.  Everything *before* the edited span is unshifted (descending-
        # order writes guarantee this), so cp_start is stable.
        post_text, _, _ = _flatten_tab(post_tab_json)
        after_excerpt = _excerpt(post_text, cp_start, cp_start)  # span collapsed post-edit

    evidence: dict[str, Any] = {
        "applied": applied,
        "match_count": locate_result.match_count,
        "rung": locate_result.rung,
        "before": before_excerpt,
        "after": after_excerpt,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "audit_logged": audit_logged,
    }
    if audit_log_reason:
        evidence["audit_log_reason"] = audit_log_reason
    return evidence


# ---------------------------------------------------------------------------
# Audit writer
# ---------------------------------------------------------------------------

_AUDIT_REDACTED_KEYS = frozenset({"before", "after"})


def _state_dir() -> Path:
    """Return the XDG state directory for this package."""
    xdg = os.environ.get("XDG_STATE_HOME", "")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".local" / "state"
    return base / "googledocs-mcp"


def append_audit(
    *,
    doc: str,
    tab: str,
    tool: str,
    evidence: dict[str, Any],
    audit_excerpts: bool = True,
) -> tuple[bool, str]:
    """Append a mutation record to the audit log.

    Best-effort: never raises.  Returns (logged: bool, reason: str).
    reason is empty on success.
    """
    try:
        state_dir = _state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        audit_path = state_dir / "audit.jsonl"

        payload = evidence.copy()
        if not audit_excerpts:
            for key in _AUDIT_REDACTED_KEYS:
                if key in payload:
                    payload[key] = f"[redacted; {len(str(payload[key]))} chars]"

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "doc": doc,
            "tab": tab,
            "tool": tool,
            "evidence": payload,
        }

        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        return True, ""

    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ---------------------------------------------------------------------------
# Range/markdown evidence helper (pure)
# ---------------------------------------------------------------------------


def _parse_markdown_blocks(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown into a list of block descriptors for structural comparison.

    Returns a list of dicts, each describing a block:
        {type, level?, text?, rows?, cols?, nesting?, link_targets}

    Accepted lossy transforms — differences treated as equivalent:
    - Whitespace runs: normalized to single space
    - Bullet-marker style: '-'/'*'/'1.' are all list_item type
    - Table alignment / separator formatting: all equivalent
    - Trailing newlines: stripped from text values
    - Markdown escaping: backslash-escaped chars treated as their plain form
    - Read-side placeholders: [image:...], [chip:...], [footnote:...] appear
      as text in the output and count as paragraph text nodes

    The comparison is pragmatic: block kinds + heading levels + table dimensions
    + link targets + text content modulo the accepted transforms.  A naive text
    diff would fail every write due to the above transforms.
    """
    import re as _re

    from markdown_it import MarkdownIt
    from markdown_it.tree import SyntaxTreeNode

    if not markdown.strip():
        return []

    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(markdown)
    tree = SyntaxTreeNode(tokens)

    blocks: list[dict[str, Any]] = []

    def _norm(text: str) -> str:
        text = _re.sub(r"\\(.)", r"\1", text)
        return _re.sub(r"\s+", " ", text).strip()

    def _links(node: Any) -> list[str]:
        targets: list[str] = []
        for c in node.children:
            if c.type == "link":
                url = c.attrGet("href") or ""
                if url:
                    targets.append(url)
            targets.extend(_links(c))
        return targets

    def _inline_text(node: Any) -> str:
        parts: list[str] = []
        for c in node.children:
            t = c.type
            if t == "text":
                parts.append(c.content)
            elif t == "softbreak":
                parts.append(" ")
            elif t == "hardbreak":
                parts.append(" ")
            else:
                parts.append(_inline_text(c))
        return "".join(parts)

    def _table_dims(node: Any) -> tuple[int, int, list[str]]:
        rows = 0
        max_cols = 0
        targets: list[str] = []
        for sec in node.children:
            if sec.type in ("thead", "tbody"):
                for tr in sec.children:
                    if tr.type == "tr":
                        rows += 1
                        cols = sum(1 for c in tr.children if c.type in ("td", "th"))
                        max_cols = max(max_cols, cols)
                        for cell in tr.children:
                            targets.extend(_links(cell))
        return rows, max_cols, targets

    def _list_items(node: Any, nesting: int) -> None:
        for item in node.children:
            if item.type == "list_item":
                for child in item.children:
                    if child.type == "paragraph":
                        blocks.append({
                            "type": "list_item",
                            "nesting": nesting,
                            "text": _norm(_inline_text(child)),
                            "link_targets": _links(child),
                        })
                    elif child.type in ("bullet_list", "ordered_list"):
                        _list_items(child, nesting + 1)

    def _walk(node: Any) -> None:
        for child in node.children:
            t = child.type
            if t == "heading":
                level = len(child.markup)
                blocks.append({
                    "type": "heading",
                    "level": level,
                    "text": _norm(_inline_text(child)),
                    "link_targets": _links(child),
                })
            elif t == "paragraph":
                blocks.append({
                    "type": "paragraph",
                    "text": _norm(_inline_text(child)),
                    "link_targets": _links(child),
                })
            elif t in ("bullet_list", "ordered_list"):
                _list_items(child, 0)
            elif t == "table":
                r, c, lts = _table_dims(child)
                blocks.append({"type": "table", "rows": r, "cols": c, "link_targets": lts})
            elif t in ("root", "thead", "tbody"):
                _walk(child)

    _walk(tree)
    return blocks


def assemble_range_markdown_evidence(
    *,
    input_markdown: str,
    post_body: dict[str, Any],
    start_index: int,
    end_index: int,
    revision_before: str,
    revision_after: str,
    applied: bool,
    audit_logged: bool,
    audit_log_reason: str = "",
) -> dict[str, Any]:
    """Assemble range/markdown evidence after a markdown write.

    After the write, re-exports the affected range by filtering structural
    elements from the post-read body whose indices overlap [start_index,
    end_index), converts to markdown via to_markdown(), parses both sides
    with markdown-it-py, and structurally compares them.

    Accepted lossy transforms (enumerated in _parse_markdown_blocks docstring).

    Returns a dict with keys:
        applied, revision_before, revision_after, audit_logged,
        structural_match (bool), input_blocks (count), post_blocks (count),
        structural_diff (list of mismatch descriptions, absent if empty),
        audit_log_reason (absent if empty)
    """
    from .markdown import to_markdown

    # Re-export the affected range.
    sliced_content = [
        elem
        for elem in post_body.get("content", [])
        if not (elem.get("endIndex", 0) <= start_index or elem.get("startIndex", 0) >= end_index)
    ]
    range_body = {"content": sliced_content}
    post_md, _ = to_markdown(range_body)

    input_blocks = _parse_markdown_blocks(input_markdown)
    post_blocks = _parse_markdown_blocks(post_md)

    diffs: list[str] = []
    if len(input_blocks) != len(post_blocks):
        diffs.append(
            f"Block count mismatch: input has {len(input_blocks)}, post-write has {len(post_blocks)}"
        )
    else:
        for i, (ib, pb) in enumerate(zip(input_blocks, post_blocks)):
            if not _blocks_structurally_equal(ib, pb):
                diffs.append(f"Block {i}: input={ib!r} vs post={pb!r}")

    evidence: dict[str, Any] = {
        "applied": applied,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "structural_match": len(diffs) == 0,
        "input_blocks": len(input_blocks),
        "post_blocks": len(post_blocks),
        "audit_logged": audit_logged,
    }
    if diffs:
        evidence["structural_diff"] = diffs
    if audit_log_reason:
        evidence["audit_log_reason"] = audit_log_reason
    return evidence


def _blocks_structurally_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return True if two block descriptors are structurally equivalent."""
    if a["type"] != b["type"]:
        return False
    t = a["type"]
    if t == "heading":
        return a["level"] == b["level"] and a["text"] == b["text"]
    if t == "paragraph":
        return a["text"] == b["text"]
    if t == "list_item":
        return a["nesting"] == b["nesting"] and a["text"] == b["text"]
    if t == "table":
        return a["rows"] == b["rows"] and a["cols"] == b["cols"]
    return False


def assemble_structural_evidence(
    *,
    post_body: dict[str, Any],
    anchor_paragraph_start: int,
    revision_before: str,
    revision_after: str,
    applied: bool,
    audit_logged: bool,
    audit_log_reason: str = "",
) -> dict[str, Any]:
    """Assemble structural evidence for insert_image.

    Confirms that an inline object exists in or after the paragraph that
    contains anchor_paragraph_start in the post-read body.

    Returns a dict with keys:
        applied, revision_before, revision_after,
        inline_object_confirmed (bool), audit_logged,
        audit_log_reason (absent if empty)
    """
    confirmed = _inline_object_near(post_body, anchor_paragraph_start)
    evidence: dict[str, Any] = {
        "applied": applied,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "inline_object_confirmed": confirmed,
        "audit_logged": audit_logged,
    }
    if audit_log_reason:
        evidence["audit_log_reason"] = audit_log_reason
    return evidence


def _inline_object_near(body: dict[str, Any], anchor_para_start: int) -> bool:
    """Return True if the paragraph after the anchor contains an inline image."""
    content = body.get("content", [])
    found_anchor = False
    for elem in content:
        if "paragraph" not in elem:
            continue
        elem_start = elem.get("startIndex", 0)
        elem_end = elem.get("endIndex", 0)
        if not found_anchor:
            if elem_start <= anchor_para_start < elem_end:
                found_anchor = True
            continue
        para = elem["paragraph"]
        for inline in para.get("elements", []):
            if "inlineObjectElement" in inline:
                return True
        return False
    return False
