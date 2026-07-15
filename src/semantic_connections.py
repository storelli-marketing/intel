"""Semantic Connection Layer.

Links internal Storelli evidence (winning profiles / learnings — PROOF) to
external inspiration videos (execution reference ONLY, never proof) through a
STORYTELLING STRUCTURE, with adaptation + safety notes. Builds rows in the
SEMANTIC_CONNECTIONS tab, and answers Slack "what videos should we take
inspiration from" questions with specific external videos (not the idea list).

Rules: external inspiration never raises evidence fit (evidence fit comes from
internal winning profiles only). Never treat external content as proof. No
famous-player/match/broadcast references. Read-only w.r.t. internal Storelli
rows; writes only to SEMANTIC_CONNECTIONS.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

import slack_response_style as st
from inspiration_scanner import _finalize_and_log_run, _new_run
from inspiration_sheets import InspirationSheets
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External = execution reference only, not proof it works for Storelli._"

# Part B — storytelling structures keyed by dominant hook archetype.
STORYTELLING_STRUCTURES = {
    "curiosity gap": "Curiosity Gap → Demo → Pain Reveal → Product Protection Reveal → CTA",
    "fear / risk": "Fear/Risk → Story-Demo → Pain Moment → Protected Replay → CTA",
    "education": "Education → Tutorial → Mistake → Correction → Product Reason → CTA",
    "do / don't": "Do/Don't → Contrast Demo → Wrong Gear vs Right Gear → CTA",
    "aspiration": "POV → Wince Moment → Confidence Shift → Protection Proof → CTA",
    "authority": "Authority → Explainer → Protection Logic → Confidence → CTA",
}
_DEFAULT_STRUCTURE = STORYTELLING_STRUCTURES["fear / risk"]

_LEGGINGS = ("bodyshield", "leggings", "pants", "sliders", "leg")
_GLOVES = ("glove",)
_QUALITY_MIN = 80.0


def _num(v, default=0.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _family(text: str) -> str:
    t = str(text or "").lower()
    if any(k in t for k in _LEGGINGS):
        return "leggings"
    if any(k in t for k in _GLOVES):
        return "gloves"
    return ""


def _first_tag(cell: str) -> str:
    return next((x.strip() for x in str(cell or "").split(",") if x.strip()), "")


def structure_for(hook_tags: str) -> str:
    for h in (x.strip().lower() for x in str(hook_tags or "").split(",")):
        if h in STORYTELLING_STRUCTURES:
            return STORYTELLING_STRUCTURES[h]
    return _DEFAULT_STRUCTURE


def _split(cell) -> list[str]:
    return [u.strip() for u in str(cell or "").split(";") if u.strip()]


def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url) or re.search(r"instagram\.com/([\w.\-]+)/", url)
    return "@" + m.group(1) if m else "creator"


# ---------------------------------------------------------------------------
# concept candidates (Part C step 1)
# ---------------------------------------------------------------------------
def concept_candidates(profiles: list[dict], ideas: list[dict],
                       products: Optional[list] = None, cap: int = 10) -> list[dict]:
    actives = [p for p in profiles
               if str(p.get("ACTIVE", "")).strip().lower() == "true"
               and str(p.get("CONFIDENCE", "")).strip().lower() in ("medium", "high")]
    fams = {_family(p) for p in (products or [])} if products else None

    concepts = []
    for p in actives:
        if fams and _family(p.get("PRODUCT", "")) not in fams:
            continue
        hook = _first_tag(p.get("HOOK_TAGS")) or "Fear / Risk"
        concepts.append(_concept_from_profile(p, hook))
    # Idea-anchored concepts (named after refined ideas) tied to their profile.
    byid = {p.get("PROFILE_ID"): p for p in actives}
    seen = {c["CONCEPT_KEY"] for c in concepts}
    for idea in sorted(ideas, key=lambda i: _num(i.get("IDEA_SCORE")), reverse=True):
        prof = byid.get(str(idea.get("SOURCE_PROFILE_ID", "")).strip())
        if not prof:
            continue
        if fams and _family(prof.get("PRODUCT", "")) not in fams:
            continue
        title = (idea.get("REFINED_IDEA_TITLE") or idea.get("IDEA_TITLE") or "").strip()
        key = "CX-" + _slug(title)
        if not title or key in seen:
            continue
        c = _concept_from_profile(prof, _first_tag(prof.get("HOOK_TAGS")) or "Fear / Risk")
        c.update({"CONCEPT_KEY": key, "CONCEPT_NAME": title})
        concepts.append(c)
        seen.add(key)
    return concepts[:cap]


def _concept_from_profile(p: dict, hook: str) -> dict:
    product = str(p.get("PRODUCT", "")).strip()
    return {
        "CONCEPT_KEY": "CP-" + _slug(product) + "-" + _slug(hook),
        "CONCEPT_NAME": f"{product} — {hook} concept",
        "PRODUCT": product, "ICP": str(p.get("ICP", "")).strip(),
        "HOOK_ARCHETYPE": hook, "FORMAT_ARCHETYPE": _first_tag(p.get("FORMAT_TAGS")),
        "PROBLEM_TYPE": _first_tag(p.get("PROBLEM_TAGS")),
        "SOLUTION_TYPE": _first_tag(p.get("SOLUTION_TAGS")),
        "FUNNEL_STAGE": _first_tag(p.get("FUNNEL_STAGE_TAGS")),
        "STORYTELLING_STRUCTURE": structure_for(p.get("HOOK_TAGS")),
        "WINNING_PROFILE_ID": p.get("PROFILE_ID", ""),
        "WINNING_PROFILE_NAME": p.get("PROFILE_NAME", ""),
        "INTERNAL_EVIDENCE_URLS": p.get("SUPPORTING_VIDEO_URLS", ""),
        "INTERNAL_LEARNING_IDS": p.get("SUPPORTING_LEARNING_IDS", ""),
        "INTERNAL_LEARNING_SUMMARY": (
            f"{hook} + {_first_tag(p.get('FORMAT_TAGS'))} performs for "
            f"{product} / {p.get('ICP', '')} (internal sample {p.get('INTERNAL_SAMPLE_SIZE', '?')}, "
            f"confidence {p.get('CONFIDENCE', '?')})."),
    }


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")[:40]


# ---------------------------------------------------------------------------
# shortlist external inspiration (Part C step 2) — never blind pairing
# ---------------------------------------------------------------------------
_THEME_KW = {
    "turf": ("turf", "burn", "scrape", "slide"), "dive": ("dive", "diving", "landing"),
    "fear": ("fear", "afraid", "confidence", "hesitat"), "grip": ("grip", "catch", "glove"),
    "protection": ("protect", "pad", "guard", "injury", "prevent"),
}


def _concept_terms(concept: dict) -> set:
    blob = " ".join(str(concept.get(k, "")) for k in
                    ("CONCEPT_NAME", "PRODUCT", "HOOK_ARCHETYPE", "PROBLEM_TYPE")).lower()
    terms = set(re.findall(r"[a-z]{4,}", blob))
    for kws in _THEME_KW.values():
        terms.update(k for k in kws if any(k in blob for k in kws))
    return terms


def eligible_inspiration(rows: list[dict]) -> list[dict]:
    return [r for r in rows
            if str(r.get("SAFETY_STATUS", "")).strip().lower() == "safe"
            and str(r.get("ANALYSIS_STATUS", "")).strip().lower() == "analyzed"
            and str(r.get("USE_FOR_IDEA_GEN", "")).strip().upper() == "TRUE"
            and _num(r.get("INSPIRATION_QUALITY_SCORE")) >= _QUALITY_MIN]


def shortlist(concept: dict, inspiration: list[dict], k: int = 4) -> list[dict]:
    fam = _family(concept.get("PRODUCT", ""))
    terms = _concept_terms(concept)
    scored = []
    for r in eligible_inspiration(inspiration):
        rfam = _family(str(r.get("BEST_MATCHED_PROFILE_NAME", "")) or str(r.get("SUBCATEGORY", "")))
        text = " ".join(str(r.get(c, "")) for c in
                        ("CAPTION", "CREATIVE_MECHANISM", "HANDLE", "HOOK_TAGS", "PROBLEM_TAGS")).lower()
        overlap = len(terms & set(re.findall(r"[a-z]{4,}", text)))
        fam_bonus = 2 if (fam and rfam == fam) else 0
        score = fam_bonus + overlap + _num(r.get("INSPIRATION_QUALITY_SCORE")) / 100
        scored.append((score, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:k]]


# ---------------------------------------------------------------------------
# deterministic connection composition (reused for the sheet + Slack fallback)
# ---------------------------------------------------------------------------
def _steal_for(concept: dict) -> str:
    problem = str(concept.get("PROBLEM_TYPE", "")).lower()
    fmt = str(concept.get("FORMAT_ARCHETYPE", "")).lower()
    bits = []
    if "pain" in problem or "acute" in problem or "chronic" in problem:
        bits.append("the relatable pain/wince moment")
    if "demo" in fmt or "tutorial" in fmt:
        bits.append("the demo pacing and clear beats")
    bits.append("the visual rhythm and hook framing")
    return "; ".join(bits[:3])


def _adaptation_for(concept: dict) -> str:
    return (f"Adapt the structure onto {concept.get('PRODUCT', 'the product')}: "
            f"{concept.get('STORYTELLING_STRUCTURE', _DEFAULT_STRUCTURE)}.")


_NOT_COPY = ("their caption/script, personal story, on-screen branding, and any "
             "non-goalkeeper or non-sports claims")
_RISK_NOTE = ("Reference only — do not reuse their footage, audio, or captions; no famous "
              "players, match clips, or broadcast footage.")


def _det_connection(concept: dict, externals: list[dict]) -> dict:
    creators = [(_handle(str(r.get("POST_URL", ""))) or "creator") for r in externals]
    return {
        "why_this_connection": (
            f"These external videos show the same creative mechanism "
            f"({concept.get('HOOK_ARCHETYPE', '')} → {concept.get('FORMAT_ARCHETYPE', '')}) that our "
            f"internal winning profile proves works for {concept.get('PRODUCT', '')}."),
        "what_to_steal": _steal_for(concept),
        "what_not_to_copy": _NOT_COPY,
        "storelli_adaptation": _adaptation_for(concept),
        "shooting_notes": " | ".join(concept.get("STORYTELLING_STRUCTURE", _DEFAULT_STRUCTURE).split(" → ")),
        "copyright_risk_notes": _RISK_NOTE,
        "creators": ", ".join(creators),
    }


# ---------------------------------------------------------------------------
# scoring (evidence fit = internal only)
# ---------------------------------------------------------------------------
def _evidence_fit(concept: dict) -> float:
    # From the internal winning profile only — never external.
    conf = str(concept.get("WINNING_PROFILE_NAME", "")).lower()
    base = 92.0 if "high" in _profile_conf(concept) else 80.0
    n_evi = len(_split(concept.get("INTERNAL_EVIDENCE_URLS")))
    return round(min(100.0, base + min(6.0, n_evi)), 1)


def _profile_conf(concept: dict) -> str:
    return str(concept.get("_CONFIDENCE", "")).lower()


def connection_score(evidence, story, inspiration, adaptation, safety) -> float:
    return round(0.30 * evidence + 0.25 * story + 0.20 * inspiration
                 + 0.15 * adaptation + 0.10 * safety, 1)


# ---------------------------------------------------------------------------
# LLM semantic judge (Part D) — validated, deterministic fallback
# ---------------------------------------------------------------------------
_JUDGE_PROMPT = (
    "You are Storelli's senior creative strategist. Connect internal Storelli evidence with "
    "external inspiration videos through a storytelling structure. Internal Storelli evidence is "
    "PROOF. External inspiration is ONLY an execution reference (never proof; never say its views "
    "prove anything). Use ONLY the supplied evidence. Do not invent URLs, creators, scores, or "
    "claims. No famous players, match/broadcast footage, or fan edits.\n\n"
    "Evidence pack:\n<<FACTS>>\n\n"
    "Return ONLY strict JSON: {\"concept_name\": str, \"storytelling_structure\": str, "
    "\"why_this_connection\": str, \"what_to_steal\": [str], \"what_not_to_copy\": [str], "
    "\"storelli_adaptation\": str, \"shooting_notes\": [str], \"copyright_risk_notes\": str, "
    "\"scores\": {\"evidence_fit\": 0, \"story_structure_fit\": 0, \"inspiration_fit\": 0, "
    "\"adaptation_clarity\": 0, \"safety\": 0}}")

_UNSAFE = ("messi", "ronaldo", "neymar", "mbappe", "champions league", "premier league",
           "world cup", "match highlights", "broadcast", "fan edit", "full match")


def _facts(concept: dict, externals: list[dict]) -> tuple:
    lines = [f"Concept: {concept.get('CONCEPT_NAME')}",
             f"Product/ICP: {concept.get('PRODUCT')}/{concept.get('ICP')}",
             f"Storytelling structure: {concept.get('STORYTELLING_STRUCTURE')}",
             f"Internal winning profile (PROOF): {concept.get('WINNING_PROFILE_NAME')}",
             f"Internal learning: {concept.get('INTERNAL_LEARNING_SUMMARY')}"]
    s_urls = _split(concept.get("INTERNAL_EVIDENCE_URLS"))[:3]
    for i, u in enumerate(s_urls, 1):
        lines.append(f"[S{i}] Storelli internal proof: {u}")
    e_ids, creators = [], []
    for i, r in enumerate(externals, 1):
        u = str(r.get("POST_URL", "")).strip()
        e_ids.append((f"E{i}", u))
        creators.append(_handle(u))
        lines.append(f"[E{i}] External inspiration (reference only) {_handle(u)}: "
                     f"mechanism '{str(r.get('CREATIVE_MECHANISM', '')).strip()}', "
                     f"caption '{str(r.get('CAPTION', ''))[:80]}'")
    allowed = {f"S{i}" for i in range(1, len(s_urls) + 1)} | {eid for eid, _ in e_ids}
    return "\n".join(lines), allowed, s_urls, e_ids


def _validate_judge(obj, allowed: set, s_urls: list, e_ids: list) -> tuple:
    if not isinstance(obj, dict):
        return False, "not dict"
    if not str(obj.get("storytelling_structure", "")).strip():
        return False, "no structure"
    if not s_urls:
        return False, "no internal evidence url"
    if not e_ids:
        return False, "no external reference url"
    text = " ".join([str(obj.get("why_this_connection", "")), str(obj.get("storelli_adaptation", "")),
                     " ".join(map(str, obj.get("what_to_steal", []) or [])),
                     " ".join(map(str, obj.get("shooting_notes", []) or []))]).lower()
    if re.search(r"(external|inspiration)[^.]{0,40}\bprov(e|es|en|ing)\b", text):
        return False, "external as proof"
    if any(u in text for u in _UNSAFE):
        return False, "unsafe reference"
    return True, ""


def llm_judge(concept: dict, externals: list[dict], gemini) -> Optional[dict]:
    if gemini is None:
        return None
    facts, allowed, s_urls, e_ids = _facts(concept, externals)
    try:
        from analyzer import parse_model_json
        obj = parse_model_json(gemini.summarize_findings(_JUDGE_PROMPT.replace("<<FACTS>>", facts)))
    except Exception as e:  # noqa: BLE001
        log.warning("semantic judge failed: %s", e)
        return None
    ok, reason = _validate_judge(obj, allowed, s_urls, e_ids)
    if not ok:
        log.info("semantic judge rejected (%s) -> deterministic", reason)
        return None
    return obj


# ---------------------------------------------------------------------------
# build connection row + orchestrator (Part C)
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _connection_id(concept: dict, externals: list[dict]) -> str:
    # Stable per CONCEPT (not per external-reference set) so a refreshed pool
    # UPDATES the existing connection row in place instead of creating a
    # duplicate — the upsert is keyed by CONNECTION_ID. `externals` is kept in
    # the signature for call-site compatibility but no longer part of the id.
    return "SC-" + hashlib.sha1(str(concept.get("CONCEPT_KEY", "")).encode()).hexdigest()[:12]


def build_connection_row(concept: dict, externals: list[dict], profile_conf: str,
                         gemini=None) -> Optional[dict]:
    if not externals or not _split(concept.get("INTERNAL_EVIDENCE_URLS")):
        return None   # no external OR no internal evidence -> no connection
    det = _det_connection(concept, externals)
    judged = llm_judge(concept, externals, gemini)
    concept = {**concept, "_CONFIDENCE": profile_conf}

    # Scores are deterministic + reproducible (the LLM only enriches the
    # narrative). Evidence fit = internal winning profile only; inspiration fit =
    # external quality; structure/adaptation are structural; safety drops if any
    # external caption trips a risk term.
    evidence = _evidence_fit(concept)
    story = 85.0
    adaptation = 85.0
    inspiration = round(sum(_num(r.get("INSPIRATION_QUALITY_SCORE")) for r in externals) / len(externals), 1)
    blob = " ".join(str(r.get("CAPTION", "")) for r in externals).lower()
    safety = 60.0 if any(u in blob for u in _UNSAFE) else 95.0

    if judged:
        structure = str(judged.get("storytelling_structure") or concept["STORYTELLING_STRUCTURE"])
        why = str(judged.get("why_this_connection") or det["why_this_connection"])
        steal = "; ".join(map(str, judged.get("what_to_steal", []) or [])) or det["what_to_steal"]
        not_copy = "; ".join(map(str, judged.get("what_not_to_copy", []) or [])) or det["what_not_to_copy"]
        adapt = str(judged.get("storelli_adaptation") or det["storelli_adaptation"])
        shooting = " | ".join(map(str, judged.get("shooting_notes", []) or [])) or det["shooting_notes"]
        risk = str(judged.get("copyright_risk_notes") or det["copyright_risk_notes"])
    else:
        structure = concept["STORYTELLING_STRUCTURE"]
        why, steal, not_copy = det["why_this_connection"], det["what_to_steal"], det["what_not_to_copy"]
        adapt, shooting, risk = det["storelli_adaptation"], det["shooting_notes"], det["copyright_risk_notes"]

    score = connection_score(evidence, story, inspiration, adaptation, safety)
    return {
        "CONNECTION_ID": _connection_id(concept, externals), "CREATED_AT": _now_iso(),
        "CONCEPT_KEY": concept["CONCEPT_KEY"], "CONCEPT_NAME": concept["CONCEPT_NAME"],
        "PRODUCT": concept["PRODUCT"], "ICP": concept["ICP"],
        "STORYTELLING_STRUCTURE": structure, "HOOK_ARCHETYPE": concept["HOOK_ARCHETYPE"],
        "FORMAT_ARCHETYPE": concept["FORMAT_ARCHETYPE"], "PROBLEM_TYPE": concept["PROBLEM_TYPE"],
        "SOLUTION_TYPE": concept["SOLUTION_TYPE"], "FUNNEL_STAGE": concept["FUNNEL_STAGE"],
        "INTERNAL_LEARNING_IDS": concept["INTERNAL_LEARNING_IDS"],
        "INTERNAL_LEARNING_SUMMARY": concept["INTERNAL_LEARNING_SUMMARY"],
        "WINNING_PROFILE_ID": concept["WINNING_PROFILE_ID"],
        "WINNING_PROFILE_NAME": concept["WINNING_PROFILE_NAME"],
        "INTERNAL_EVIDENCE_URLS": concept["INTERNAL_EVIDENCE_URLS"],
        "EXTERNAL_CONTENT_IDS": ";".join(str(r.get("SOURCE_ID", "")).strip() for r in externals),
        "EXTERNAL_REFERENCE_URLS": ";".join(str(r.get("POST_URL", "")).strip() for r in externals),
        "EXTERNAL_CREATORS": det["creators"],
        "WHY_THIS_CONNECTION": why[:900], "WHAT_TO_STEAL": steal[:600],
        "WHAT_NOT_TO_COPY": not_copy[:600], "STORELLI_ADAPTATION": adapt[:600],
        "SHOOTING_NOTES": shooting[:600], "COPYRIGHT_RISK_NOTES": risk[:400],
        "CONNECTION_SCORE": score, "EVIDENCE_FIT_SCORE": evidence,
        "STORY_STRUCTURE_FIT_SCORE": story, "INSPIRATION_FIT_SCORE": inspiration,
        "ADAPTATION_CLARITY_SCORE": adaptation, "SAFETY_SCORE": safety,
        "REVIEW_STATUS": "Built",
    }


def _clamp(v, default):
    n = _num(v, None)
    if n is None:
        return round(float(default), 1)
    return round(max(0.0, min(100.0, n)), 1)


def build_semantic_connections(sheets: Optional[InspirationSheets] = None, gemini="auto",
                               products: Optional[list] = None, max_concepts: int = 10) -> dict:
    sheets = sheets or InspirationSheets()
    try:
        sheets.ensure_semantic_connections_tab()
    except Exception as e:  # noqa: BLE001
        log.warning("ensure semantic tab failed (continuing): %s", e)
    if gemini == "auto":
        try:
            from gemini_client import GeminiClient
            gemini = GeminiClient()
        except Exception:  # noqa: BLE001
            gemini = None

    profiles = sheets.read_profiles()
    ideas = sheets.read_ideas()
    inspiration = sheets.read_content_rows()
    conf_by_id = {p.get("PROFILE_ID"): str(p.get("CONFIDENCE", "")) for p in profiles}

    concepts = concept_candidates(profiles, ideas, products=products, cap=max_concepts)
    run = _new_run("SemanticConnections", "semantic-judge")
    run["POSTS_DISCOVERED"] = len(concepts)
    rows, weak = [], []
    for c in concepts:
        ext = shortlist(c, inspiration, k=4)
        row = build_connection_row(c, ext, conf_by_id.get(c.get("WINNING_PROFILE_ID"), ""), gemini)
        if row:
            rows.append(row)
        else:
            weak.append(c["CONCEPT_NAME"])

    errors: list[str] = []
    created = updated = 0
    try:
        created, updated = sheets.upsert_semantic_connections(rows)
    except Exception as e:  # noqa: BLE001
        errors.append(f"upsert failed: {e}")
    run["POSTS_ADDED"] = created
    run["POSTS_ANALYZED"] = len(rows)
    run["POSTS_SKIPPED_EXISTING"] = len(weak)
    run["_connections"] = rows
    run["_weak"] = weak
    log.info("Semantic connections: %d built (%d created, %d updated), %d weak/missing",
             len(rows), created, updated, len(weak))
    return _finalize_and_log_run(sheets, run, errors, failed=len(errors), total=1)


def print_connections_summary(run: dict) -> None:
    rows = run.get("_connections", [])
    print("\nSemantic connections built.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Concepts considered:    {run.get('POSTS_DISCOVERED')}")
    print(f"Connections built:      {run.get('POSTS_ANALYZED')}")
    print(f"Weak/missing:           {run.get('POSTS_SKIPPED_EXISTING')} {run.get('_weak', [])}")
    for r in sorted(rows, key=lambda x: _num(x.get("CONNECTION_SCORE")), reverse=True)[:10]:
        print(f"  [{r['CONNECTION_SCORE']}] {r['CONCEPT_NAME'][:44]} <- {r['EXTERNAL_CREATORS']}")


# ---------------------------------------------------------------------------
# Slack answer (Parts E/F) — read-only; returns external VIDEOS, not the idea list
# ---------------------------------------------------------------------------
def is_inspiration_query(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    if "inspo" in t:
        return True
    if "inspiration" in t and any(k in t for k in ("video", "content", "clip", "for ",
                                                   "concept", "from", "reference")):
        return True
    if "video" in t and any(k in t for k in ("take inspiration", "good to take", "map to",
                                             "maps to", "use as", "should we use", "support the")):
        return True
    if "storytelling structure" in t or "story structure" in t:
        return True
    if "adapt" in t and any(k in t for k in ("this", " it ", "without copying", "safely")):
        return True
    if "why is this video" in t or "why this video" in t:
        return True
    return False


def _resolve_family(text: str, context: Optional[list]) -> str:
    fam = _family(text)
    if fam:
        return fam
    # "related to the ideas you proposed" -> product from the prior turn.
    last = next((m.get("text", "") for m in reversed(context or [])
                 if m.get("role") == "assistant"), "")
    return _family(last)


def answer_inspiration(text: str, context: Optional[list] = None, sheets=None,
                       gemini="auto") -> Optional[str]:
    try:
        s = sheets or InspirationSheets()
        connections = s.read_semantic_connections()
        profiles = s.read_profiles()
        inspiration = s.read_content_rows()
        ideas = s.read_ideas()
    except Exception as e:  # noqa: BLE001
        log.warning("answer_inspiration load failed: %s", e)
        return None

    fam = _resolve_family(text, context)
    tl = (text or "").lower()
    # 1) prefer a stored connection matching family + theme.
    conns = [c for c in connections if not fam or _family(c.get("PRODUCT", "")) == fam]
    conn = None
    if conns:
        terms = {k for k in ("turf", "dive", "fear", "grip", "protect", "landing") if k in tl}
        conns.sort(key=lambda c: (len(terms & set(re.findall(r"[a-z]{4,}", str(c.get("CONCEPT_NAME", "")).lower()))),
                                  _num(c.get("CONNECTION_SCORE"))), reverse=True)
        conn = conns[0]

    if conn:
        externals = list(zip(_split(conn.get("EXTERNAL_REFERENCE_URLS")),
                             [x.strip() for x in str(conn.get("EXTERNAL_CREATORS", "")).split(",")]))
        return _render_from_connection(conn, externals, text)

    # 2) on-the-fly (read-only, no write): build a concept + shortlist.
    concepts = concept_candidates(profiles, ideas, products=[fam] if fam else None, cap=3)
    if not concepts:
        concepts = concept_candidates(profiles, ideas, cap=1)
    if not concepts:
        return ("I don't have a winning profile to anchor inspiration to yet — build "
                "profiles + discover inspiration first.")
    concept = concepts[0]
    ext = shortlist(concept, inspiration, k=4)
    if not ext:
        return (f"No safe, high-quality external references matched the {concept.get('PRODUCT', '')} "
                "concept yet — run discovery/quality-review to grow the pool.")
    return _render_onthefly(concept, ext, text)


def _short(text: str, n: int = 14) -> str:
    """First sentence, capped to n words — keeps LLM-enriched narrative from
    blowing past the CEO length budget (which would drop the whole line)."""
    s = re.split(r"(?<=[.!?])\s+", str(text or "").strip())[0].strip().rstrip(".")
    w = s.split()
    return (" ".join(w[:n]) + ("…" if len(w) > n else "")) if w else ""


def _assemble(product, structure, adaptation, steal, not_copy, video_bullets,
              s_row, e_rows, mode) -> str:
    lead = (f"For the {product} concepts, use these {len(video_bullets)} videos as "
            "execution references:")
    # Keep bullets <= 5 AND each line short enough to survive length enforcement:
    # video lines + one steal/skip line (structure -> My move).
    why = video_bullets[:4] + [f"Steal: {_short(steal)} · Don't copy: {_short(not_copy)}."]
    move = f"Structure: {_short(structure, 18)}. {_short(adaptation, 16)}"
    src_rows = ([s_row] if s_row else []) + e_rows
    sources = st.compact_sources(src_rows)
    return st.render_ceo_summary(lead, why=why, move=move,
                                 sources=(f"{sources}\n{_NOT_PROOF}" if sources else ""), mode=mode)


def _render_from_connection(conn: dict, externals: list, text: str) -> str:
    mode = st.detect_response_mode(text)
    s_urls = _split(conn.get("INTERNAL_EVIDENCE_URLS"))[:1]
    bullets, e_rows = [], []
    for i, (url, creator) in enumerate(externals[:3], 1):
        c = creator or _handle(url)
        e_rows.append((f"E{i}", url, f"External inspiration — {c}"))
        bullets.append(f"*{i}. {c}* — maps to the "
                       f"{conn.get('HOOK_ARCHETYPE', '')} → {conn.get('FORMAT_ARCHETYPE', '')} mechanism.")
    s_row = ("S1", s_urls[0], f"Storelli internal proof — {conn.get('WINNING_PROFILE_NAME', '')}") if s_urls else None
    return _assemble(conn.get("PRODUCT", ""), conn.get("STORYTELLING_STRUCTURE", ""),
                     str(conn.get("STORELLI_ADAPTATION", "")) or "Adapt the structure onto the product.",
                     conn.get("WHAT_TO_STEAL", ""), conn.get("WHAT_NOT_TO_COPY", ""),
                     bullets, s_row, e_rows, mode)


def _render_onthefly(concept: dict, ext: list[dict], text: str) -> str:
    mode = st.detect_response_mode(text)
    det = _det_connection(concept, ext)
    s_urls = _split(concept.get("INTERNAL_EVIDENCE_URLS"))[:1]
    bullets, e_rows = [], []
    for i, r in enumerate(ext[:3], 1):
        url = str(r.get("POST_URL", "")).strip()
        creator = _handle(url)
        mech = str(r.get("CREATIVE_MECHANISM", "")).strip() or "on-topic training mechanism"
        e_rows.append((f"E{i}", url, f"External inspiration — {creator}"))
        bullets.append(f"*{i}. {creator}* — {mech}; maps to the "
                       f"{concept.get('HOOK_ARCHETYPE', '')} → {concept.get('FORMAT_ARCHETYPE', '')} pattern.")
    s_row = ("S1", s_urls[0], f"Storelli internal proof — {concept.get('WINNING_PROFILE_NAME', '')}") if s_urls else None
    return _assemble(concept.get("PRODUCT", ""), concept.get("STORYTELLING_STRUCTURE", ""),
                     det["storelli_adaptation"], det["what_to_steal"], det["what_not_to_copy"],
                     bullets, s_row, e_rows, mode)
