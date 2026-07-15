"""Slack Conversational RAG Orchestrator.

Sits in front of the deterministic Slack answer paths. It parses intent, resolves
references from the prior thread turn ("it", "the one you suggested", "tell me
more", "the first one"), builds a small focused evidence pack, reasons over it
(e.g. urgency = strategic value + shootability + evidence + learning value, NOT
just IDEA_SCORE), and renders a concise, source-backed, CEO-readable answer.

Read-only. Never writes Sheets/Notion, never generates ideas. Returns None when
it has nothing better to add, so the existing (concise) retrieval paths handle
the turn. If a reference can't be resolved, it asks a clarifying question rather
than hallucinating.
"""
from __future__ import annotations

import re
from typing import Optional

import slack_response_style as st
from idea_retrieval import (SourceRegistry, _cite_idea, _display_risk,
                            _field, _first_sentence, _split, _uses_refined)
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"


def _num(v, default=0.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())).strip()


def _title(idea: dict) -> str:
    return _field(idea, "REFINED_IDEA_TITLE", "IDEA_TITLE") or "Untitled"


# ---------------------------------------------------------------------------
# Part A — conversation memory
# ---------------------------------------------------------------------------
def build_memory(context: Optional[list], ideas: list[dict]) -> dict:
    """Extract structured memory from the prior assistant turn."""
    ctx = context or []
    last_assistant = next((m.get("text", "") for m in reversed(ctx)
                           if m.get("role") == "assistant"), "")
    # Ordered idea titles: numbered list first, else the "My move: Shoot *X*" line.
    titles = re.findall(r"\*\d+\.\s*([^*\n]+?)\*", last_assistant)
    if not titles:
        titles = re.findall(r"(?:Shoot|Prioritize|Start with)\s+\*([^*\n]+?)\*", last_assistant)
    by_title = {}
    for idea in ideas:
        for k in ("REFINED_IDEA_TITLE", "IDEA_TITLE"):
            nt = _norm(idea.get(k, ""))
            if nt:
                by_title.setdefault(nt, idea)
    ids, matched_titles = [], []
    for t in titles:
        idea = by_title.get(_norm(t)) or next(
            (v for k, v in by_title.items() if _norm(t) and _norm(t) in k), None)
        if idea and idea.get("IDEA_ID") not in ids:
            ids.append(idea.get("IDEA_ID"))
            matched_titles.append(_title(idea))
    product = ""
    for p in ("BodyShield", "Leggings", "Gloves", "ExoShield", "Pants", "Sliders"):
        if p.lower() in last_assistant.lower():
            product = p
            break
    return {
        "last_recommended_idea_ids": ids,
        "last_recommended_idea_titles": matched_titles,
        "last_product": product,
        "last_answer_summary": (last_assistant.strip().splitlines() or [""])[0][:160],
    }


_ORDINAL = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2}
_REF_WORDS = ("tell me more", "more about", "the one you", "you suggested",
              "you recommended", "that idea", "this idea", "about it", " it ",
              "is this shootable", "why this", "why that", "deep dive", "explain")


def resolve_idea_reference(text: str, ideas: list[dict], memory: dict) -> tuple:
    """Return (idea, how) where how in {'explicit','memory','ambiguous','none'}."""
    nt = _norm(text)
    # 1) explicit idea title present in the message.
    best, best_len = None, 0
    for idea in ideas:
        for k in ("REFINED_IDEA_TITLE", "IDEA_TITLE"):
            title = _norm(idea.get(k, ""))
            if len(title) >= 8 and title in nt and len(title) > best_len:
                best, best_len = idea, len(title)
    if best:
        return best, "explicit"
    # 2) pronoun / ordinal reference to a prior recommendation.
    if any(w in (" " + text.lower() + " ") for w in _REF_WORDS):
        ids = memory.get("last_recommended_idea_ids") or []
        byid = {i.get("IDEA_ID"): i for i in ideas}
        idx = 0
        m = re.search(r"\b(first|second|third|1st|2nd|3rd)\b", text.lower())
        if m:
            idx = _ORDINAL.get(m.group(1), 0)
        if ids and idx < len(ids) and ids[idx] in byid:
            return byid[ids[idx]], "memory"
        return None, "ambiguous"
    return None, "none"


# ---------------------------------------------------------------------------
# Part B — intent detection
# ---------------------------------------------------------------------------
def detect_intent(text: str, memory: dict) -> str:
    t = " " + (text or "").lower() + " "
    if ("urgent" in t or "priorit" in t) and ("test" in t or "idea" in t):
        return "urgent_tests"
    if "what should we test" in t or "what to test" in t:
        return "urgent_tests"
    if any(k in t for k in ("compare", " vs ", "versus", "which is better", "better idea")):
        return "compare_ideas"
    if any(k in t for k in ("tell me more", "more about", "you suggested", "you recommended",
                            "deep dive", "why this one", "is this shootable", "explain the")):
        return "idea_deep_dive"
    return "fallback_question"


# ---------------------------------------------------------------------------
# Part C — evidence-pack reasoning
# ---------------------------------------------------------------------------
def _shootability(i: dict) -> float:
    return (_num(i.get("EXECUTION_CLARITY_SCORE")) + _num(i.get("FEASIBILITY_SCORE"))) / 2


def _product_priority(i: dict) -> float:
    c = str(i.get("CONFIDENCE", "")).strip().lower()
    return 90.0 if c == "high" else 75.0 if c == "medium" else 60.0


def urgency_score(i: dict) -> float:
    """Test-urgency reasoning — deliberately NOT just IDEA_SCORE. Weighs strategic
    value, shootability now, internal evidence strength, learning value
    (novelty), and product priority."""
    return round(0.30 * _num(i.get("STRATEGIC_PRIORITY_SCORE"))
                 + 0.20 * _shootability(i)
                 + 0.20 * _num(i.get("EVIDENCE_FIT_SCORE"))
                 + 0.15 * _num(i.get("NOVELTY_SCORE"))
                 + 0.15 * _product_priority(i), 1)


def _urgency_reasons(i: dict) -> str:
    r = []
    if _shootability(i) >= 85:
        r.append("shootable now")
    elif _shootability(i) < 65:
        r.append("harder to shoot")
    if _num(i.get("EVIDENCE_FIT_SCORE")) >= 85:
        r.append("strong internal proof")
    elif _num(i.get("EVIDENCE_FIT_SCORE")) < 70:
        r.append("thinner internal proof")
    if _num(i.get("NOVELTY_SCORE")) >= 80:
        r.append("fresh learning angle")
    if _product_priority(i) >= 90:
        r.append("high-priority product")
    return ", ".join(r[:3]) or "solid all-round"


# ---------------------------------------------------------------------------
# composition (deterministic; strategist-grade)
# ---------------------------------------------------------------------------
def _compose_urgent(ideas: list[dict], mode: str) -> Optional[str]:
    pool = [i for i in ideas if str(i.get("STATUS", "")).strip().lower()
            in ("", "proposed", "draft", "review")]
    if not pool:
        return None
    ranked = sorted(pool, key=urgency_score, reverse=True)[:3]
    reg = SourceRegistry()
    bullets = []
    for i in ranked:
        s_txt, e_txt = _cite_idea(i, reg)
        bullets.append(f"*{_title(i)}* — {_urgency_reasons(i)} · {s_txt} {e_txt}")
    lead = ("Test these first — I'm ranking by strategic value we can actually shoot and "
            "learn from now, not just idea score.")
    move = f"Start with *{_title(ranked[0])}* ({ranked[0].get('RECOMMENDED_SHOOT_PRIORITY', 'Medium')} priority)."
    src = reg.render()
    return st.render_ceo_summary(lead, why=bullets, move=move,
                                 sources=(f"{src}\n{_NOT_PROOF}" if src else ""), mode=mode)


def _compose_deep_dive(idea: dict, mode: str) -> str:
    reg = SourceRegistry()
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "a winning profile"
    s_ids = [reg.internal(u, prof) for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))[:3]]
    e_ids = [reg.external(u) for u in _split(idea.get("EXTERNAL_REFERENCE_URLS"))[:3]]
    concept = _field(idea, "REFINED_CONCEPT", "CONCEPT")
    shot = _field(idea, "REFINED_SHOT_LIST", "SHOT_LIST")
    film = _first_sentence(shot.split("|")[0] if shot else concept, 18)
    lead = f"Here's the rundown on *{_title(idea)}*:"
    why = [
        f"Why this one: anchored to internal winning profile *{prof}* "
        f"(confidence {idea.get('CONFIDENCE', '?')}), idea score {idea.get('IDEA_SCORE', '?')}.",
        f"What to film: {film}.",
        f"Storelli proof it works: {' '.join('[' + x + ']' for x in s_ids) or '(profile evidence)'}.",
        f"External inspiration (reference only, not proof): {' '.join('[' + x + ']' for x in e_ids) or '(none)'}.",
        f"Watch out: {_display_risk(idea)}.",
    ]
    move = (f"Shoot it {idea.get('RECOMMENDED_SHOOT_PRIORITY', 'Medium')} priority — "
            f"CTA: {str(idea.get('CTA', 'tie to the product')).strip()[:60]}.")
    src = reg.render()
    return st.render_ceo_summary(lead, why=why, move=move,
                                 sources=(f"{src}\n{_NOT_PROOF}" if src else ""), mode=mode)


def _compose_compare(ideas: list[dict], mode: str) -> Optional[str]:
    from idea_retrieval import parse_query, rank_ideas
    ranked = rank_ideas(ideas, parse_query(""))[:2]
    if len(ranked) < 2:
        return None
    reg = SourceRegistry()
    a, b = ranked
    bullets = [
        f"*{_title(a)}* — score {a.get('IDEA_SCORE', '?')}, {_urgency_reasons(a)}.",
        f"*{_title(b)}* — score {b.get('IDEA_SCORE', '?')}, {_urgency_reasons(b)}.",
    ]
    winner = a if urgency_score(a) >= urgency_score(b) else b
    lead = "Head-to-head:"
    move = f"Go with *{_title(winner)}* first — better strategic value we can shoot now."
    return st.render_ceo_summary(lead, why=bullets, move=move, sources="", mode=mode)


def _clarify(memory: dict) -> str:
    titles = memory.get("last_recommended_idea_titles") or []
    if len(titles) >= 2:
        return (f"I'm not sure which one you mean — *{titles[0]}* or *{titles[1]}*? "
                "Name it and I'll dig in.")
    if titles:
        return f"Do you mean *{titles[0]}*? Say the word and I'll break it down."
    return ("I'm missing the previous item you mean — tell me the idea (or product) and "
            "I'll dig into it.")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
def _load_ideas(sheets) -> list[dict]:
    if sheets is None:
        from inspiration_sheets import InspirationSheets
        sheets = InspirationSheets()
    return sheets.read_ideas()   # read-only


def answer(text: str, context: Optional[list] = None, sheets=None,
           ideas: Optional[list] = None) -> Optional[str]:
    """Return a reasoned answer for reasoning-heavy intents, else None so the
    existing concise retrieval paths handle the turn."""
    try:
        rows = ideas if ideas is not None else _load_ideas(sheets)
    except Exception as e:  # noqa: BLE001
        log.warning("orchestrator: could not load ideas: %s", e)
        return None
    if not rows:
        return None

    mode = st.detect_response_mode(text)
    memory = build_memory(context, rows)
    intent = detect_intent(text, memory)

    try:
        if intent == "urgent_tests":
            return _compose_urgent(rows, mode)
        if intent == "compare_ideas":
            return _compose_compare(rows, mode)
        if intent == "idea_deep_dive":
            idea, how = resolve_idea_reference(text, rows, memory)
            if idea:
                return _compose_deep_dive(idea, mode)
            if how == "ambiguous":
                return _clarify(memory)
            return None
        # Not a reasoning-heavy intent — but if the message clearly names a
        # specific idea and asks about it, deep-dive anyway (fixes lost context).
        idea, how = resolve_idea_reference(text, rows, memory)
        if idea and how == "explicit" and "?" in text:
            return _compose_deep_dive(idea, mode)
        return None
    except Exception as e:  # noqa: BLE001 - never break the bot; fall back deterministically
        log.warning("orchestrator failed, falling through: %s", e)
        return None
