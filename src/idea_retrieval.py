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

from logger import get_logger

log = get_logger()

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
    q = {"product": "", "icp": "", "count": 5, "mode": "list", "target": None}

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
def _idea_block(n: int, idea: dict, reg: SourceRegistry) -> str:
    s_txt, e_txt = _cite_idea(idea, reg)
    weaknesses = critique_points(idea)
    weak = weaknesses[0] if weaknesses else "no major weakness flagged"
    shoot = str(idea.get("RECOMMENDED_SHOOT_PRIORITY", "")).strip() or "Medium"
    shot = str(idea.get("SHOT_LIST", "")).strip()
    shot_note = (shot.split("|")[0].strip() + ("…" if "|" in shot else "")) if shot else "n/a"
    return (
        f"\n*{n}. {idea.get('IDEA_TITLE', 'Untitled')}*  "
        f"_(score {idea.get('IDEA_SCORE', '?')}, priority {idea.get('STRATEGIC_PRIORITY_SCORE', '?')})_\n"
        f"• {idea.get('PRODUCT', '?')} / {idea.get('ICP', '?')} · *{idea.get('FORMAT', '')}* · shoot: *{shoot}*\n"
        f"• Hook: {idea.get('HOOK', '')}\n"
        f"• Why it's worth shooting: {str(idea.get('CONCEPT', ''))[:220]}\n"
        f"• Shoot note: {shot_note}  ·  CTA: {idea.get('CTA', '')}\n"
        f"• Internal proof: {s_txt}   ·   External inspiration (reference only): {e_txt}\n"
        f"• Watch-out: {weak}"
    )


_DISCLAIMER = ("_External inspiration is a creative reference only — its views/followers "
               "are not proof it will work for Storelli._")


def _render_list(ranked: list[dict], q: dict) -> str:
    count = max(3, min(q["count"], 5, len(ranked)))
    reg = SourceRegistry()
    head_bits = []
    if q["product"]:
        head_bits.append(q["product"])
    if q["icp"]:
        head_bits.append(q["icp"])
    scope = (" for " + " / ".join(head_bits)) if head_bits else ""
    shown = ranked[:count]
    body = [f"*Top {count} rated idea(s){scope}* — grounded in internal winning profiles, "
            "adapting external creative mechanisms."]
    # If any shown idea is a related-family match (not a literal product match),
    # say so naturally so the user knows why adjacent products appear.
    if q["product"] and any(_is_adjacent(i, q["product"]) for i in shown):
        fam = _FAMILY_LABEL.get(_family_for(q["product"]), "related")
        body.append(f"_I'm including related {fam} ideas because they map to the "
                    f"{q['product']} family._")
    for n, idea in enumerate(shown, 1):
        body.append(_idea_block(n, idea, reg))
    body.append("\n" + _DISCLAIMER)
    src = reg.render()
    return "\n".join(body) + (f"\n\n{src}" if src else "")


def _render_shoot_first(ranked: list[dict], q: dict) -> str:
    reg = SourceRegistry()
    body = ["*What to shoot first* — ranked by production practicality "
            "(shoot priority → feasibility → execution clarity), not just idea score."]
    for n, idea in enumerate(ranked[:3], 1):
        s_txt, e_txt = _cite_idea(idea, reg)
        shot = str(idea.get("SHOT_LIST", "")).strip()
        body.append(
            f"\n*{n}. {idea.get('IDEA_TITLE', 'Untitled')}*  "
            f"_(shoot {idea.get('RECOMMENDED_SHOOT_PRIORITY', 'Medium')}, "
            f"feasibility {idea.get('FEASIBILITY_SCORE', '?')}, exec {idea.get('EXECUTION_CLARITY_SCORE', '?')})_\n"
            f"• {idea.get('PRODUCT', '?')} / {idea.get('ICP', '?')} · {idea.get('FORMAT', '')}\n"
            f"• Fastest path: {shot.split('|')[0].strip() if shot else 'n/a'}\n"
            f"• Internal proof: {s_txt} · External ref: {e_txt}")
    body.append("\n" + _DISCLAIMER)
    src = reg.render()
    return "\n".join(body) + (f"\n\n{src}" if src else "")


def _render_critique(ranked: list[dict], q: dict) -> str:
    body = ["*Blunt critique of the top ideas:*"]
    for n, idea in enumerate(ranked[:5], 1):
        pts = critique_points(idea)
        verdict = "solid — worth shooting" if not pts else "; ".join(pts)
        body.append(f"\n*{n}. {idea.get('IDEA_TITLE', 'Untitled')}* "
                    f"_(score {idea.get('IDEA_SCORE', '?')})_\n• {verdict}")
    body.append("\n_(Critique only — nothing on the sheet was changed.)_")
    return "\n".join(body)


def _render_generic(ranked: list[dict], q: dict) -> str:
    flagged = [(i, generic_language_flags(i)) for i in ranked]
    flagged = [(i, f) for i, f in flagged if f]
    if not flagged:
        return "None of the current ideas trip the generic-language check — hooks look specific."
    body = ["*Ideas that read as too generic* (hype words over specifics):"]
    for i, f in flagged[:6]:
        body.append(f"\n• *{i.get('IDEA_TITLE', 'Untitled')}* — vague: _{', '.join(sorted(set(f)))}_.\n"
                    f"  Sharper: {suggest_sharper_hook(i)}")
    body.append("\n_Suggestions only — I haven't changed the sheet._")
    return "\n".join(body)


def _render_evidence(ranked: list[dict], q: dict) -> str:
    if not ranked:
        return "I don't have a matching idea to show evidence for."
    idx = (q["target"] or 1) - 1
    idx = max(0, min(idx, len(ranked) - 1))
    idea = ranked[idx]
    reg = SourceRegistry()
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "winning profile"
    s_ids = [reg.internal(u, prof) for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))]
    e_ids = [reg.external(u) for u in _split(idea.get("EXTERNAL_REFERENCE_URLS"))]
    body = [
        f"*Evidence behind '{idea.get('IDEA_TITLE', 'Untitled')}'*",
        f"• Anchored to internal winning profile: *{prof}* "
        f"(confidence {idea.get('CONFIDENCE', '?')}).",
        f"• Storelli internal proof: {' '.join('[' + x + ']' for x in s_ids) or '(profile)'}  "
        "— this is the evidence the format works for Storelli.",
        f"• External inspiration (execution reference ONLY, not proof): "
        f"{' '.join('[' + x + ']' for x in e_ids) or '(none)'}.",
        f"• Rationale: {str(idea.get('IDEA_RATIONALE', ''))[:400]}",
        "\n" + _DISCLAIMER,
    ]
    src = reg.render()
    return "\n".join(body) + (f"\n\n{src}" if src else "")


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
        return ("I don't have any rated ideas saved yet. Generate them from the dashboard "
                "(*Generate Rated Creative Ideas*) or `generate-ideas`, then ask me again.")

    q = parse_query(text)
    ranked = rank_ideas(rows, q)
    if not ranked:
        scope = " ".join(x for x in (q["product"], q["icp"]) if x) or "that"
        return (f"I don't have any eligible rated ideas for *{scope}* right now. "
                "Ask for another product/ICP, or *what are the best ideas we have?*")

    if q["mode"] == "critique":
        return _render_critique(ranked, q)
    if q["mode"] == "generic":
        return _render_generic(ranked, q)
    if q["mode"] == "shoot_first":
        return _render_shoot_first(ranked, q)
    if q["mode"] == "evidence":
        return _render_evidence(ranked, q)
    return _render_list(ranked, q)
