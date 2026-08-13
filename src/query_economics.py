"""Query economics / self-tuning for external inspiration discovery (Part E).

Light per-query performance scoring used to keep the weekly discovery batch
BOUNDED and on-domain — prefer goalkeeper/soccer/youth/coach queries, keep the
known-bad families paused, and recommend RUN / PAUSE / REVIEW. Read-only:
recommends, never permanently deletes a query, never auto-toggles ACTIVE, and
NEVER lets external engagement influence internal proof.
"""
from __future__ import annotations

from typing import Optional

# Prefer: goalkeeper education/mistakes/training/landing/diving/protection,
# soccer injury prevention, youth soccer safety, coach-led soccer education.
_GOOD_TERMS = ("goalkeeper", "goalkeeping", " gk ", "gk ", "keeper", "youth soccer",
               "soccer injury", "injury prevention", "coach", "diving save", "landing",
               "turf", "soccer safety", "youth football", "grassroots soccer")
# Known-bad / off-domain (Part D): broad cross-sport, generic off-domain gear,
# weak mental/confidence, adjacent-sport ring.
_OFFDOMAIN_TERMS = ("taekwondo", "mma", "motocross", "motorcross", "volleyball", "running",
                    "marathon", "hockey", "lacrosse", "rugby", "skate", "skateboard", "moto",
                    "basketball", "tennis", "crossfit", "gym bro")
_WEAK_TERMS = ("mindset", "confidence hack", "mental toughness", "manifest", "affirmation",
               "3 things athletes", "i wish i knew", "watch before you buy")
# Research rings we deprioritize unless a query is clearly on-domain (goalkeeper).
_DEPRIORITIZED_RINGS = {"3", "5", "6", "7"}


def _lower(v) -> str:
    return str(v or "").strip().lower()


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).strip() or default)
    except (TypeError, ValueError):
        return default


def _text_of(q: dict) -> str:
    return " ".join(_lower(q.get(k, "")) for k in ("QUERY", "SUBCATEGORY", "SHOULD_FIND",
                                                   "REASON_FOR_QUERY", "TARGET_PRODUCT"))


def classify_family(q: dict) -> tuple:
    """('good'|'bad'|'neutral', reason). Goalkeeper/soccer/youth/coach on-domain
    is good; off-domain cross-sport, weak mindset, and deprioritized rings that
    aren't clearly goalkeeper are bad."""
    t = " " + _text_of(q) + " "
    on_domain = any(g in t for g in _GOOD_TERMS)
    if any(o in t for o in _OFFDOMAIN_TERMS) and not on_domain:
        return "bad", "off-domain cross-sport"
    if any(w in t for w in _WEAK_TERMS) and not on_domain:
        return "bad", "weak mental/confidence or generic creator query"
    ring = _lower(q.get("RESEARCH_RING")).replace("r", "").strip()
    if ring in _DEPRIORITIZED_RINGS and not on_domain:
        return "bad", f"deprioritized ring {ring} without goalkeeper focus"
    if on_domain:
        return "good", "goalkeeper/soccer/youth/coach on-domain"
    return "neutral", "no strong signal"


def stats_from_query_row(q: dict, history: Optional[dict] = None) -> dict:
    """Assemble the per-query stats the utility score needs. `history` (optional)
    overrides/augments what the query row already stores."""
    h = history or {}
    added = _num(h.get("new_rows_added", q.get("RESULTS_ADDED")))
    skipped = _num(h.get("duplicates", q.get("RESULTS_SKIPPED")))
    found = _num(h.get("results_found")) or (added + skipped)
    return {
        "results_found": found,
        "new_rows_added": added,
        "duplicates": skipped,
        "rejected": _num(h.get("rejected")),
        "quality_70_plus": _num(h.get("quality_70_plus")),
        "quality_80_plus": _num(h.get("quality_80_plus")),
        "quality_90_plus": _num(h.get("quality_90_plus")),
        "average_quality": _num(h.get("average_quality")),
        "connection_usage_count": _num(h.get("connection_usage_count")),
        "runs_without_useful_result": _num(h.get("runs_without_useful_result")),
        "last_run_status": _lower(q.get("LAST_RUN_STATUS")),
    }


def query_utility(stats: dict) -> float:
    """0–100. 40% quality yield + 30% new-row yield + 20% semantic-connection
    usage + 10% novelty. Zero history -> 0 (unproven), so unproven queries fall to
    REVIEW rather than being trusted as RUN."""
    found = stats.get("results_found", 0) or 0
    if found <= 0:
        # no run history yet — utility unknown (treated as low, -> REVIEW)
        return 0.0
    quality_yield = min(1.0, (stats.get("quality_80_plus", 0)
                              + 0.5 * stats.get("quality_70_plus", 0)) / found)
    new_row_yield = min(1.0, stats.get("new_rows_added", 0) / found)
    connection = min(1.0, stats.get("connection_usage_count", 0) / 3.0)
    dupes = stats.get("duplicates", 0)
    novelty = max(0.0, 1.0 - (dupes / found)) if found else 0.0
    score = 100 * (0.40 * quality_yield + 0.30 * new_row_yield
                   + 0.20 * connection + 0.10 * novelty)
    return round(score, 1)


def recommend(q: dict, stats: Optional[dict] = None) -> tuple:
    """('RUN'|'PAUSE'|'REVIEW', reason). Bad family -> PAUSE. Repeated dry runs
    -> PAUSE. High utility -> RUN. Unproven/mid -> REVIEW. Never deletes."""
    stats = stats or stats_from_query_row(q)
    family, reason = classify_family(q)
    if family == "bad":
        return "PAUSE", reason
    if stats.get("runs_without_useful_result", 0) >= 3:
        return "PAUSE", "3+ runs without a useful result"
    util = query_utility(stats)
    if util >= 60:
        return "RUN", f"utility {util}"
    if util > 0 and util < 30:
        return "PAUSE", f"low utility {util}"
    if family == "good" and util == 0.0:
        return "RUN", "on-domain, unproven — worth a first bounded run"
    return "REVIEW", f"utility {util}"


def select_active(queries: list, max_n: int, history_by_id: Optional[dict] = None) -> dict:
    """Pick the bounded set of on-domain queries to run this batch (never toggles
    the sheet). Returns {selected, paused, review, recommendations}."""
    history_by_id = history_by_id or {}
    recs, selected, paused, review = [], [], [], []
    scored = []
    for q in queries:
        stats = stats_from_query_row(q, history_by_id.get(q.get("QUERY_ID")))
        rec, reason = recommend(q, stats)
        util = query_utility(stats)
        recs.append({"query_id": q.get("QUERY_ID"), "query": q.get("QUERY", "")[:60],
                     "recommendation": rec, "reason": reason, "utility": util})
        if rec == "PAUSE":
            paused.append(q)
        elif rec == "REVIEW":
            review.append((util, q))
        else:
            scored.append((util, q))
    # RUN queries first (by utility desc), then fill remaining slots with REVIEW.
    scored.sort(key=lambda t: t[0], reverse=True)
    review.sort(key=lambda t: t[0], reverse=True)
    for _u, q in scored + review:
        if len(selected) >= max_n:
            break
        selected.append(q)
    return {"selected": selected, "paused": paused,
            "review": [q for _u, q in review], "recommendations": recs}
