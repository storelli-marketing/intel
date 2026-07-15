"""Milestone 4B — Slack retrieval + strategic critique of rated creative ideas.

Read-only. Pulls pre-generated ideas from the INSPIRATION_IDEAS tab (Milestone
4A), ranks/filters them for a Slack question, explains why they're worth
shooting, renders internal proof [S#] and external inspiration [E#] as SEPARATE
clickable Slack links, and can critique weak points. It never generates new
ideas, never writes to the sheet, and never presents external engagement as
proof that something works for Storelli.
"""
from __future__ import annotations

import re
from typing import Optional

import slack_response_style as st
from logger import get_logger

log = get_logger()


def _first_sentence(text: str, max_words: int = 16) -> str:
    s = re.split(r"(?<=[.!?])\s", str(text or "").strip())
    out = s[0] if s and s[0] else str(text or "").strip()
    words = out.split()
    return (" ".join(words[:max_words]) + "…") if len(words) > max_words else out

# Ideas in these STATUS values are presentable (Proposed is the generator's
# default; blank/Draft/Review are hand states). Approved/Published/Rejected/etc.
# are intentionally excluded.
_ELIGIBLE_STATUS = {"", "proposed", "draft", "review"}

# Vague marketing phrases that read as generic — flagged (never auto-edited).
GENERIC_PHRASES = [
    "game-changer", "game changer", "dominate", "dominator", "unleash",
    "inner keeper", "zero hesitation", "unbreakable", "next level", "secret",
    "insane", "ultimate", "unstoppable", "revolutionary",
]

_SHOOT_RANK = {"high": 3, "medium": 2, "low": 1, "": 0}


# ---------------------------------------------------------------------------
# query understanding
# ---------------------------------------------------------------------------
def is_idea_query(text: str) -> bool:
    """Semantic-ish detection of an idea-retrieval ask (not only exact keywords).
    Excludes pure transformation follow-ups (e.g. 'turn this into a brief')."""
    t = (text or "").lower()
    if "brief" in t and "idea" not in t:
        return False
    idea_words = ("idea", "ideas", "concept", "concepts")
    shoot_words = ("what should we shoot", "shoot first", "shoot next",
                   "worth shooting", "safest idea", "what to shoot", "which to shoot",
                   "shoot quickly", "shoot fast")
    make_words = ("what should we make", "what should we film", "what should we produce",
                  "creative to make", "what to film")
    if any(w in t for w in idea_words):
        return True
    if any(w in t for w in shoot_words + make_words):
        return True
    return False


_PRODUCTS = {
    "bodyshield": "BodyShield", "leggings": "Leggings", "pants": "Pants",
    "gloves": "Gloves", "glove": "Gloves", "exoshield": "ExoShield",
    "head guard": "Head Guard", "sliders": "Sliders",
}

# Product families — strategically related products retrieved together. Storelli's
# leg-protection line (BodyShield, GK Leggings, Pants & Leggings, sliders) shares
# the same creative territory, so a "BodyShield" ask surfaces the whole family.
_PRODUCT_FAMILIES = {
    "leggings": ("bodyshield", "gk leggings", "leggings", "pants & leggings",
                 "pants", "protective leggings", "sliders", "leg protection", "leg guard"),
    "gloves": ("gloves", "glove"),
    "head": ("exoshield", "head guard", "head guards", "gladiator jersey"),
}
_FAMILY_LABEL = {"leggings": "leggings/protection", "gloves": "gloves",
                 "head": "head/jersey protection"}


def _family_for(product_text: str) -> Optional[str]:
    p = str(product_text or "").lower()
    for fam, members in _PRODUCT_FAMILIES.items():
        if any(m in p for m in members):
            return fam
    return None
_ICPS = {
    "parent": "Parents", "parents": "Parents", "aspiring pro": "Aspiring Pro",
    "adult amateur": "Adult Amateur", "amateur": "Adult Amateur",
    "general": "General", "youth": "Parents",
}


def parse_query(text: str) -> dict:
    t = (text or "").lower()
    q = {"product": "", "icp": "", "count": 5, "count_explicit": False,
         "mode": "list", "target": None}

    for k, v in _PRODUCTS.items():
        if k in t:
            q["product"] = v
            break
    for k, v in _ICPS.items():
        if k in t:
            q["icp"] = v
            break

    m = re.search(r"\b([1-9])\b", t)
    if m:
        q["count"] = int(m.group(1))
        q["count_explicit"] = True

    if "generic" in t or "too vague" in t or "cliché" in t or "cliche" in t:
        q["mode"] = "generic"
    elif "critique" in t or "criticise" in t or "criticize" in t or "weak" in t or "tear apart" in t:
        q["mode"] = "critique"
    elif any(w in t for w in ("shoot first", "shoot next", "shoot quickly", "shoot fast",
                              "safest", "what should we shoot", "what to shoot",
                              "which to shoot", "produce first", "film first")):
        q["mode"] = "shoot_first"
    elif "evidence" in t or "proof" in t or "sources" in t or "reference" in t:
        q["mode"] = "evidence"

    tm = re.search(r"(?:idea|#)\s*#?\s*(\d+)", t)
    if tm:
        q["target"] = int(tm.group(1))
    elif "top idea" in t or "best idea" in t or "first idea" in t:
        q["target"] = 1
    return q


# ---------------------------------------------------------------------------
# eligibility, filtering, ranking
# ---------------------------------------------------------------------------
def _num(v, default=0.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _has(idea: dict, *keys) -> bool:
    return any(str(idea.get(k, "")).strip() for k in keys)


def _is_refined(idea: dict) -> bool:
    return str(idea.get("REFINEMENT_STATUS", "")).strip().lower() == "refined"


def _field(idea: dict, refined_key: str, original_key: str) -> str:
    """Prefer the creative-director refined field when the idea is Refined and
    the refined value is non-empty; otherwise fall back to the original. Never
    breaks older/unrefined ideas."""
    if _is_refined(idea):
        v = str(idea.get(refined_key, "")).strip()
        if v:
            return v
    return str(idea.get(original_key, "")).strip()


def _uses_refined(idea: dict) -> bool:
    return _is_refined(idea) and _has(idea, "REFINED_IDEA_TITLE", "REFINED_HOOK",
                                      "REFINED_CONCEPT", "REFINED_SHOT_LIST")


def _dedup_weakness(text: str) -> str:
    """Collapse the redundant generic-language phrasings in ORIGINAL_WEAKNESS
    (read-only cosmetic — the sheet is not modified)."""
    out, seen = [], set()
    for part in (p.strip() for p in str(text or "").split(";") if p.strip()):
        key = re.sub(r"\(.*?\)", "", part).strip().lower()
        key = key.replace("generic hype in title/hook", "generic").replace("generic language", "generic")
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return "; ".join(out)


_REFINED_NOTE = "_Showing refined Storelli-ready versions where available._"


def eligible(idea: dict) -> bool:
    if str(idea.get("STATUS", "")).strip().lower() not in _ELIGIBLE_STATUS:
        return False
    if not _has(idea, "INTERNAL_EVIDENCE_URLS", "INTERNAL_EVIDENCE_IDS"):
        return False   # must have internal proof
    if not _has(idea, "EXTERNAL_REFERENCE_URLS", "EXTERNAL_REFERENCE_URL"):
        return False   # must have external inspiration
    if _num(idea.get("COPYRIGHT_SAFETY_SCORE"), 100) < 60:
        return False   # risk not acceptable
    return True


def _product_matches(idea: dict, q_product: str) -> bool:
    """True if the idea's product is the SAME family as the requested product
    (so a BodyShield ask also surfaces Pants & Leggings / sliders), else a
    literal substring fallback."""
    if not q_product:
        return True
    idea_product = str(idea.get("PRODUCT", ""))
    qfam, ifam = _family_for(q_product), _family_for(idea_product)
    if qfam and ifam:
        return qfam == ifam
    return q_product.lower() in idea_product.lower()


def _is_adjacent(idea: dict, q_product: str) -> bool:
    """True when the idea is a family match but NOT a literal match for the
    queried product (e.g. a Pants & Leggings idea returned for a BodyShield ask)."""
    if not q_product:
        return False
    return (_product_matches(idea, q_product)
            and q_product.lower() not in str(idea.get("PRODUCT", "")).lower())


def _matches_filters(idea: dict, q: dict) -> bool:
    if not _product_matches(idea, q["product"]):
        return False
    if q["icp"]:
        if q["icp"].lower() not in str(idea.get("ICP", "")).lower():
            return False
    return True


def _shoot_priority_key(idea: dict):
    return (_SHOOT_RANK.get(str(idea.get("RECOMMENDED_SHOOT_PRIORITY", "")).strip().lower(), 0),
            _num(idea.get("FEASIBILITY_SCORE")), _num(idea.get("EXECUTION_CLARITY_SCORE")),
            _num(idea.get("IDEA_SCORE")))


def rank_ideas(ideas: list[dict], q: dict) -> list[dict]:
    pool = [i for i in ideas if eligible(i) and _matches_filters(i, q)]
    if q["mode"] == "shoot_first":
        pool.sort(key=_shoot_priority_key, reverse=True)
    else:
        pool.sort(key=lambda i: (_num(i.get("IDEA_SCORE")),
                                 _num(i.get("STRATEGIC_PRIORITY_SCORE"))), reverse=True)
    return pool


# ---------------------------------------------------------------------------
# critique helpers
# ---------------------------------------------------------------------------
def generic_language_flags(idea: dict) -> list[str]:
    text = " ".join(str(idea.get(k, "")) for k in ("IDEA_TITLE", "HOOK")).lower()
    return [p for p in GENERIC_PHRASES if p in text]


def critique_points(idea: dict) -> list[str]:
    pts = []
    flags = generic_language_flags(idea)
    if flags:
        pts.append(f"Generic language ({', '.join(sorted(set(flags)))}) — sharpen the hook to a "
                   "specific pain/number instead of hype.")
    if _num(idea.get("PRODUCT_FIT_SCORE"), 100) < 70:
        pts.append("Weak product fit — tie the concept more explicitly to the product's benefit.")
    if _num(idea.get("EXECUTION_CLARITY_SCORE"), 100) < 70:
        pts.append("Hard to shoot — the shot list needs clearer, simpler beats.")
    if _num(idea.get("NOVELTY_SCORE"), 100) < 55:
        pts.append("Too close to the reference — find a fresher execution angle.")
    if _num(idea.get("EVIDENCE_FIT_SCORE"), 100) < 75:
        pts.append("Thin internal evidence — anchor it to a stronger winning profile.")
    if _num(idea.get("COPYRIGHT_SAFETY_SCORE"), 100) < 80:
        pts.append("Copyright/footage risk — verify no protected/match footage is implied.")
    return pts


def suggest_sharper_hook(idea: dict) -> str:
    prod = str(idea.get("PRODUCT", "")).strip() or "the product"
    return (f"e.g. lead with a concrete stakes line for {prod} — a specific pain, number, or "
            "mistake — instead of hype words.")


# ---------------------------------------------------------------------------
# source rendering (clickable Slack links; internal vs external SEPARATED)
# ---------------------------------------------------------------------------
def _handle_from_url(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url) or re.search(r"instagram\.com/([\w.\-]+)/", url)
    return "@" + m.group(1) if m else "creator"


class SourceRegistry:
    def __init__(self):
        self._s: dict = {}
        self._e: dict = {}

    def internal(self, url: str, label: str) -> str:
        url = url.strip()
        if url not in self._s:
            self._s[url] = (f"S{len(self._s) + 1}", label)
        return self._s[url][0]

    def external(self, url: str) -> str:
        url = url.strip()
        if url not in self._e:
            self._e[url] = f"E{len(self._e) + 1}"
        return self._e[url]

    def render(self) -> str:
        if not (self._s or self._e):
            return ""
        lines = ["*Sources:*"]
        for url, (sid, label) in self._s.items():
            lines.append(f"  [{sid}] <{url}|Storelli internal evidence — {label}>")
        for url, eid in self._e.items():
            lines.append(f"  [{eid}] <{url}|External inspiration — {_handle_from_url(url)}>")
        return "\n".join(lines)


def _split(cell) -> list[str]:
    return [u.strip() for u in str(cell or "").split(";") if u.strip()]


def _cite_idea(idea: dict, reg: SourceRegistry, max_each: int = 2) -> tuple[str, str]:
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "winning profile"
    s_ids = [reg.internal(u, prof) for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))[:max_each]]
    e_ids = [reg.external(u) for u in _split(idea.get("EXTERNAL_REFERENCE_URLS"))[:max_each]]
    s_txt = " ".join(f"[{x}]" for x in s_ids) or "(profile evidence)"
    e_txt = " ".join(f"[{x}]" for x in e_ids) or "(none)"
    return s_txt, e_txt


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------
_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"


def _cap(q: dict, mode: str, default: int = 3, hard: int = 5) -> int:
    n = q["count"] if q.get("count_explicit") else default
    n = min(n, hard)
    if mode == st.MODE_CONCISE:
        n = min(n, 3)
    return max(1, n)


def _idea_line(n: int, idea: dict, reg: SourceRegistry, blunt: bool) -> str:
    s_txt, e_txt = _cite_idea(idea, reg)
    title = _field(idea, "REFINED_IDEA_TITLE", "IDEA_TITLE") or "Untitled"
    hook = _field(idea, "REFINED_HOOK", "HOOK")
    shoot = str(idea.get("RECOMMENDED_SHOOT_PRIORITY", "")).strip() or "Medium"
    pts = critique_points(idea)
    risk = _first_sentence(pts[0], 9) if pts else "shootable, no big weakness"
    tag = " _(refined)_" if _uses_refined(idea) else ""
    why = _first_sentence(hook, 16)
    return (f"*{n}. {title}*{tag} _({idea.get('PRODUCT', '?')}, score {idea.get('IDEA_SCORE', '?')})_\n"
            f"{why} _· shoot {shoot} · {risk}_ · proof {s_txt} · ref {e_txt}")


def _render_list(ranked: list[dict], q: dict, mode: str, blunt: bool) -> str:
    reg = SourceRegistry()
    count = min(_cap(q, mode), len(ranked))
    shown = ranked[:count]
    scope = " ".join(x for x in (q["product"], q["icp"]) if x)
    lead = (f"The {count} strongest{(' ' + scope) if scope else ''} idea(s) to shoot"
            + (" (blunt takes below)" if blunt else "") + ":")
    if q["product"] and any(_is_adjacent(i, q["product"]) for i in shown):
        fam = _FAMILY_LABEL.get(_family_for(q["product"]), "related")
        lead += f" _(incl. related {fam} — same {q['product']} family)_"
    blocks = "\n".join(_idea_line(n, i, reg, blunt) for n, i in enumerate(shown, 1))
    top = _field(shown[0], "REFINED_IDEA_TITLE", "IDEA_TITLE")
    move = f"Shoot *{top}* first ({shown[0].get('RECOMMENDED_SHOOT_PRIORITY', 'Medium')} priority)."
    src = reg.render()
    sources = (f"{src}\n{_NOT_PROOF}") if src else ""
    return st.render_ceo_summary(lead + "\n\n" + blocks, move=move, sources=sources, mode=mode)


def _render_shoot_first(ranked: list[dict], q: dict, mode: str, blunt: bool) -> str:
    reg = SourceRegistry()
    shown = ranked[:3]
    blocks = []
    for n, idea in enumerate(shown, 1):
        s_txt, e_txt = _cite_idea(idea, reg)
        shot = _field(idea, "REFINED_SHOT_LIST", "SHOT_LIST")
        title = _field(idea, "REFINED_IDEA_TITLE", "IDEA_TITLE") or "Untitled"
        blocks.append(f"*{n}. {title}* _(shoot {idea.get('RECOMMENDED_SHOOT_PRIORITY', 'Medium')}, "
                      f"feasibility {idea.get('FEASIBILITY_SCORE', '?')})_\n"
                      f"Fastest path: {_first_sentence(shot.split('|')[0], 12) if shot else 'n/a'} "
                      f"· {s_txt} {e_txt}")
    lead = "Shoot these first — ranked by production practicality (priority → feasibility → clarity), not raw score:"
    src = reg.render()
    sources = (f"{src}\n{_NOT_PROOF}") if src else ""
    move = f"Block a shoot day for *{_field(shown[0], 'REFINED_IDEA_TITLE', 'IDEA_TITLE')}* this week." if shown else ""
    return st.render_ceo_summary(lead + "\n\n" + "\n".join(blocks), move=move, sources=sources, mode=mode)


def _render_critique(ranked: list[dict], q: dict, mode: str, blunt: bool) -> str:
    n_show = 3 if mode == st.MODE_CONCISE else 5
    lines = []
    for idea in ranked[:n_show]:
        title = _field(idea, "REFINED_IDEA_TITLE", "IDEA_TITLE") or "Untitled"
        pts = critique_points(idea)
        verdict = ("This is shootable — no major weakness." if not pts
                   else _first_sentence(_dedup_weakness(idea.get("ORIGINAL_WEAKNESS", "")) or pts[0], 16))
        if generic_language_flags(idea):
            verdict += " (original used generic language)"
        line = f"• *{title}* — {verdict}"
        notes = str(idea.get("CREATIVE_DIRECTOR_NOTES", "")).strip()
        if notes and mode != st.MODE_CONCISE:
            line += f"\n  ✏️ Creative director: {_first_sentence(notes, 16)}"
        lines.append(line)
    lead = "Straight critique of the top ideas:"
    return st.render_ceo_summary(lead + "\n\n" + "\n".join(lines),
                                 move="Fix the hooks first; the shootable ones can go now.",
                                 sources="", mode=mode)


def _render_generic(ranked: list[dict], q: dict, mode: str, blunt: bool) -> str:
    flagged = [(i, f) for i, f in ((i, generic_language_flags(i)) for i in ranked) if f]
    if not flagged:
        return "None of the current ideas trip the generic-language check — the hooks look specific."
    lines = []
    for i, f in flagged[:6 if mode != st.MODE_CONCISE else 3]:
        line = f"• *{i.get('IDEA_TITLE', 'Untitled')}* — vague (_{', '.join(sorted(set(f)))}_)"
        if _uses_refined(i):
            line += f" → already refined to *{_field(i, 'REFINED_IDEA_TITLE', 'IDEA_TITLE')}*"
        lines.append(line)
    lead = "These read as too generic (hype over specifics):"
    return st.render_ceo_summary(lead + "\n\n" + "\n".join(lines),
                                 move="Rewrite each hook around a concrete pain, number, or mistake.",
                                 sources="", mode=mode)


def _render_evidence(ranked: list[dict], q: dict, mode: str, blunt: bool) -> str:
    if not ranked:
        return "I don't have a matching idea to show evidence for."
    idx = max(0, min((q["target"] or 1) - 1, len(ranked) - 1))
    idea = ranked[idx]
    reg = SourceRegistry()
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "winning profile"
    s_ids = [reg.internal(u, prof) for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))]
    e_ids = [reg.external(u) for u in _split(idea.get("EXTERNAL_REFERENCE_URLS"))]
    title = _field(idea, "REFINED_IDEA_TITLE", "IDEA_TITLE") or "Untitled"
    lead = f"Evidence behind *{title}*:"
    why = [
        f"Anchored to internal winning profile *{prof}* (confidence {idea.get('CONFIDENCE', '?')}) — "
        f"that's the proof it works for Storelli: {' '.join('[' + x + ']' for x in s_ids) or '(profile)'}.",
        f"External inspiration is execution reference only, not proof: "
        f"{' '.join('[' + x + ']' for x in e_ids) or '(none)'}.",
    ]
    src = reg.render()
    return st.render_ceo_summary(lead, why=why, move="", sources=(f"{src}\n{_NOT_PROOF}" if src else ""), mode=mode)


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
def _load_ideas(sheets) -> list[dict]:
    if sheets is None:
        from inspiration_sheets import InspirationSheets
        sheets = InspirationSheets()
    return sheets.read_ideas()   # read-only


def answer_ideas(text: str, sheets=None, ideas: Optional[list] = None,
                 fallback=None) -> str:
    """Main Slack entry for rated-idea retrieval. Read-only. `fallback` (callable)
    is used only when there are no rated ideas at all, to preserve the older
    live-idea path."""
    try:
        rows = ideas if ideas is not None else _load_ideas(sheets)
    except Exception as e:  # noqa: BLE001
        log.warning("idea_retrieval: could not load ideas: %s", e)
        rows = []

    if not rows:
        if fallback:
            return fallback()
        return ("I don't have any rated ideas saved yet — generate them from the dashboard "
                "(*Generate Rated Creative Ideas*) first.")

    mode = st.detect_response_mode(text)
    blunt = "blunt" in (text or "").lower()
    q = parse_query(text)
    ranked = rank_ideas(rows, q)
    if not ranked:
        scope = " ".join(x for x in (q["product"], q["icp"]) if x) or "that"
        return f"No eligible rated ideas for *{scope}* yet — try another product/ICP."

    renderer = {"critique": _render_critique, "generic": _render_generic,
                "shoot_first": _render_shoot_first, "evidence": _render_evidence}.get(
        q["mode"], _render_list)
    return renderer(ranked, q, mode, blunt)
