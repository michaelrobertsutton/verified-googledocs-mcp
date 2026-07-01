"""Offline simulator for Docs API index bounds (no network, pure).

Replays a fully-assembled batchUpdate request list — the exact list that is
about to be sent, not just ``compile_markdown``'s output — against a running
model of the tab's index space, to catch invalid indices *before* they reach
the API. Both ``dry_run`` and the real write call :func:`simulate_requests`
on the same assembled list, so the two paths cannot disagree about whether a
write is index-valid.

Honest scope
------------
This simulator shares the compiler's table-geometry model (imported directly
from ``markdown_writer`` so the two can never silently drift apart) — it
cannot catch a *wrong* shared model, only a *wrong request* against a model
both sides already agree on. What it DOES catch:

  - Any location/range index that would fall outside the segment's current
    length as the batch is replayed, accounting for every request's own
    effect on that length, in order.
  - Any ``insertText`` whose index lands inside a table's structural span but
    is not one of that table's known-valid cell-paragraph indices — this is
    the exact failure signature of the original bug ("insertion index must
    be inside the bounds of an existing paragraph").

Pre-existing tab content (anything not created by requests in *this* batch)
is treated as one large valid region, since it is real, already-verified
Docs content — this module only re-derives structure for content its own
batch creates. It also cannot see revision races, auth, or quota errors;
those remain real-API-only failure modes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .markdown_writer import _table_cell_index, _table_stride, _table_structural_size, _utf16_len

_RANGE_REQUEST_KEYS = ("updateParagraphStyle", "updateTextStyle", "createParagraphBullets")


class IndexSimulationError(Exception):
    """A request in the batch would land outside document bounds, or inside
    a table's structure but not on a valid paragraph."""

    def __init__(self, message: str, *, request_index: int, request: dict[str, Any]) -> None:
        super().__init__(message)
        self.request_index = request_index
        self.request = request


@dataclass
class _TableSpan:
    start: int  # inclusive, in *current* coordinate space
    end: int  # exclusive
    valid_cell_indices: frozenset[int]


def _shift_tables(tables: list[_TableSpan], idx: int, delta: int) -> None:
    """Apply the index shift caused by inserting/deleting *delta* at *idx*.

    A table wholly at or after *idx* moves entirely. A table whose span
    straddles *idx* (e.g. a cell-text insertText landing inside it) keeps its
    own start but grows/shrinks its end and any cell indices past *idx*. A
    table entirely before *idx* is unaffected.
    """
    for i, t in enumerate(tables):
        if idx <= t.start:
            tables[i] = _TableSpan(
                start=t.start + delta,
                end=t.end + delta,
                valid_cell_indices=frozenset(c + delta for c in t.valid_cell_indices),
            )
        elif idx < t.end:
            tables[i] = replace(
                t,
                end=t.end + delta,
                valid_cell_indices=frozenset(
                    (c + delta if c > idx else c) for c in t.valid_cell_indices
                ),
            )
        # else idx >= t.end: table is entirely before idx, unaffected.


def simulate_requests(
    requests: list[dict[str, Any]],
    *,
    tab_start: int,
    tab_end: int,
) -> None:
    """Replay *requests* against a running model of the tab's index space.

    Raises :class:`IndexSimulationError` on the first request whose
    index/range would be invalid. *tab_start*/*tab_end* describe the
    pre-existing tab's segment extent (from the pre-read document), before
    any request in *requests* is applied.
    """
    length = tab_end
    tables: list[_TableSpan] = []

    def _check_point(idx: int, req_idx: int, req: dict[str, Any], *, is_insert_text: bool) -> None:
        if idx < tab_start or idx > length:
            raise IndexSimulationError(
                f"request[{req_idx}] index {idx} is outside the current segment "
                f"bounds [{tab_start}, {length}]",
                request_index=req_idx,
                request=req,
            )
        if is_insert_text:
            for t in tables:
                if t.start <= idx < t.end and idx not in t.valid_cell_indices:
                    raise IndexSimulationError(
                        f"request[{req_idx}] insertText at index {idx} lands inside "
                        "table structure but is not a valid cell paragraph index — "
                        "the insertion index must be inside the bounds of an "
                        "existing paragraph",
                        request_index=req_idx,
                        request=req,
                    )

    def _check_range(start: int, end: int, req_idx: int, req: dict[str, Any]) -> None:
        if start < tab_start or end > length or start > end:
            raise IndexSimulationError(
                f"request[{req_idx}] range [{start}, {end}) is outside the current "
                f"segment bounds [{tab_start}, {length}]",
                request_index=req_idx,
                request=req,
            )

    for req_idx, req in enumerate(requests):
        if "insertText" in req:
            body = req["insertText"]
            idx = body["location"]["index"]
            _check_point(idx, req_idx, req, is_insert_text=True)
            text_len = _utf16_len(body["text"])
            _shift_tables(tables, idx, text_len)
            length += text_len

        elif "insertTable" in req:
            body = req["insertTable"]
            idx = body["location"]["index"]
            _check_point(idx, req_idx, req, is_insert_text=False)
            n_rows, n_cols = body["rows"], body["columns"]
            size = _table_structural_size(n_rows, n_cols)
            _shift_tables(tables, idx, size)
            table_start = idx + 1
            valid = frozenset(
                _table_cell_index(table_start, n_cols, r, c)
                for r in range(n_rows)
                for c in range(n_cols)
            )
            tables.append(
                _TableSpan(
                    start=table_start,
                    end=table_start + n_rows * _table_stride(n_cols) + 2,
                    valid_cell_indices=valid,
                )
            )
            length += size

        elif "deleteContentRange" in req:
            rng = req["deleteContentRange"]["range"]
            start, end = rng["startIndex"], rng["endIndex"]
            _check_range(start, end, req_idx, req)
            delta = -(end - start)
            tables[:] = [t for t in tables if not (start <= t.start and t.end <= end)]
            _shift_tables(tables, end, delta)
            length += delta

        elif any(key in req for key in _RANGE_REQUEST_KEYS):
            key = next(k for k in _RANGE_REQUEST_KEYS if k in req)
            rng = req[key]["range"]
            _check_range(rng["startIndex"], rng["endIndex"], req_idx, req)

        # Any other request type is outside what this compiler emits and is
        # left unmodeled — this simulator only covers the request kinds
        # produced by compile_markdown() and the handlers in
        # markdown_mutations.py.


def compiled_requests_growth(requests: list[dict[str, Any]]) -> int:
    """Total net UTF-16 index growth a list of compiled requests would cause.

    Sums insertText UTF-16 lengths and insertTable structural sizes; ignores
    style/bullet requests (they don't change segment length). Used to bound
    an evidence re-export window to exactly the content a write inserted,
    instead of Python ``len()`` (wrong for astral emoji) or a text-only sum
    (wrong when a table is present, since its structural markers don't
    appear in any ``insertText``).
    """
    growth = 0
    for req in requests:
        if "insertText" in req:
            growth += _utf16_len(req["insertText"]["text"])
        elif "insertTable" in req:
            body = req["insertTable"]
            growth += _table_structural_size(body["rows"], body["columns"])
    return growth
