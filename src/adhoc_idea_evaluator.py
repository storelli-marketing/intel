"""Ad-Hoc Notion Idea Evaluation Layer.

Evaluate any specific idea (pasted as a Notion page URL in Slack) against the
Storelli brain: internal winning profiles + learnings (PROOF), semantic
connections, external inspiration (execution reference ONLY, never proof),
existing rated/refined ideas, and calendar ratings. Scores are deterministic
and reproducible; Gemini only enriches the narrative and picks which reference
videos to call out — never the numbers or the recommendation.

Evaluations are stored in the ADHOC_IDEA_EVALUATIONS Google Sheet tab for
memory/history. NOTHING is written back to Notion; no internal Storelli rows,
profiles, or calendar ratings are ever modified.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

import slack_response_style as st
from calendar_rater import (closest_profile, closest_rated_idea, hook_strength,
                            internal_evidence_fit, inspiration_alignment,
                            matching_inspiration, novelty, product_icp_fit,
                            shootability, _family, _has_specific_signal, _tokens)
from idea_generator import copyright_recheck
from inspiration_sheets import InspirationSheets
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External = execution reference only, not proof it works for Storelli._"

# IDEA_EVALUATION_SCORE weights (sum = 1.0).
W_EVIDENCE = 0.25
W_SEMANTIC = 0.20
W_INSPIRATION = 0.15
W_STRUCTURE = 0.15
W_PRODICP = 0.10       # avg(product_fit, icp_fit)
W_SHOOT = 0.10
W_COPYRIGHT = 0.05

_UNSAFE = ("messi", "ronaldo", "neymar", "mbappe", "champions league", "premier league",
           "world cup", "match highlights", "broadcast", "fan edit", "full match")


def _num(v, default=0.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _clamp(v):
    return max(0.0, min(100.0, v))


def _split(cell) -> list[str]:
    return [u.strip() for u in re.split(r"[;\n]", str(cell or "")) if u.strip()]


# ---------------------------------------------------------------------------
# normalized-idea -> calendar-style item (so we can reuse calendar_rater math)
# ---------------------------------------------------------------------------
def _as_item(idea: dict) -> dict:
    notes = " ".join(str(idea.get(k, "")) for k in ("concept", "caption", "script", "notes")).strip()
    return {"title": idea.get("title", ""), "notes": notes,
            "product": idea.get("product", ""), "icp": idea.get("icp", ""),
            "platform": idea.get("platform", ""), "asset_format": idea.get("format", ""),
            "has_camera_emoji": False}


# ---------------------------------------------------------------------------
# retrieval — semantic connections (new); profiles/inspiration/ideas reused
# ---------------------------------------------------------------------------
def closest_semantic_connection(idea: dict, connections: list[dict]) -> tuple[Optional[dict], float]:
    blob = f"{idea.get('title','')} {idea.get('concept','')} {idea.get('hook','')} {idea.get('product','')}"
    fam = _family(blob)
    pool = [c for c in connections if not fam or _family(c.get("PRODUCT", "")) == fam]
    if not pool:
        return None, 0.0
    toks = _tokens(blob)
    best, best_fit = None, 0.0
    for c in pool:
        fit = 0.0
        if fam and _family(c.get("PRODUCT", "")) == fam:
            fit += 0.5
        overlap = toks & _tokens(f"{c.get('CONCEPT_NAME','')} {c.get('STORYTELLING_STRUCTURE','')} "
                                 f"{c.get('HOOK_ARCHETYPE','')} {c.get('PROBLEM_TYPE','')}")
        fit += min(0.3, 0.06 * len(overlap))
        fit += 0.2 * (_num(c.get("CONNECTION_SCORE")) / 100)
        if fit > best_fit:
            best, best_fit = c, fit
    return best, round(best_fit, 3)


def connection_external_refs(conn: Optional[dict], inspiration: list[dict], k: int = 5) -> list[dict]:
    """External inspiration rows referenced by a semantic connection (safe /
    high-quality only). Falls back to [] when the connection has none."""
    if not conn:
        return []
    ids = set(_split(conn.get("EXTERNAL_CONTENT_IDS")))
    urls = set(_split(conn.get("EXTERNAL_REFERENCE_URLS")))
    refs = [r for r in inspiration
            if str(r.get("SOURCE_ID", "")).strip() in ids
            or str(r.get("POST_URL", "")).strip() in urls]
    refs = [r for r in refs
            if str(r.get("SAFETY_STATUS", "")).strip().lower() == "safe"
            and _num(r.get("INSPIRATION_QUALITY_SCORE")) >= 80]
    refs.sort(key=lambda r: _num(r.get("INSPIRATION_QUALITY_SCORE")), reverse=True)
    return refs[:k]


# ---------------------------------------------------------------------------
# deterministic sub-scores
# ---------------------------------------------------------------------------
def semantic_connection_fit(conn: Optional[dict], fit01: float) -> float:
    if not conn:
        return 35.0
    base = _num(conn.get("CONNECTION_SCORE"), 80.0)
    return round(_clamp(40 + (base - 40) * min(1.0, 0.4 + fit01)), 1)


def storytelling_structure_fit(idea: dict, conn: Optional[dict]) -> float:
    s = 50.0
    if conn and str(conn.get("STORYTELLING_STRUCTURE", "")).strip():
        s += 25
    body = " ".join(str(idea.get(k, "")) for k in ("concept", "caption", "script"))
    if _has_specific_signal(idea.get("title", "")) or len(body.strip()) >= 60:
        s += 20
    if str(idea.get("hook", "")).strip():
        s += 5
    return round(_clamp(s), 1)


def evaluation_score(evidence, semantic, inspiration, structure, product_fit, icp_fit,
                     shoot, copyright_safety) -> float:
    return round(
        W_EVIDENCE * evidence + W_SEMANTIC * semantic + W_INSPIRATION * inspiration
        + W_STRUCTURE * structure + W_PRODICP * (product_fit + icp_fit) / 2
        + W_SHOOT * shoot + W_COPYRIGHT * copyright_safety, 1)


# ---------------------------------------------------------------------------
# recommendation (guarded)
# ---------------------------------------------------------------------------
_GENERIC = ("game changer", "game-changer", "dominate", "unleash", "unbreakable",
            "secret", "ultimate", "insane", "next level")


def _is_vague(idea: dict) -> bool:
    body = " ".join(str(idea.get(k, "")) for k in ("concept", "caption", "script", "notes"))
    body_chars = len(re.sub(r"[^a-z0-9]", "", body.lower()))
    specific = _has_specific_signal(idea.get("title", "")) or body_chars >= 50
    generic = any(g in f"{idea.get('title','')}".lower() for g in _GENERIC)
    return (not specific) or generic


def recommend(score: float, idea: dict, has_content: bool, has_internal_evidence: bool,
              copyright_safe: bool) -> str:
    if not has_content or not str(idea.get("product", "")).strip():
        return "Needs more info"          # unclear product / not enough to judge
    if not copyright_safe:
        return "Do not prioritize"        # match-footage / famous-player risk
    if _is_vague(idea):
        return "Revise then shoot"
    if score >= 78 and has_internal_evidence:
        rec = "Shoot"
    elif score >= 62:
        rec = "Revise then shoot"
    elif score >= 48:
        rec = "Keep as test"
    else:
        rec = "Do not prioritize"
    # No internal evidence -> can be an interesting test, never a strong keep.
    if not has_internal_evidence and rec == "Shoot":
        rec = "Revise then shoot"
    return rec


def confidence_label(has_internal_evidence: bool, conn: Optional[dict], conn_fit: float) -> str:
    if has_internal_evidence and conn and conn_fit >= 0.5:
        return "High"
    if has_internal_evidence or conn:
        return "Medium"
    return "Thin"


# ---------------------------------------------------------------------------
# LLM synthesis (Part D) — validated, deterministic fallback
# ---------------------------------------------------------------------------
_SYNTH_PROMPT = (
    "You are Storelli's senior creative strategist. Evaluate this Notion idea using ONLY the "
    "evidence pack. Internal Storelli evidence is PROOF. External inspiration is execution "
    "reference ONLY (never proof; never say its views prove anything). Be blunt, practical, and "
    "concise. Do not invent facts, scores, links, or source IDs. No famous players, match, or "
    "broadcast footage.\n\n"
    "The deterministic recommendation is '<<REC>>' and score is <<SCORE>>/100 — keep your "
    "narrative consistent with it.\n\n"
    "Evidence pack:\n<<FACTS>>\n\n"
    "Return ONLY strict JSON: {\"recommendation\": str, \"confidence\": str, \"lead\": str, "
    "\"why\": [str], \"what_works\": [str], \"what_is_weak\": [str], \"how_to_improve\": [str], "
    "\"suggested_story_structure\": str, \"videos_to_take_inspo_from\": [{\"source_id\": str, "
    "\"why\": str, \"what_to_steal\": str, \"what_not_to_copy\": str}], \"my_move\": str, "
    "\"source_ids_used\": [str]}")


def _facts(idea, profile, conn, refs, close_idea, cal, score, rec) -> tuple:
    lines, allowed = [], set()
    lines.append(f"[N1] Notion idea (the thing being evaluated): "
                 f"'{idea.get('title','')}' — {idea.get('concept','') or idea.get('notes','')}"[:400])
    lines.append(f"Product/ICP/Format/Hook: {idea.get('product','?')}/{idea.get('icp','?')}/"
                 f"{idea.get('format','?')}/{idea.get('hook','?')}")
    allowed.add("N1")
    s_urls = []
    if profile:
        s_urls = _split(profile.get("SUPPORTING_VIDEO_URLS"))[:3]
        lines.append(f"[S1] Storelli internal PROOF — winning profile '{profile.get('PROFILE_NAME','')}' "
                     f"(confidence {profile.get('CONFIDENCE','?')}, sample {profile.get('INTERNAL_SAMPLE_SIZE','?')}).")
        allowed.add("S1")
    if conn:
        lines.append(f"[C1] Semantic connection '{conn.get('CONCEPT_NAME','')}' — structure "
                     f"{conn.get('STORYTELLING_STRUCTURE','')}; steal: {conn.get('WHAT_TO_STEAL','')[:120]}.")
        allowed.add("C1")
    for i, r in enumerate(refs, 1):
        eid = f"E{i}"
        lines.append(f"[{eid}] External inspiration (reference only) {_handle(str(r.get('POST_URL','')))}: "
                     f"mechanism '{str(r.get('CREATIVE_MECHANISM',''))[:60]}', "
                     f"caption '{str(r.get('CAPTION',''))[:70]}'.")
        allowed.add(eid)
    if close_idea:
        lines.append(f"[I1] Similar rated Storelli idea: "
                     f"'{close_idea.get('REFINED_IDEA_TITLE') or close_idea.get('IDEA_TITLE','')}' "
                     f"(score {close_idea.get('IDEA_SCORE','?')}).")
        allowed.add("I1")
    if cal:
        lines.append(f"[CAL1] Calendar rating for a similar item: '{cal.get('CALENDAR_TITLE','')}' "
                     f"-> {cal.get('RECOMMENDATION','')} ({cal.get('CALENDAR_IDEA_SCORE','?')}).")
        allowed.add("CAL1")
    facts = "\n".join(lines)
    facts = facts  # for clarity
    return facts, allowed, s_urls


def _validate_synth(obj, allowed: set) -> tuple:
    if not isinstance(obj, dict):
        return False, "not dict"
    used = [str(x).strip() for x in (obj.get("source_ids_used") or [])]
    if any(u and u not in allowed for u in used):
        return False, "invented source id"
    text = " ".join([str(obj.get("lead", "")),
                     " ".join(map(str, obj.get("why", []) or [])),
                     " ".join(map(str, obj.get("what_works", []) or [])),
                     " ".join(map(str, obj.get("how_to_improve", []) or []))]).lower()
    if re.search(r"(external|inspiration|their video|reference)[^.]{0,40}\bprov(e|es|en|ing)\b", text):
        return False, "external as proof"
    if any(u in text for u in _UNSAFE):
        return False, "unsafe reference"
    return True, ""


def llm_synth(facts, allowed, score, rec, gemini) -> Optional[dict]:
    if gemini is None:
        return None
    prompt = (_SYNTH_PROMPT.replace("<<FACTS>>", facts)
              .replace("<<REC>>", rec).replace("<<SCORE>>", str(score)))
    try:
        from analyzer import parse_model_json
        obj = parse_model_json(gemini.summarize_findings(prompt))
    except Exception as e:  # noqa: BLE001
        log.warning("adhoc eval synth failed: %s", e)
        return None
    ok, reason = _validate_synth(obj, allowed)
    if not ok:
        log.info("adhoc eval synth rejected (%s) -> deterministic", reason)
        return None
    return obj


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url) or re.search(r"instagram\.com/([\w.\-]+)/", url)
    return "@" + m.group(1) if m else "creator"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _content_hash(idea: dict) -> str:
    basis = "|".join(str(idea.get(k, "")) for k in
                     ("title", "concept", "caption", "script", "notes"))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _eval_id(idea: dict, chash: str) -> str:
    return "AE-" + hashlib.sha1(f"{idea.get('page_id','')}|{chash}".encode()).hexdigest()[:12]


def _det_narrative(idea, profile, conn, refs, rec, score, copyright_safe, why_risk):
    prof_name = profile.get("PROFILE_NAME", "") if profile else "no strong internal match"
    lead = {
        "Shoot": "Shoot it — this maps cleanly to proven Storelli territory.",
        "Revise then shoot": "Worth shooting, but I'd revise before we roll.",
        "Keep as test": "No internal proof yet — run it as a low-cost test.",
        "Needs more info": "I can't judge this yet — it needs more detail.",
        "Do not prioritize": "I wouldn't prioritize this one.",
    }.get(rec, "Here's my read.")
    why = [f"Closest internal proof: {prof_name}." if profile else
           "No strong internal winning profile matches this yet."]
    if conn:
        why.append(f"Maps to our '{conn.get('CONCEPT_NAME','')}' storytelling structure.")
    why.append("Hook is workable but the concept needs a sharper, single painful moment."
               if _is_vague(idea) else "Concept is specific enough to shoot against a clear structure.")
    improve = ["Open on one concrete pain moment in the first 2 seconds and tie it to the product.",
               "Specify 2-3 shootable beats that follow the structure below."]
    structure = conn.get("STORYTELLING_STRUCTURE", "") if conn else \
        "Curiosity Gap → Demo → Pain Reveal → Product Protection Reveal → CTA"
    steal = conn.get("WHAT_TO_STEAL", "") if conn else "the relatable pain/wince moment and demo pacing"
    not_copy = conn.get("WHAT_NOT_TO_COPY", "") if conn else \
        "their caption/script, on-screen branding, and any non-goalkeeper claims"
    my_move = "Rewrite it around one wince → protected replay → CTA, then shoot the test."
    risk = "" if copyright_safe else f"Copyright/footage risk: {why_risk}."
    return {"lead": lead, "why": why, "what_works": ["Relevant to a proven product/ICP."],
            "what_is_weak": ["Concept too broad; story structure not explicit."],
            "how_to_improve": improve, "suggested_story_structure": structure,
            "steal": steal, "not_copy": not_copy, "my_move": my_move, "risk": risk}


# ---------------------------------------------------------------------------
# core evaluation (pure given reads) — Part C
# ---------------------------------------------------------------------------
def evaluate_idea(idea: dict, profiles: list[dict], connections: list[dict],
                  inspiration: list[dict], ideas: list[dict],
                  calendar_ratings: Optional[list[dict]] = None, gemini=None) -> dict:
    item = _as_item(idea)
    text = f"{item['title']} {item['notes']}".strip()
    has_content = len(re.sub(r"[^a-z0-9]", "", text.lower())) >= 8
    copyright_safe, why_risk = copyright_recheck(text)

    # Retrieval (bounded evidence pack).
    profile, fit01 = closest_profile(item, profiles)
    conn, conn_fit = closest_semantic_connection(idea, connections)
    refs = connection_external_refs(conn, inspiration, k=5) or matching_inspiration(item, inspiration, k=5)
    close_idea = closest_rated_idea(item, ideas)
    cal = _closest_calendar(item, calendar_ratings or [])

    internal_urls = ";".join(_split(profile.get("SUPPORTING_VIDEO_URLS")) if profile else [])
    has_internal_evidence = bool(profile) and bool(internal_urls) and internal_evidence_fit(profile, fit01) >= 70

    # Deterministic sub-scores.
    evidence = internal_evidence_fit(profile, fit01)
    semantic = semantic_connection_fit(conn, conn_fit)
    inspiration_score = inspiration_alignment(refs)
    structure = storytelling_structure_fit(idea, conn)
    pf, icf = product_icp_fit(item, profile)
    shoot = shootability(item)
    copyright_safety = 100.0 if copyright_safe else 15.0
    score = evaluation_score(evidence, semantic, inspiration_score, structure, pf, icf,
                             shoot, copyright_safety)
    nov = novelty(item, close_idea)
    rec = recommend(score, idea, has_content, has_internal_evidence, copyright_safe)
    conf = confidence_label(has_internal_evidence, conn, conn_fit)

    # Narrative: deterministic base, optionally enriched by the LLM.
    det = _det_narrative(idea, profile, conn, refs, rec, score, copyright_safe, why_risk)
    facts, allowed, _ = _facts(idea, profile, conn, refs, close_idea, cal, score, rec)
    synth = llm_synth(facts, allowed, score, rec, gemini)

    def pick(key, default):
        if synth and synth.get(key):
            return synth[key]
        return default

    why = pick("why", det["why"])
    what_works = pick("what_works", det["what_works"])
    what_weak = pick("what_is_weak", det["what_is_weak"])
    improve = pick("how_to_improve", det["how_to_improve"])
    structure_text = str(pick("suggested_story_structure", det["suggested_story_structure"]))
    lead = str(pick("lead", det["lead"]))
    my_move = str(pick("my_move", det["my_move"]))

    # Which reference videos to call out (LLM may re-rank within the pack).
    videos = _videos_payload(synth, refs, det)
    external_urls = ";".join(str(r.get("POST_URL", "")).strip() for r in refs if r.get("POST_URL"))

    chash = _content_hash(idea)
    return {
        "EVALUATION_ID": _eval_id(idea, chash), "EVALUATED_AT": _now_iso(),
        "SOURCE_TYPE": idea.get("source_type", "notion_page"),
        "SOURCE_URL": idea.get("page_url", ""), "NOTION_PAGE_ID": idea.get("page_id", ""),
        "NOTION_PAGE_TITLE": idea.get("title", ""), "IDEA_TITLE": idea.get("title", ""),
        "PRODUCT": idea.get("product", ""), "ICP": idea.get("icp", ""),
        "PLATFORM": idea.get("platform", ""), "FORMAT": idea.get("format", ""),
        "HOOK": idea.get("hook", ""), "CONCEPT_TEXT": str(idea.get("concept", ""))[:1500],
        "NORMALIZED_IDEA_TEXT": str(idea.get("raw_text", ""))[:2000],
        "CLOSEST_WINNING_PROFILE_ID": profile.get("PROFILE_ID", "") if profile else "",
        "CLOSEST_WINNING_PROFILE_NAME": profile.get("PROFILE_NAME", "") if profile else "",
        "CLOSEST_SEMANTIC_CONNECTION_ID": conn.get("CONNECTION_ID", "") if conn else "",
        "CLOSEST_SEMANTIC_CONNECTION_NAME": conn.get("CONCEPT_NAME", "") if conn else "",
        "CLOSEST_RATED_IDEA_ID": close_idea.get("IDEA_ID", "") if close_idea else "",
        "CLOSEST_RATED_IDEA_TITLE": (close_idea.get("REFINED_IDEA_TITLE")
                                     or close_idea.get("IDEA_TITLE", "")) if close_idea else "",
        "INTERNAL_EVIDENCE_URLS": internal_urls, "EXTERNAL_REFERENCE_URLS": external_urls,
        "IDEA_EVALUATION_SCORE": score, "INTERNAL_EVIDENCE_FIT_SCORE": evidence,
        "SEMANTIC_CONNECTION_FIT_SCORE": semantic, "INSPIRATION_ALIGNMENT_SCORE": inspiration_score,
        "STORYTELLING_STRUCTURE_SCORE": structure, "PRODUCT_FIT_SCORE": pf, "ICP_FIT_SCORE": icf,
        "HOOK_STRENGTH_SCORE": hook_strength(item), "SHOOTABILITY_SCORE": shoot,
        "NOVELTY_SCORE": nov, "COPYRIGHT_SAFETY_SCORE": copyright_safety,
        "RECOMMENDATION": rec, "CONFIDENCE": conf,
        "WHY": _join(why), "WHAT_WORKS": _join(what_works), "WHAT_IS_WEAK": _join(what_weak),
        "HOW_TO_IMPROVE": _join(improve), "SUGGESTED_STORY_STRUCTURE": structure_text[:400],
        "VIDEOS_TO_TAKE_INSPO_FROM": _videos_text(videos), "WHAT_TO_STEAL": str(det["steal"])[:400],
        "WHAT_NOT_TO_COPY": str(det["not_copy"])[:400], "RISK_NOTES": (det["risk"] or "No copyright/famous-player risk detected.")[:400],
        "REVIEW_STATUS": "Evaluated", "CONTENT_HASH": chash,
        # transient (not persisted) — used by the Slack renderer:
        "_lead": lead, "_my_move": my_move, "_videos": videos,
        "_s_url": (_split(profile.get("SUPPORTING_VIDEO_URLS"))[:1] if profile else []),
        "_conn": conn, "_close_idea": close_idea, "_why": why, "_improve": improve,
    }


def _closest_calendar(item: dict, ratings: list[dict]) -> Optional[dict]:
    fam = _family(f"{item.get('title','')} {item.get('notes','')} {item.get('product','')}")
    pool = [r for r in ratings if str(r.get("SHOULD_RATE", "")).upper() == "TRUE"
            and (not fam or _family(r.get("PRODUCT", "")) == fam)]
    if not pool:
        return None
    toks = _tokens(f"{item.get('title','')} {item.get('notes','')}")
    return max(pool, key=lambda r: len(toks & _tokens(r.get("CALENDAR_TITLE", ""))))


def _videos_payload(synth, refs, det) -> list[dict]:
    by_eid = {f"E{i}": r for i, r in enumerate(refs, 1)}
    out = []
    if synth and synth.get("videos_to_take_inspo_from"):
        for v in synth["videos_to_take_inspo_from"][:3]:
            r = by_eid.get(str(v.get("source_id", "")).strip())
            if not r:
                continue
            out.append({"url": str(r.get("POST_URL", "")), "creator": _handle(str(r.get("POST_URL", ""))),
                        "why": str(v.get("why", ""))[:120],
                        "steal": str(v.get("what_to_steal", "")) or str(det["steal"]),
                        "not_copy": str(v.get("what_not_to_copy", "")) or str(det["not_copy"])})
    if not out:
        for r in refs[:3]:
            out.append({"url": str(r.get("POST_URL", "")), "creator": _handle(str(r.get("POST_URL", ""))),
                        "why": str(r.get("CREATIVE_MECHANISM", ""))[:80],
                        "steal": str(det["steal"]), "not_copy": str(det["not_copy"])})
    return out


def _join(items) -> str:
    if isinstance(items, str):
        return items[:900]
    return " | ".join(str(x) for x in (items or []))[:900]


def _videos_text(videos) -> str:
    return " | ".join(f"{v['creator']}: steal {v['steal']}" for v in (videos or []))[:900]


# ---------------------------------------------------------------------------
# Slack render (CEO style) — Part E
# ---------------------------------------------------------------------------
def _short(text: str, n: int = 16) -> str:
    s = re.split(r"(?<=[.!?])\s+", str(text or "").strip())[0].strip().rstrip(".")
    w = s.split()
    return (" ".join(w[:n]) + ("…" if len(w) > n else "")) if w else ""


def render_evaluation(ev: dict, text: str = "") -> str:
    mode = st.detect_response_mode(text) if text else st.MODE_DEFAULT
    score = ev.get("IDEA_EVALUATION_SCORE")
    rec, conf = ev.get("RECOMMENDATION", ""), ev.get("CONFIDENCE", "")
    lead = f"{ev.get('_lead','')}\n\nScore: {int(round(_num(score)))}/100 — {conf} confidence."

    why = [_short(w, 18) for w in (ev.get("_why") or [])][:3]
    fixes = ev.get("_improve") or []
    if fixes:
        why.append(f"Fix: {_short(fixes[0], 20)}")
    videos = ev.get("_videos") or []
    for i, v in enumerate(videos[:2], 1):
        why.append(f"Inspo [E{i}] {v['creator']} — steal {_short(v['steal'], 10)}; "
                   f"don't copy {_short(v['not_copy'], 8)}")

    move = f"Structure: {_short(ev.get('SUGGESTED_STORY_STRUCTURE',''), 20)}. {_short(ev.get('_my_move',''), 16)}"

    # Sources: [N1] notion, [S1] internal proof, [C1] connection, [E#] externals, [I1] similar idea.
    rows = [("N1", ev.get("SOURCE_URL", ""), f"Notion idea — {ev.get('IDEA_TITLE','')[:40]}")]
    s_url = ev.get("_s_url") or []
    if s_url:
        rows.append(("S1", s_url[0], f"Storelli internal proof — {ev.get('CLOSEST_WINNING_PROFILE_NAME','')}"))
    conn = ev.get("_conn")
    if conn and conn.get("INTERNAL_EVIDENCE_URLS"):
        rows.append(("C1", _split(conn.get("EXTERNAL_REFERENCE_URLS"))[:1][0] if _split(conn.get("EXTERNAL_REFERENCE_URLS")) else "",
                     f"Semantic connection — {conn.get('CONCEPT_NAME','')[:40]}"))
    for i, v in enumerate(videos[:2], 1):
        rows.append((f"E{i}", v["url"], f"External reference — {v['creator']}"))
    close = ev.get("_close_idea")
    if close:
        rows.append(("I1", "", f"Similar rated idea — {(close.get('REFINED_IDEA_TITLE') or close.get('IDEA_TITLE',''))[:40]}"))
    sources = st.compact_sources([(t, u, l) for (t, u, l) in rows if u])

    return st.render_ceo_summary(lead, why=why, move=move,
                                 sources=(f"{sources}\n{_NOT_PROOF}" if sources else ""), mode=mode)


# ---------------------------------------------------------------------------
# Slack routing + orchestrator (Part E) — read-only
# ---------------------------------------------------------------------------
_EVAL_LANG = ("evaluate", "worth shooting", "worth it", "score this", "score it", "rate this",
              "how would you improve", "how to improve", "improve this", "improve it",
              "compare this", "assess", "review this", "is this good", "should we shoot",
              "thoughts on this", "what do you think", "grade this")
_FOLLOWUP = ("why", "how do we improve", "how would you improve", "improve it", "improve this",
             "what videos", "what inspiration", "which videos", "make it more", "what to fix",
             "how to fix", "what's weak", "whats weak")


def is_evaluation_query(text: str, context: Optional[list] = None) -> bool:
    import notion_idea_ingest as ni
    t = (text or "").lower()
    if ni.find_notion_url(text):
        # A Notion URL + eval language, or basically just a pasted page.
        if any(k in t for k in _EVAL_LANG):
            return True
        stripped = ni.NOTION_URL_RE.sub("", text).strip()
        return len(re.sub(r"[^a-z0-9]", "", stripped.lower())) <= 6   # bare paste
    # Follow-up on a prior evaluation (resolve "that Notion idea" from memory).
    if _prior_eval_url(context) and any(k in t for k in _FOLLOWUP):
        return True
    return False


def _prior_eval_url(context: Optional[list]) -> str:
    import notion_idea_ingest as ni
    for m in reversed(context or []):
        url = ni.find_notion_url(m.get("text", ""))
        if url:
            return url
    return ""


def answer_evaluation(text: str, context: Optional[list] = None, sheets=None,
                      gemini="auto") -> Optional[str]:
    """Ingest a Notion idea (from the message or prior turn), evaluate it against
    the brain, persist to ADHOC_IDEA_EVALUATIONS, and return a CEO Slack answer.
    Read-only w.r.t. Notion and all internal rows."""
    import notion_idea_ingest as ni
    url = ni.find_notion_url(text) or _prior_eval_url(context)
    if not url:
        return None
    idea, err = ni.ingest(url)
    if err:
        return err
    if gemini == "auto":
        try:
            from gemini_client import GeminiClient
            gemini = GeminiClient()
        except Exception:  # noqa: BLE001
            gemini = None
    try:
        s = sheets or InspirationSheets()
        profiles = s.read_profiles()
        connections = s.read_semantic_connections()
        inspiration = s.read_content_rows()
        ideas = s.read_ideas()
        cal = s.read_calendar_ratings()
    except Exception as e:  # noqa: BLE001
        log.warning("answer_evaluation load failed: %s", e)
        return None

    ev = evaluate_idea(idea, profiles, connections, inspiration, ideas, cal, gemini=gemini)
    persist = {k: v for k, v in ev.items() if not k.startswith("_")}
    try:
        s.upsert_adhoc_evaluations([persist])
    except Exception as e:  # noqa: BLE001 - Slack answer still returns
        log.warning("adhoc evaluation persist failed: %s", e)
    return render_evaluation(ev, text)


# ---------------------------------------------------------------------------
# CLI entry (Part F)
# ---------------------------------------------------------------------------
def evaluate_notion_url(url: str, sheets: Optional[InspirationSheets] = None,
                        gemini="auto") -> dict:
    """CLI/dashboard entry: ingest + evaluate + persist a single Notion URL.
    Returns the (persisted) evaluation dict, or {"error": ...}."""
    import notion_idea_ingest as ni
    idea, err = ni.ingest(url)
    if err:
        return {"error": err}
    if gemini == "auto":
        try:
            from gemini_client import GeminiClient
            gemini = GeminiClient()
        except Exception:  # noqa: BLE001
            gemini = None
    s = sheets or InspirationSheets()
    try:
        s.ensure_adhoc_evaluations_tab()
    except Exception as e:  # noqa: BLE001
        log.warning("ensure adhoc tab failed (continuing): %s", e)
    ev = evaluate_idea(idea, s.read_profiles(), s.read_semantic_connections(),
                       s.read_content_rows(), s.read_ideas(), s.read_calendar_ratings(),
                       gemini=gemini)
    persist = {k: v for k, v in ev.items() if not k.startswith("_")}
    created, updated = s.upsert_adhoc_evaluations([persist])
    persist["_created"], persist["_updated"] = created, updated
    persist["_render"] = render_evaluation(ev)
    return persist
