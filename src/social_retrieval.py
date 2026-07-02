"""Lightweight retrieval helpers for the Slack conversational brain.

Read-only: parses a free-text question for filters (product / ICP / taxonomy
layer / performance bucket), filters already-tagged Sheet rows by them, slices
correlation results by layer (optionally recomputed within a Product/ICP
subgroup for accuracy), and does a best-effort optional Notion Brain lookup.
Never writes to the Sheet, never triggers video analysis, never invents data
that wasn't actually retrieved.
"""
from __future__ import annotations

import re

import correlations as corr
import interpretation
import performance
from logger import get_logger

log = get_logger()

# Words that imply a specific taxonomy layer is being asked about. Longest
# keys first at lookup time so "visual style" matches before "visual".
_LAYER_WORDS = {
    "hooks": "hook", "hook": "hook",
    "formats": "format", "format": "format",
    "visual style": "visual_style", "visual": "visual_style",
    "problems": "problem_type", "problem": "problem_type",
    "solutions": "solution_type", "solution": "solution_type",
    "conversion": "conversion",
    "offers": "offer", "offer": "offer",
    "product presence": "product_presence",
    "funnel stage": "funnel_stage", "funnel": "funnel_stage",
}

_PERF_WORDS = {"great": "Great", "good": "Good", "ok": "Ok", "underdog": "Underdog"}


def detect_layer(text: str) -> str | None:
    t = (text or "").lower()
    for word in sorted(_LAYER_WORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", t):
            return _LAYER_WORDS[word]
    return None


def detect_performance_bucket(text: str) -> str | None:
    t = (text or "").lower()
    for word, label in _PERF_WORDS.items():
        if re.search(rf"\b{word}\b", t):
            return label
    return None


def extract_filters(text: str) -> dict:
    """Best-effort filters parsed from free text. Product/ICP only ever come
    from the same alias maps the idea interpreter already uses — never guessed."""
    return {
        "product": interpretation.detect_product(text),
        "icp": interpretation.detect_icp(text),
        "layer": detect_layer(text),
        "performance": detect_performance_bucket(text),
    }


def filter_rows(rows: list[dict], filters: dict) -> list[dict]:
    """Filter rows by the detected Product / ICP / performance-bucket dimensions."""
    out = list(rows)
    if filters.get("product"):
        out = [r for r in out if str(r.get("Product", "")).strip().lower()
               == filters["product"].lower()]
    if filters.get("icp"):
        out = [r for r in out if str(r.get("ICP", "")).strip().lower()
               == filters["icp"].lower()]
    if filters.get("performance"):
        out = [r for r in out if str(r.get("PERFORMANCE", "")).strip().lower()
               == filters["performance"].lower()]
    return out


def signals_for_layer(results: list[dict], layer: str, winning: bool = True) -> list[dict]:
    pool = corr.winning(results) if winning else corr.weak(results)
    return [r for r in pool if r["layer"] == layer]


def segment_results(analyzed: list[dict], buckets: dict, results: list[dict],
                    filters: dict, min_rows: int = 3) -> tuple[list[dict], str]:
    """Recompute correlations within a Product/ICP subgroup when one is
    detected and there's enough data; otherwise fall back to the sheet-wide
    results. Returns (results, note) where note explains what was done."""
    if not (filters.get("icp") or filters.get("product")):
        return results, ""
    seg = filters.get("icp") or filters.get("product")
    subset = filter_rows(analyzed, {"icp": filters.get("icp"), "product": filters.get("product")})
    if len(subset) < min_rows:
        return results, f" (not enough tagged {seg} rows yet — showing sheet-wide instead)"
    subset_buckets = {r["_row"]: buckets[r["_row"]] for r in subset if r["_row"] in buckets}
    return corr.compute(subset, subset_buckets), f" for **{seg}**"


def example_rows(rows: list[dict], limit: int = 3) -> list[dict]:
    """Internal (non-reference) rows only — inspiration rows are never cited
    as evidence."""
    internal = [r for r in rows if not performance.is_reference_row(r)]
    return internal[:limit]


def notion_learnings(limit: int = 2) -> list[dict]:
    """Best-effort read of a couple of entries from the Notion 'Marketing
    Learnings' database, for an optional [S4] citation. Returns [] on any
    failure or when Notion isn't configured — never raises, never writes."""
    import config
    if not (config.NOTION_API_KEY and config.NOTION_PARENT_PAGE_ID):
        return []
    try:
        from notion_brain import NotionBrain
        brain = NotionBrain()
        dbs = brain._find_child_databases()
        db_id = dbs.get("Marketing Learnings")
        if not db_id:
            return []
        res = brain._call(brain.client.databases.query, database_id=db_id, page_size=limit)
        out = []
        for page in (res.get("results") or [])[:limit]:
            title_parts = page.get("properties", {}).get("Title", {}).get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts)
            if title:
                out.append({"title": title, "url": page.get("url", "")})
        return out
    except Exception as e:  # noqa: BLE001 - Notion is a bonus source, never fatal
        log.warning("social_retrieval: notion_learnings unavailable: %s", e)
        return []
