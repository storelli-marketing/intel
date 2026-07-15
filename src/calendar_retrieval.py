"""Read-only Slack retrieval over CONTENT_CALENDAR_IDEA_RATINGS.

Answers calendar-rating questions from the stored ratings — it never rates live
(that runs only via the dashboard/CLI) and never writes anywhere. Internal proof
[S#] and external inspiration [E#] are rendered as separate clickable links, and
external inspiration is never presented as Storelli proof.
"""
from __future__ import annotations

import re
from typing import Optional

import slack_response_style as st
from logger import get_logger

log = get_logger()

_NO_RATINGS = "I need to run the calendar rating workflow first."
_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"


def is_calendar_query(text: str) -> bool:
    t = (text or "").lower()
    if "calendar" in t and any(w in t for w in ("idea", "ideas", "rate", "rating", "ratings",
                                                "shoot", "revise", "weak", "content", "next week")):
        return True
    # "which proposed ideas are weak?" / "which proposed ideas should we revise?"
    if "proposed idea" in t and any(w in t for w in ("weak", "revise", "reject", "shoot", "rate")):
        return True
    return False


def _num(v, default=-1.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _load(sheets) -> list[dict]:
    if sheets is None:
        from inspiration_sheets import InspirationSheets
        sheets = InspirationSheets()
    return sheets.read_calendar_ratings()   # read-only


def _rated(rows: list[dict]) -> list[dict]:
    return [r for r in rows if str(r.get("SHOULD_RATE", "")).strip().upper() == "TRUE"]


def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url) or re.search(r"instagram\.com/([\w.\-]+)/", url)
    return "@" + m.group(1) if m else "creator"


def _sources_block(rows: list[dict]) -> str:
    """Combined Sources: [S#] internal proof, [E#] external inspiration,
    [N#] Notion calendar page."""
    s, e, n = {}, {}, {}
    for r in rows:
        for u in str(r.get("INTERNAL_EVIDENCE_URLS", "")).split(";"):
            u = u.strip()
            if u and u not in s:
                s[u] = f"S{len(s)+1}"
        for u in str(r.get("EXTERNAL_REFERENCE_URLS", "")).split(";"):
            u = u.strip()
            if u and u not in e:
                e[u] = f"E{len(e)+1}"
        u = str(r.get("NOTION_PAGE_URL", "")).strip()
        if u and u not in n:
            n[u] = f"N{len(n)+1}"
    items = ([(sid, u, "Storelli internal proof") for u, sid in s.items()]
             + [(eid, u, f"External inspiration — {_handle(u)}") for u, eid in e.items()]
             + [(nid, u, "Notion calendar") for u, nid in n.items()])
    return st.compact_sources(items)


def _line(r: dict) -> str:
    return (f"• *{r.get('CALENDAR_TITLE', 'Untitled')}* "
            f"_(score {r.get('CALENDAR_IDEA_SCORE', '?')})_ <{r.get('NOTION_PAGE_URL', '')}|open> — "
            f"{_first(r.get('RATIONALE', ''), 14)}")


def _first(text, n=14):
    s = re.split(r"(?<=[.!?])\s", str(text or "").strip())
    out = s[0] if s and s[0] else str(text or "").strip()
    w = out.split()
    return (" ".join(w[:n]) + "…") if len(w) > n else out


def _recurring_weakness(rows: list[dict]) -> str:
    from collections import Counter
    weak = [r for r in rows if str(r.get("RECOMMENDATION")) in ("Revise", "Reject")]
    c = Counter()
    for r in weak:
        rv = str(r.get("REVISION_SUGGESTION", "")).lower()
        if "hook" in rv:
            c["vague hooks"] += 1
        if "product" in rv:
            c["weak product tie-in"] += 1
        if "shoot" in rv or "beat" in rv:
            c["unclear shot list"] += 1
        if _num(r.get("INTERNAL_EVIDENCE_FIT_SCORE")) < 60:
            c["thin internal evidence"] += 1
    if not c:
        return ""
    top = c.most_common(1)[0]
    return f"*Biggest recurring weakness:* {top[0]} (in {top[1]} idea(s))."


def answer_calendar(text: str, sheets=None) -> str:
    rows = _rated(_load(sheets))
    if not rows:
        return _NO_RATINGS
    rows.sort(key=lambda r: _num(r.get("CALENDAR_IDEA_SCORE")), reverse=True)
    t = (text or "").lower()
    mode = st.detect_response_mode(text)

    keep = [r for r in rows if r.get("RECOMMENDATION") == "Keep"]
    revise = [r for r in rows if r.get("RECOMMENDATION") == "Revise"]
    reject = [r for r in rows if r.get("RECOMMENDATION") == "Reject"]
    weakness = _recurring_weakness(rows)

    # Focused modes.
    if "revise" in t:
        pool = revise[:3 if mode == st.MODE_CONCISE else 5]
        if not pool:
            return "Nothing's flagged to revise right now — the proposed ideas hold up."
        lead = "Calendar ideas to revise:"
        return st.render_ceo_summary(lead + "\n\n" + "\n".join(_line(r) for r in pool),
                                     move="Rework the hook + product tie-in on the top one, then re-rate.",
                                     sources=_sources_block(pool) + ("\n" + _NOT_PROOF), mode=mode)
    if "weak" in t or "reject" in t or "avoid" in t:
        pool = (reject + revise)[:3 if mode == st.MODE_CONCISE else 5]
        if not pool:
            return "Nothing looks weak — the proposed ideas are reasonable."
        lead = "Weakest proposed ideas (fix or drop):"
        return st.render_ceo_summary(lead + "\n\n" + "\n".join(_line(r) for r in pool),
                                     why=[weakness] if weakness else None,
                                     move="Don't shoot these as-is; the shared fix is above.",
                                     sources=_sources_block(pool) + ("\n" + _NOT_PROOF), mode=mode)
    if "shoot" in t or "worth" in t:
        pool = (keep or rows)[:3 if mode == st.MODE_CONCISE else 5]
        lead = "Calendar ideas worth shooting:"
        move = f"Prioritize *{pool[0].get('CALENDAR_TITLE', 'the top one')}*." if pool else ""
        return st.render_ceo_summary(lead + "\n\n" + "\n".join(_line(r) for r in pool),
                                     move=move,
                                     sources=_sources_block(pool) + ("\n" + _NOT_PROOF), mode=mode)

    # Default: top 3 to shoot / top 3 to revise / one recurring weakness / sources.
    shoot = keep[:3] or rows[:3]
    parts = [f"{len(rows)} proposed calendar ideas rated. Here's the shortlist:"]
    parts.append("*Shoot:*\n" + "\n".join(_line(r) for r in shoot))
    if revise:
        parts.append("*Revise:*\n" + "\n".join(_line(r) for r in revise[:3]))
    if reject:
        parts.append("*Skip:*\n" + "\n".join(_line(r) for r in reject[:3]))
    if weakness:
        parts.append(weakness)
    src = _sources_block(shoot)
    body = "\n\n".join(parts)
    return st.compact_slack_response(body + (f"\n\n{src}\n{_NOT_PROOF}" if src else ""), mode)
