"""Read-only Slack retrieval over CONTENT_CALENDAR_IDEA_RATINGS.

Answers calendar-rating questions from the stored ratings — it never rates live
(that runs only via the dashboard/CLI) and never writes anywhere. Internal proof
[S#] and external inspiration [E#] are rendered as separate clickable links, and
external inspiration is never presented as Storelli proof.
"""
from __future__ import annotations

import re
from typing import Optional

from logger import get_logger

log = get_logger()

_NO_RATINGS = "I need to run the calendar rating workflow first."
_DISCLAIMER = ("_External inspiration is a creative reference only — its views are not "
               "proof it will work for Storelli._")


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
    s, e = {}, {}
    for r in rows:
        for u in str(r.get("INTERNAL_EVIDENCE_URLS", "")).split(";"):
            u = u.strip()
            if u and u not in s:
                s[u] = f"S{len(s)+1}"
        for u in str(r.get("EXTERNAL_REFERENCE_URLS", "")).split(";"):
            u = u.strip()
            if u and u not in e:
                e[u] = f"E{len(e)+1}"
    if not (s or e):
        return ""
    lines = ["*Sources:*"]
    for u, sid in s.items():
        lines.append(f"  [{sid}] <{u}|Storelli internal evidence>")
    for u, eid in e.items():
        lines.append(f"  [{eid}] <{u}|External inspiration — {_handle(u)}>")
    return "\n".join(lines)


def _line(r: dict) -> str:
    return (f"• *{r.get('CALENDAR_TITLE', 'Untitled')}* "
            f"_(score {r.get('CALENDAR_IDEA_SCORE', '?')}, {r.get('PRODUCT', '?')}/{r.get('ICP', '?')})_ "
            f"<{r.get('NOTION_PAGE_URL', '')}|open>\n"
            f"   {str(r.get('RATIONALE', ''))[:180]}")


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

    keep = [r for r in rows if r.get("RECOMMENDATION") == "Keep"]
    revise = [r for r in rows if r.get("RECOMMENDATION") == "Revise"]
    reject = [r for r in rows if r.get("RECOMMENDATION") in ("Reject",)]

    # Focused modes.
    if "revise" in t:
        body = ["*Calendar ideas to revise:*"] + [_line(r) for r in revise[:6]] \
            if revise else ["No calendar ideas are currently flagged to revise."]
        return "\n".join(body) + "\n\n" + _DISCLAIMER
    if "weak" in t or "reject" in t or "avoid" in t:
        pool = (reject + revise)[:6]
        body = ["*Weakest proposed calendar ideas (reject/avoid or revise):*"] + [_line(r) for r in pool] \
            if pool else ["Nothing looks weak — the proposed ideas are reasonable."]
        w = _recurring_weakness(rows)
        return "\n".join(body) + (f"\n\n{w}" if w else "") + "\n\n" + _DISCLAIMER
    if "shoot" in t or "worth" in t:
        pool = keep[:5] or rows[:5]
        body = ["*Calendar ideas worth shooting (top-rated, Keep):*"] + [_line(r) for r in pool]
        return "\n".join(body) + "\n\n" + _sources_block(pool) + "\n\n" + _DISCLAIMER

    # Default: a balanced report.
    body = [f"*Content calendar ratings* — {len(rows)} proposed idea(s) rated."]
    body.append("\n*Top to shoot:*")
    body += [_line(r) for r in (keep[:3] or rows[:3])]
    if revise:
        body.append("\n*To revise:*")
        body += [_line(r) for r in revise[:3]]
    if reject:
        body.append("\n*To reject / avoid:*")
        body += [_line(r) for r in reject[:3]]
    w = _recurring_weakness(rows)
    if w:
        body.append("\n" + w)
    src = _sources_block((keep[:3] or rows[:3]))
    return "\n".join(body) + "\n\n" + _DISCLAIMER + (f"\n\n{src}" if src else "")
