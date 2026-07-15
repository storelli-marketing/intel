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
from idea_retrieval import _display_risk, _field, _first_sentence, _split
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
    if "why" in t and any(k in t for k in (" before ", " over ", " instead of ",
                                           " rather than ", " ahead of ")):
        return "compare_ideas"
    if any(k in t for k in ("tell me more", "more about", "you suggested", "you recommended",
                            "deep dive", "why this", "why that", "this idea", "that idea",
                            "is this shootable", "explain the", "about it")):
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
# source map — stable [S#]/[E#]/[N#] ids -> real URLs, filterable by used ids
# ---------------------------------------------------------------------------
def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url) or re.search(r"instagram\.com/([\w.\-]+)/", url)
    return "@" + m.group(1) if m else "creator"


class _SrcMap:
    def __init__(self):
        self.s, self.e, self.n = {}, {}, {}

    def internal(self, url: str) -> str:
        url = (url or "").strip()
        if url and url not in self.s:
            self.s[url] = (f"S{len(self.s) + 1}", "Storelli internal proof")
        return self.s.get(url, ("", ""))[0]

    def external(self, url: str) -> str:
        url = (url or "").strip()
        if url and url not in self.e:
            self.e[url] = (f"E{len(self.e) + 1}", f"External inspiration — {_handle(url)}")
        return self.e.get(url, ("", ""))[0]

    def all_ids(self) -> set:
        return ({v[0] for v in self.s.values()} | {v[0] for v in self.e.values()}
                | {v[0] for v in self.n.values()})

    def render(self, used: Optional[set] = None) -> str:
        items = []
        for store in (self.s, self.e, self.n):
            for url, (sid, label) in store.items():
                if used is None or sid in used:
                    items.append((sid, url, label))
        return st.compact_sources(items)


def _idea_ids(idea: dict, srcmap: _SrcMap) -> tuple:
    s = [x for x in (srcmap.internal(u) for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))[:3]) if x]
    e = [x for x in (srcmap.external(u) for u in _split(idea.get("EXTERNAL_REFERENCE_URLS"))[:3]) if x]
    return s, e


# ---------------------------------------------------------------------------
# pack builders — each returns (deterministic_answer, facts, srcmap)
# ---------------------------------------------------------------------------
def _pack_urgent(ideas: list[dict], mode: str):
    pool = [i for i in ideas if str(i.get("STATUS", "")).strip().lower()
            in ("", "proposed", "draft", "review")]
    if not pool:
        return None
    ranked = sorted(pool, key=urgency_score, reverse=True)[:3]
    srcmap = _SrcMap()
    facts = ["Question: which proposed ideas are most URGENT to test now, and why "
             "(reason from evidence, not just idea score)."]
    det_bullets = []
    for i in ranked:
        s, e = _idea_ids(i, srcmap)
        cite = " ".join(f"[{x}]" for x in s + e)
        facts.append(
            f"- {_title(i)}: urgency {urgency_score(i)}, idea_score {i.get('IDEA_SCORE', '?')}, "
            f"strategic {_num(i.get('STRATEGIC_PRIORITY_SCORE'))}, shootability {round(_shootability(i))}, "
            f"evidence_fit {_num(i.get('EVIDENCE_FIT_SCORE'))}, novelty {_num(i.get('NOVELTY_SCORE'))}, "
            f"product '{i.get('PRODUCT', '')}'/{i.get('ICP', '')}, why: {_urgency_reasons(i)}; sources {cite}")
        det_bullets.append(f"*{_title(i)}* — {_urgency_reasons(i)} · {cite}")
    lead = ("Test these first — ranking by strategic value we can actually shoot and learn "
            "from now, not just idea score.")
    move = f"Start with *{_title(ranked[0])}* ({ranked[0].get('RECOMMENDED_SHOOT_PRIORITY', 'Medium')} priority)."
    src = srcmap.render()
    det = st.render_ceo_summary(lead, why=det_bullets, move=move,
                                sources=(f"{src}\n{_NOT_PROOF}" if src else ""), mode=mode)
    return det, "\n".join(facts), srcmap


def _pack_deep_dive(idea: dict, mode: str):
    srcmap = _SrcMap()
    s, e = _idea_ids(idea, srcmap)
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "a winning profile"
    concept = _field(idea, "REFINED_CONCEPT", "CONCEPT")
    shot = _field(idea, "REFINED_SHOT_LIST", "SHOT_LIST")
    film = _first_sentence(shot.split("|")[0] if shot else concept, 18)
    facts = (
        f"Question: deep dive on one idea — why it, what to film, evidence, risk.\n"
        f"Idea: {_title(idea)} (product {idea.get('PRODUCT', '')}/{idea.get('ICP', '')})\n"
        f"Internal winning profile (PROOF the format works): {prof}, confidence {idea.get('CONFIDENCE', '?')}\n"
        f"Scores: idea {idea.get('IDEA_SCORE', '?')}, strategic {_num(idea.get('STRATEGIC_PRIORITY_SCORE'))}, "
        f"shootability {round(_shootability(idea))}, evidence_fit {_num(idea.get('EVIDENCE_FIT_SCORE'))}, "
        f"novelty {_num(idea.get('NOVELTY_SCORE'))}\n"
        f"What to film (from shot list): {film}\n"
        f"Known risk/watch-out: {_display_risk(idea)}\n"
        f"CTA: {str(idea.get('CTA', '')).strip()[:80]}\n"
        f"Internal proof sources: {' '.join('[' + x + ']' for x in s) or '(profile evidence only)'}; "
        f"External inspiration (reference only, NOT proof): {' '.join('[' + x + ']' for x in e) or '(none)'}")
    why = [
        f"Why this one: anchored to internal winning profile *{prof}* "
        f"(confidence {idea.get('CONFIDENCE', '?')}), idea score {idea.get('IDEA_SCORE', '?')}.",
        f"What to film: {film}.",
        f"Storelli proof it works: {' '.join('[' + x + ']' for x in s) or '(profile evidence)'}.",
        f"External inspiration (reference only, not proof): {' '.join('[' + x + ']' for x in e) or '(none)'}.",
        f"Watch out: {_display_risk(idea)}.",
    ]
    move = (f"Shoot it {idea.get('RECOMMENDED_SHOOT_PRIORITY', 'Medium')} priority — "
            f"CTA: {str(idea.get('CTA', 'tie to the product')).strip()[:60]}.")
    src = srcmap.render()
    det = st.render_ceo_summary(f"Here's the rundown on *{_title(idea)}*:", why=why, move=move,
                                sources=(f"{src}\n{_NOT_PROOF}" if src else ""), mode=mode)
    return det, facts, srcmap


def _pack_compare(a: dict, b: dict, mode: str):
    srcmap = _SrcMap()
    facts = ["Question: why prioritize the FIRST idea over the SECOND — reason from evidence."]
    det_bullets = []
    for i in (a, b):
        s, e = _idea_ids(i, srcmap)
        cite = " ".join(f"[{x}]" for x in s + e)
        facts.append(
            f"- {_title(i)}: urgency {urgency_score(i)}, idea_score {i.get('IDEA_SCORE', '?')}, "
            f"shootability {round(_shootability(i))}, evidence_fit {_num(i.get('EVIDENCE_FIT_SCORE'))}, "
            f"novelty {_num(i.get('NOVELTY_SCORE'))}, why: {_urgency_reasons(i)}; sources {cite}")
        det_bullets.append(f"*{_title(i)}* — score {i.get('IDEA_SCORE', '?')}, {_urgency_reasons(i)}.")
    winner = a if urgency_score(a) >= urgency_score(b) else b
    src = srcmap.render()
    det = st.render_ceo_summary(
        "Head-to-head:", why=det_bullets,
        move=f"Go with *{_title(winner)}* first — stronger strategic value we can shoot now.",
        sources=(f"{src}\n{_NOT_PROOF}" if src else ""), mode=mode)
    return det, "\n".join(facts), srcmap


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
# Part D — bounded LLM synthesis (deterministic fallback)
# ---------------------------------------------------------------------------
_LLM_INTENTS = {"urgent_tests", "idea_deep_dive", "compare_ideas", "evidence_check"}
_SYNTH_TRIGGERS = ("why", "tell me more", "go deeper", "explain", "compare",
                   "strategy", "what should we do")

_PROMPT = (
    "You are Storelli's senior creative strategist. Use ONLY the evidence pack below. "
    "Be blunt, practical, and concise. Do not invent facts, sources, scores, or URLs. "
    "Internal Storelli evidence [S#] is proof. External inspiration [E#] is reference "
    "only, NOT proof — never say external content or its views prove performance. [N#] "
    "is a Notion/calendar item. If evidence is thin, say so. Answer like a sharp "
    "teammate, not a report.\n\n"
    "Evidence pack:\n<<FACTS>>\n\n"
    "You may cite ONLY these source ids: <<IDS>>. Never invent an id.\n\n"
    "Return ONLY strict JSON (no prose): {\"lead\": str, \"bullets\": [str, str, str], "
    "\"my_move\": str, \"confidence\": \"High|Medium|Thin\", \"source_ids_used\": [str], "
    "\"memory_update\": {\"last_recommended_idea_ids\": [str], \"last_answer_summary\": str}}")


def _validate(obj, allowed: set, mode: str, deep: bool) -> tuple:
    if not isinstance(obj, dict):
        return False, "not a dict"
    lead = str(obj.get("lead", "")).strip()
    move = str(obj.get("my_move", "")).strip()
    bullets = obj.get("bullets")
    if not lead:
        return False, "empty lead"
    if not move:
        return False, "empty my_move"
    if not (isinstance(bullets, list) and bullets and all(str(b).strip() for b in bullets)):
        return False, "empty bullets"
    if str(obj.get("confidence", "")).strip().lower() not in ("high", "medium", "thin"):
        return False, "bad confidence"
    used = obj.get("source_ids_used", [])
    if not isinstance(used, list):
        return False, "source_ids_used not a list"
    for i in used:
        if str(i).strip() not in allowed:
            return False, f"hallucinated source '{i}'"
    text = " ".join([lead, move] + [str(b) for b in bullets]).lower()
    if re.search(r"(external|inspiration)[^.]{0,40}\bprov(e|es|en|ing)\b", text):
        return False, "external framed as proof"
    if re.search(r"views?\s+\w{0,12}\s*prov", text):
        return False, "views framed as proof"
    if re.search(r"\[e\d+\][^.]{0,25}prov", text):
        return False, "E-source framed as proof"
    cap = st.WORD_CAP[st.MODE_DEEP] if deep else st.WORD_CAP.get(mode, st.WORD_CAP[st.MODE_DEFAULT])
    if len(text.split()) > cap * 1.6:
        return False, "too long"
    return True, ""


def _synthesize(facts: str, srcmap: _SrcMap, mode: str, gemini, deep: bool) -> Optional[str]:
    allowed = srcmap.all_ids()
    prompt = _PROMPT.replace("<<FACTS>>", facts).replace(
        "<<IDS>>", ", ".join(sorted(allowed)) or "(none)")
    try:
        from analyzer import parse_model_json
        obj = parse_model_json(gemini.summarize_findings(prompt))
    except Exception as e:  # noqa: BLE001
        log.warning("orchestrator LLM synth failed: %s", e)
        return None
    ok, reason = _validate(obj, allowed, mode, deep)
    if not ok:
        log.info("orchestrator LLM synth rejected (%s) -> deterministic", reason)
        return None
    used = {str(i).strip() for i in obj.get("source_ids_used", []) if str(i).strip() in allowed}
    sources = srcmap.render(used) if used else ""
    return st.render_ceo_summary(
        str(obj["lead"]).strip(), why=[str(b).strip() for b in obj["bullets"]],
        move=str(obj["my_move"]).strip(),
        sources=(f"{sources}\n{_NOT_PROOF}" if sources else ""),
        mode=(st.MODE_DEEP if deep else mode))


def _finalize(pack, mode: str, gemini, deep: bool, use_llm: bool) -> Optional[str]:
    if pack is None:
        return None
    det, facts, srcmap = pack
    if use_llm and gemini is not None:
        return _synthesize(facts, srcmap, mode, gemini, deep) or det
    return det


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
def _load_ideas(sheets) -> list[dict]:
    if sheets is None:
        from inspiration_sheets import InspirationSheets
        sheets = InspirationSheets()
    return sheets.read_ideas()   # read-only


def _resolve_gemini(gemini):
    if gemini == "auto":
        try:
            from gemini_client import GeminiClient
            return GeminiClient()
        except Exception:  # noqa: BLE001
            return None
    return gemini


def _resolve_compare(text: str, ideas: list[dict], memory: dict):
    """(first, second) for a 'why shoot that over/before X' question: `that` from
    memory, X named in the message."""
    explicit, how = resolve_idea_reference(text, ideas, {"last_recommended_idea_ids": []})
    ids = memory.get("last_recommended_idea_ids") or []
    byid = {i.get("IDEA_ID"): i for i in ideas}
    prior = byid.get(ids[0]) if ids else None
    if explicit and prior and explicit.get("IDEA_ID") != prior.get("IDEA_ID"):
        return prior, explicit
    return None


def answer(text: str, context: Optional[list] = None, sheets=None,
           ideas: Optional[list] = None, gemini="auto") -> Optional[str]:
    """Reasoned answer for reasoning-heavy intents (deterministic ranking +
    optional LLM synthesis of the focused evidence pack), else None so the
    existing concise retrieval paths handle the turn."""
    try:
        rows = ideas if ideas is not None else _load_ideas(sheets)
    except Exception as e:  # noqa: BLE001
        log.warning("orchestrator: could not load ideas: %s", e)
        return None
    if not rows:
        return None

    mode = st.detect_response_mode(text)
    deep = mode == st.MODE_DEEP
    memory = build_memory(context, rows)
    intent = detect_intent(text, memory)
    use_llm = (intent in _LLM_INTENTS
               or any(k in (text or "").lower() for k in _SYNTH_TRIGGERS))
    gem = _resolve_gemini(gemini) if use_llm else None

    try:
        if intent == "urgent_tests":
            return _finalize(_pack_urgent(rows, mode), mode, gem, deep, use_llm)
        if intent == "compare_ideas":
            pair = _resolve_compare(text, rows, memory)
            if not pair:
                from idea_retrieval import parse_query, rank_ideas
                top = rank_ideas(rows, parse_query(""))[:2]
                pair = tuple(top) if len(top) >= 2 else None
            if not pair:
                return None
            return _finalize(_pack_compare(pair[0], pair[1], mode), mode, gem, deep, use_llm)
        if intent == "idea_deep_dive":
            idea, how = resolve_idea_reference(text, rows, memory)
            if idea:
                return _finalize(_pack_deep_dive(idea, mode), mode, gem, deep, use_llm)
            if how == "ambiguous":
                return _clarify(memory)      # never hallucinate a missing reference
            return None
        # Explicit named idea + a question -> deep dive (fixes lost context).
        idea, how = resolve_idea_reference(text, rows, memory)
        if idea and how == "explicit" and "?" in text:
            return _finalize(_pack_deep_dive(idea, mode), mode, gem, deep, use_llm)
        return None
    except Exception as e:  # noqa: BLE001 - never break the bot; fall back deterministically
        log.warning("orchestrator failed, falling through: %s", e)
        return None
