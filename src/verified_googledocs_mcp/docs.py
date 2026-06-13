"""Docs API: tab-scoped document reads and structure extraction.

Every public function in this module is a pure transform over a Docs API
response dict — no I/O, no credentials. Network calls are the server layer's
responsibility (server.py fetches the document and passes the dict here).

Tabless legacy docs (docs without any tab metadata) are normalised to a single
synthetic implicit tab with id "_body". Tools still require and honour a tab
target; this normalisation means behaviour is well-defined rather than failing
obscurely on older documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .markdown import to_markdown

# The synthetic tab id used when a document has no tab metadata.
IMPLICIT_TAB_ID = "_body"


# ---------------------------------------------------------------------------
# Tab metadata
# ---------------------------------------------------------------------------


@dataclass
class TabInfo:
    tab_id: str
    title: str
    index: int
    child_tabs: list["TabInfo"] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"tab_id": self.tab_id, "title": self.title, "index": self.index}
        if self.child_tabs:
            d["child_tabs"] = [c.as_dict() for c in self.child_tabs]
        return d


def list_tabs_from(doc: dict[str, Any]) -> list[TabInfo]:
    """Return a flat-by-nesting list of TabInfo from a Docs API document dict.

    Use this when you need to discover available tab IDs before calling
    read_document or find_sections.

    For tabless legacy docs, returns a single TabInfo with id "_body".
    """
    tabs_raw = doc.get("tabs", [])
    if not tabs_raw:
        return [TabInfo(tab_id=IMPLICIT_TAB_ID, title="Body", index=0)]
    return [_parse_tab(t) for t in tabs_raw]


def _parse_tab(raw: dict[str, Any]) -> TabInfo:
    props = raw.get("tabProperties", {})
    child_tabs = [_parse_tab(c) for c in raw.get("childTabs", [])]
    return TabInfo(
        tab_id=props.get("tabId", ""),
        title=props.get("title", ""),
        index=props.get("index", 0),
        child_tabs=child_tabs,
    )


# ---------------------------------------------------------------------------
# Tab body extraction
# ---------------------------------------------------------------------------


def _find_tab_body(doc: dict[str, Any], tab_id: str) -> dict[str, Any] | None:
    """Locate and return the body dict for the given tab_id.

    Returns None if the tab_id is not found. Searches recursively through
    nested tabs.

    For tabless docs, tab_id must be IMPLICIT_TAB_ID ("_body"); the document's
    top-level body is returned.
    """
    tabs_raw = doc.get("tabs", [])
    if not tabs_raw:
        # Tabless document — treat top-level body as the implicit tab.
        if tab_id == IMPLICIT_TAB_ID:
            return doc.get("body")
        return None
    return _search_tabs(tabs_raw, tab_id)


def _search_tabs(tabs: list[dict[str, Any]], tab_id: str) -> dict[str, Any] | None:
    for tab in tabs:
        props = tab.get("tabProperties", {})
        if props.get("tabId") == tab_id:
            doc_tab = tab.get("documentTab", {})
            return doc_tab.get("body")
        # Recurse into children.
        child_result = _search_tabs(tab.get("childTabs", []), tab_id)
        if child_result is not None:
            return child_result
    return None


def _available_tab_ids(doc: dict[str, Any]) -> list[str]:
    tabs = list_tabs_from(doc)
    result: list[str] = []

    def collect(t: TabInfo) -> None:
        result.append(t.tab_id)
        for c in t.child_tabs:
            collect(c)

    for t in tabs:
        collect(t)
    return result


# ---------------------------------------------------------------------------
# read_document logic
# ---------------------------------------------------------------------------


@dataclass
class ReadResult:
    doc_id: str
    tab_id: str
    format: str
    content: str | dict[str, Any]  # str for markdown, dict for structured
    revision_id: str
    lossy_elements: list[dict[str, Any]] = field(default_factory=list)


def read_tab(
    doc: dict[str, Any],
    doc_id: str,
    tab_id: str,
    format: Literal["markdown", "structured"] = "markdown",
) -> ReadResult:
    """Return the content of a specific tab.

    Use read_document (the MCP tool) when you want to read a Google Doc tab
    as markdown or as a structured representation of its paragraphs and style
    runs. This function is the pure-transform core of that tool.

    Raises ValueError with the list of available tabs if tab_id is not found.
    """
    body = _find_tab_body(doc, tab_id)
    if body is None:
        available = _available_tab_ids(doc)
        raise ValueError(f"Tab '{tab_id}' not found. Available tabs: {available}")

    revision_id = doc.get("revisionId", "")

    if format == "markdown":
        md, lossy = to_markdown(body)
        return ReadResult(
            doc_id=doc_id,
            tab_id=tab_id,
            format="markdown",
            content=md,
            revision_id=revision_id,
            lossy_elements=[{"kind": e.kind, "placeholder": e.placeholder} for e in lossy],
        )

    # Structured format: return the raw paragraph data with positions and style.
    structured = _extract_structured(body)
    return ReadResult(
        doc_id=doc_id,
        tab_id=tab_id,
        format="structured",
        content=structured,
        revision_id=revision_id,
    )


def _extract_structured(body: dict[str, Any]) -> dict[str, Any]:
    """Extract paragraphs and their style information in a structured format."""
    paragraphs: list[dict[str, Any]] = []
    for elem in body.get("content", []):
        if "paragraph" in elem:
            para = elem["paragraph"]
            style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
            start = elem.get("startIndex", 0)
            end = elem.get("endIndex", 0)
            runs: list[dict[str, Any]] = []
            for inline in para.get("elements", []):
                if "textRun" in inline:
                    tr = inline["textRun"]
                    ts = tr.get("textStyle", {})
                    text = tr.get("content", "")
                    runs.append(
                        {
                            "text": text,
                            "bold": ts.get("bold", False),
                            "italic": ts.get("italic", False),
                            "link": ts.get("link", {}).get("url", ""),
                            "start": inline.get("startIndex", 0),
                            "end": inline.get("endIndex", 0),
                        }
                    )
            paragraphs.append(
                {
                    "style": style,
                    "start": start,
                    "end": end,
                    "runs": runs,
                }
            )
    return {"paragraphs": paragraphs}


# ---------------------------------------------------------------------------
# find_sections logic
# ---------------------------------------------------------------------------


@dataclass
class SectionMatch:
    heading: str
    matched_text: str
    start_index: int
    end_index: int
    computed_at_revision: str  # revision ID at the time of this read


def find_sections_in(
    doc: dict[str, Any],
    heading: str,
    tab_id: str,
) -> list[SectionMatch]:
    """Return heading matches with their document ranges, stamped with revision ID.

    Use find_sections (the MCP tool) when you need section ranges for targeted
    edits. The computed_at_revision stamp is consumed by range-editing tools in
    later milestones to refuse stale ranges. M1 stamps; M2+ enforces.

    Matching is case-insensitive and substring-based so that partial heading
    queries return useful results.

    Raises ValueError if the tab is not found.
    """
    body = _find_tab_body(doc, tab_id)
    if body is None:
        available = _available_tab_ids(doc)
        raise ValueError(f"Tab '{tab_id}' not found. Available tabs: {available}")

    revision_id = doc.get("revisionId", "")
    needle = heading.lower()
    matches: list[SectionMatch] = []

    for elem in body.get("content", []):
        if "paragraph" not in elem:
            continue
        para = elem["paragraph"]
        style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
        if not style.startswith("HEADING_"):
            continue

        # Collect the full text of the heading paragraph.
        text_parts: list[str] = []
        for inline in para.get("elements", []):
            if "textRun" in inline:
                text_parts.append(inline["textRun"].get("content", ""))
        full_text = "".join(text_parts).rstrip("\n")

        if needle in full_text.lower():
            matches.append(
                SectionMatch(
                    heading=heading,
                    matched_text=full_text,
                    start_index=elem.get("startIndex", 0),
                    end_index=elem.get("endIndex", 0),
                    computed_at_revision=revision_id,
                )
            )

    return matches


# ---------------------------------------------------------------------------
# Google API helpers (I/O edge — not pure transforms)
# ---------------------------------------------------------------------------


def build_docs_service(credentials: Any) -> Any:  # type: ignore[return]
    """Build and return a Google Docs API service resource.

    credentials: a google.oauth2.credentials.Credentials instance.
    """
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=credentials)


def fetch_document(service: Any, doc_id: str) -> dict[str, Any]:
    """Fetch a document from the Docs API and return the response dict.

    Always requests includeTabsContent=true so multi-tab documents are fully
    populated. Uses google-api-python-client's built-in retry (num_retries=3)
    rather than a custom loop.

    Pins suggestionsViewMode=PREVIEW_WITHOUT_SUGGESTIONS so the text the locator
    and all index math run over is the base document, never suggestion-inline
    text. Left unset it resolves to DEFAULT_FOR_CURRENT_ACCESS — SUGGESTIONS_INLINE
    for an editor — which would let a pending suggestion alter one of two duplicate
    sentences so the locator sees a single match, silently defeating replace_text's
    match-count guard (issue #28). list_open_items deliberately uses a separate
    SUGGESTIONS_INLINE get because it must see suggestions; this read must not.
    """
    return (
        service.documents()
        .get(
            documentId=doc_id,
            includeTabsContent=True,
            suggestionsViewMode="PREVIEW_WITHOUT_SUGGESTIONS",
        )
        .execute(num_retries=3)
    )
