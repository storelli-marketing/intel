"""Slack Social Strategist Skill Pack.

Conversational social-media strategy skills that answer practical team questions
using the EXISTING brain (winning profiles, refined ideas, semantic connections,
calendar ratings, ad-hoc evaluations, latest_learnings.md). This is a routing +
evidence-pack + synthesis layer, NOT a new backend: it reads only, never writes
Sheets/Notion, never changes scoring, never generates canonical ideas.

Core principle — every answer separates three things:
  1. hard Storelli evidence (internal proof)
  2. external inspiration / execution reference (never proof)
  3. strategic inference (judgement where data is thin)
If a metric isn't in the data (e.g. comment counts), the answer says so plainly.

Skills: comment_drivers, test_hypothesis, concept_references, idea_diagnosis,
calendar_doctor, learning_to_action, content_gap, shot_brief. Returns None when
no skill matches, so the existing Slack paths handle the turn. If a referenced
subject can't be resolved, it asks a clarifying question instead of guessing.
"""
from __future__ import annotations

import re
from typing import Optional

import slack_response_style as st
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"

# We do NOT ingest post-level engagement (comment/reply/like) metrics anywhere in
# the pipeline, so any comment/engagement question is answered as inference.
_HAS_COMMENT_METRICS = False

_PRODUCT_KW = [("bodyshield", "BodyShield GK Leggings"), ("gk leggings", "BodyShield GK Leggings"),
               ("leggings", "Pants & Leggings"), ("pants", "Pants & Leggings"),
               ("slider", "Sliders"), ("glove", "Gloves"), ("exoshield", "ExoShield"),
               ("head guard", "Head Guard"), ("headguard", "Head Guard"), ("jersey", "Jersey")]
_ICPS = ("Parents", "Adult Amateur", "Aspiring Pro")
_UNSAFE = ("messi", "ronaldo", "neymar", "mbappe", "champions league", "premier league",
           "world cup", "match highlights", "broadcast", "fan edit", "full match")


def _num(v, default=0.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _split(cell) -> list[str]:
    return [u.strip() for u in re.split(r"[;\n]", str(cell or "")) if u.strip()]


def _lower(s) -> str:
    return str(s or "").lower()


def _family(text: str) -> str:
    t = _lower(text)
    if any(k in t for k in ("bodyshield", "leggings", "pants", "sliders", " leg")):
        return "leggings"
    if "glove" in t:
        return "gloves"
    return ""


def _product_label(text: str) -> str:
    t = _lower(text)
    for kw, label in _PRODUCT_KW:
        if kw in t:
            return label
    return ""


def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url) or re.search(r"instagram\.com/([\w.\-]+)/", url)
    return "@" + m.group(1) if m else "creator"


def _pain_phrase(text: str) -> str:
    t = _lower(text)
    for kw, phrase in (("turf burn", "the turf burn"), ("sting", "the turf sting"),
                       ("wince", "the wince after a dive"), ("scrape", "the scrape"),
                       ("bruise", "the bruise"), ("bare knee", "bare knees on turf"),
                       ("grip", "the ball slipping loose")):
        if kw in t:
            return phrase
    return "the pain moment"


# ---------------------------------------------------------------------------
# sources — [S]=internal proof, [E]=external reference, [C]=semantic connection,
# [N]=Notion/calendar item, [I]=similar rated idea. Linked when a URL exists.
# ---------------------------------------------------------------------------
class _Src:
    def __init__(self):
        self.rows = []

    def add(self, kind: str, url: str, label: str) -> str:
        n = sum(1 for (t, _, _) in self.rows if t[0] == kind) + 1
        tag = f"{kind}{n}"
        self.rows.append((tag, str(url or "").strip(), label))
        return tag

    @property
    def ids(self) -> set:
        return {t for (t, _, _) in self.rows}

    @property
    def has_external(self) -> bool:
        return any(t[0] == "E" for (t, _, _) in self.rows)

    def render(self, used: Optional[set] = None) -> str:
        lines = ["*Sources:*"]
        for tag, url, label in self.rows:
            if used is not None and tag not in used:
                continue
            lines.append(f"  [{tag}] <{url}|{label}>" if url else f"  [{tag}] {label}")
        return "\n".join(lines) if len(lines) > 1 else ""


def _sources_tail(src: _Src, used: Optional[set] = None) -> str:
    block = src.render(used)
    if not block:
        return ""
    return f"{block}\n{_NOT_PROOF}" if src.has_external else block


# ---------------------------------------------------------------------------
# CEO renderer with flexible section labels (comment "Try", test "If it wins")
# ---------------------------------------------------------------------------
def _short(text, n: int = 18) -> str:
    s = re.split(r"(?<=[.!?])\s+", str(text or "").strip())[0].strip().rstrip(".")
    w = s.split()
    return (" ".join(w[:n]) + ("…" if len(w) > n else "")) if w else ""


def _ceo(lead: str, bullets=None, sections=None, src_tail: str = "",
         mode: str = st.MODE_DEFAULT) -> str:
    parts = [str(lead).strip()]
    if bullets:
        parts.append("*Why:*\n" + "\n".join(f"• {b}" for b in bullets[:5] if str(b).strip()))
    for label, text in (sections or []):
        if str(text).strip():
            parts.append(f"*{label}:* {str(text).strip()}")
    body = "\n\n".join(p for p in parts if p.strip())
    text = body + ("\n\n" + src_tail if src_tail else "")
    return st.compact_slack_response(text, mode)


# ---------------------------------------------------------------------------
# brain loading (read-only) + subject resolution
# ---------------------------------------------------------------------------
def _rd(sheets, name):
    try:
        return getattr(sheets, name)()
    except Exception:  # noqa: BLE001
        return []


def _load_brain(sheets):
    return {
        "profiles": _rd(sheets, "read_profiles"),
        "connections": _rd(sheets, "read_semantic_connections"),
        "ideas": _rd(sheets, "read_ideas"),
        "calendar": _rd(sheets, "read_calendar_ratings"),
        "adhoc": _rd(sheets, "read_adhoc_evaluations"),
    }


def _active_profiles(brain) -> list[dict]:
    return [p for p in brain["profiles"]
            if _lower(p.get("ACTIVE")) == "true"
            and _lower(p.get("CONFIDENCE")) in ("medium", "high")]


def _best_profile(fam: str, brain) -> Optional[dict]:
    pool = [p for p in _active_profiles(brain) if not fam or _family(p.get("PRODUCT", "")) == fam]
    if not pool:
        return None
    return sorted(pool, key=lambda p: (_lower(p.get("CONFIDENCE")) == "high",
                                       _num(p.get("INTERNAL_SAMPLE_SIZE"))), reverse=True)[0]


def _best_connection(fam: str, theme: str, brain) -> Optional[dict]:
    pool = [c for c in brain["connections"] if not fam or _family(c.get("PRODUCT", "")) == fam]
    if not pool:
        return None
    toks = set(re.findall(r"[a-z]{4,}", _lower(theme)))

    def fit(c):
        overlap = len(toks & set(re.findall(r"[a-z]{4,}", _lower(c.get("CONCEPT_NAME", "")))))
        return overlap * 10 + _num(c.get("CONNECTION_SCORE"))
    return sorted(pool, key=fit, reverse=True)[0]


def _top_refined_idea(fam: str, brain) -> Optional[dict]:
    pool = [i for i in brain["ideas"] if not fam or _family(i.get("PRODUCT", "")) == fam]
    if not pool:
        return None
    return sorted(pool, key=lambda i: _num(i.get("IDEA_SCORE")), reverse=True)[0]


def _external_refs(conn: Optional[dict]) -> list[dict]:
    if not conn:
        return []
    urls = _split(conn.get("EXTERNAL_REFERENCE_URLS"))
    creators = [x.strip() for x in str(conn.get("EXTERNAL_CREATORS", "")).split(",")]
    out = []
    for i, u in enumerate(urls):
        out.append({"url": u, "creator": (creators[i] if i < len(creators) and creators[i] else _handle(u))})
    return out


def _subject_from_eval(ev: dict) -> dict:
    return {
        "title": ev.get("IDEA_TITLE", "") or "the evaluated idea",
        "product": ev.get("PRODUCT", ""),
        "concept": ev.get("CONCEPT_TEXT", "") or ev.get("NORMALIZED_IDEA_TEXT", ""),
        "structure": ev.get("SUGGESTED_STORY_STRUCTURE", ""),
        "steal": ev.get("WHAT_TO_STEAL", ""), "not_copy": ev.get("WHAT_NOT_TO_COPY", ""),
        "weak": ev.get("WHAT_IS_WEAK", ""), "improve": ev.get("HOW_TO_IMPROVE", ""),
        "internal_urls": _split(ev.get("INTERNAL_EVIDENCE_URLS")),
        "external_refs": [{"url": u, "creator": _handle(u)}
                          for u in _split(ev.get("EXTERNAL_REFERENCE_URLS"))],
        "profile_name": ev.get("CLOSEST_WINNING_PROFILE_NAME", ""),
        "connection_name": ev.get("CLOSEST_SEMANTIC_CONNECTION_NAME", ""),
        "recommendation": ev.get("RECOMMENDATION", ""),
    }


def _subject_from_brain(fam: str, theme: str, brain) -> Optional[dict]:
    prof = _best_profile(fam, brain)
    conn = _best_connection(fam, theme, brain)
    idea = _top_refined_idea(fam, brain)
    if not (prof or conn or idea):
        return None
    import semantic_connections as sc
    product = (prof or {}).get("PRODUCT") or (idea or {}).get("PRODUCT") or _product_label(theme) or "the product"
    structure = (conn or {}).get("STORYTELLING_STRUCTURE") or \
        sc.structure_for((prof or {}).get("HOOK_TAGS", ""))
    title = (idea or {}).get("REFINED_IDEA_TITLE") or (idea or {}).get("IDEA_TITLE") or \
        (conn or {}).get("CONCEPT_NAME") or f"{product} concept"
    return {
        "title": title, "product": product,
        "concept": (idea or {}).get("REFINED_CONCEPT") or (idea or {}).get("CONCEPT") or "",
        "structure": structure,
        "steal": (conn or {}).get("WHAT_TO_STEAL", "the relatable pain/wince beat and demo pacing"),
        "not_copy": (conn or {}).get("WHAT_NOT_TO_COPY",
                                     "their caption/script, on-screen branding, and non-goalkeeper claims"),
        "weak": (idea or {}).get("ORIGINAL_WEAKNESS", "") or (idea or {}).get("RISK_NOTES", ""),
        "improve": "",
        "internal_urls": _split((prof or {}).get("SUPPORTING_VIDEO_URLS"))
        or _split((idea or {}).get("INTERNAL_EVIDENCE_URLS")),
        "external_refs": _external_refs(conn),
        "profile_name": (prof or {}).get("PROFILE_NAME", ""),
        "connection_name": (conn or {}).get("CONCEPT_NAME", ""),
        "recommendation": "",
    }


def _last_evaluated(context) -> Optional[dict]:
    """The most recent ad-hoc Notion evaluation, if the thread refers to it."""
    try:
        import adhoc_idea_evaluator as ae
        import notion_idea_ingest as ni
    except Exception:  # noqa: BLE001
        return None
    url = ""
    for m in reversed(context or []):
        url = ni.find_notion_url(m.get("text", ""))
        if url:
            break
    if url:
        pid = ni.extract_page_id(url)
        if pid and pid in ae._EVAL_CACHE:
            return ae._EVAL_CACHE[pid]
    return next(reversed(ae._EVAL_CACHE.values())) if ae._EVAL_CACHE else None


def _resolve_subject(text: str, context, brain) -> Optional[dict]:
    """A concrete subject to reason about: explicit product/theme in the message,
    else the last evaluated Notion idea, else the product from the prior turn."""
    fam = _family(text)
    if fam:
        return _subject_from_brain(fam, text, brain)
    ev = _last_evaluated(context)
    if ev:
        return _subject_from_eval(ev)
    last = " ".join(m.get("text", "") for m in (context or []))
    fam = _family(last)
    if fam:
        return _subject_from_brain(fam, last, brain)
    return None


def _subject_sources(subject: dict, src: _Src, max_ext: int = 3) -> None:
    for u in subject.get("internal_urls", [])[:1]:
        src.add("S", u, f"Storelli internal proof — {subject.get('profile_name','winning profile')}")
    if subject.get("connection_name"):
        src.add("C", "", f"Semantic connection — {subject['connection_name'][:40]}")
    for r in subject.get("external_refs", [])[:max_ext]:
        src.add("E", r["url"], f"External execution reference — {r['creator']}")


# ---------------------------------------------------------------------------
# LLM synthesis (Part C) — new strategist schema, validated, deterministic fallback
# ---------------------------------------------------------------------------
_SYNTH_PROMPT = (
    "You are Storelli's senior social media strategist. Use ONLY the supplied evidence. "
    "Be blunt, practical, and concise. Separate hard Storelli evidence from strategic "
    "inference. External inspiration is execution reference only, not proof. If a metric "
    "is not available (e.g. comment counts), say so.\n\n"
    "Evidence pack:\n<<FACTS>>\n\n"
    "You may cite ONLY these source ids: <<IDS>>.\n\n"
    "Return ONLY strict JSON: {\"lead\": str, \"hard_evidence\": [str], "
    "\"strategic_inference\": [str], \"recommendation\": str, \"next_action\": str, "
    "\"sources_used\": [str]}")


def _validate_strategy(obj, allowed: set, require_metric_caveat: bool = False) -> tuple:
    if not isinstance(obj, dict):
        return False, "not a dict"
    if not str(obj.get("lead", "")).strip():
        return False, "empty lead"
    if not str(obj.get("recommendation", "")).strip():
        return False, "empty recommendation"
    used = obj.get("sources_used", [])
    if not isinstance(used, list):
        return False, "sources_used not a list"
    if any(str(i).strip() and str(i).strip() not in allowed for i in used):
        return False, "invented source id"
    text = " ".join([str(obj.get("lead", "")), str(obj.get("recommendation", "")),
                     str(obj.get("next_action", "")),
                     " ".join(map(str, obj.get("hard_evidence", []) or [])),
                     " ".join(map(str, obj.get("strategic_inference", []) or []))]).lower()
    if re.search(r"(external|inspiration|their video|reference)[^.]{0,40}\bprov(e|es|en|ing)\b", text):
        return False, "external as proof"
    if any(u in text for u in _UNSAFE):
        return False, "unsafe reference"
    # Comment/engagement honesty: if we have no metrics, an answer that asserts a
    # hard comment metric (a number of comments) is invalid.
    if require_metric_caveat and re.search(r"\d+\s*(comments|replies)\b", text):
        return False, "claims hard comment metric we don't have"
    return True, ""


def _synthesize(facts: str, src: _Src, gemini, mode: str,
                require_metric_caveat: bool = False) -> Optional[dict]:
    if gemini is None:
        return None
    allowed = src.ids
    prompt = (_SYNTH_PROMPT.replace("<<FACTS>>", facts)
              .replace("<<IDS>>", ", ".join(sorted(allowed)) or "(none)"))
    try:
        from analyzer import parse_model_json
        obj = parse_model_json(gemini.summarize_findings(prompt))
    except Exception as e:  # noqa: BLE001
        log.warning("strategy synth failed: %s", e)
        return None
    ok, reason = _validate_strategy(obj, allowed, require_metric_caveat)
    if not ok:
        log.info("strategy synth rejected (%s) -> deterministic", reason)
        return None
    return obj


def _render_strategy(obj: dict, src: _Src, mode: str) -> str:
    bullets = []
    for h in (obj.get("hard_evidence") or [])[:2]:
        bullets.append(f"Evidence: {_short(h, 16)}")
    for inf in (obj.get("strategic_inference") or [])[:2]:
        bullets.append(f"Inference: {_short(inf, 16)}")
    used = {str(i).strip() for i in (obj.get("sources_used") or []) if str(i).strip() in src.ids}
    move = f"{obj.get('recommendation','')} {obj.get('next_action','')}".strip()
    return _ceo(str(obj["lead"]).strip(), bullets=bullets, sections=[("My move", move)],
                src_tail=_sources_tail(src, used or None), mode=mode)


def _resolve_gemini(gemini):
    if gemini == "auto":
        try:
            from gemini_client import GeminiClient
            return GeminiClient()
        except Exception:  # noqa: BLE001
            return None
    return gemini


# ---------------------------------------------------------------------------
# skill implementations
# ---------------------------------------------------------------------------
def _skill_comment_drivers(text, context, brain, gemini, mode) -> str:
    prof = _best_profile(_family(text) or _family(" ".join(m.get("text", "") for m in context or [])), brain) \
        or (_active_profiles(brain)[0] if _active_profiles(brain) else None)
    src = _Src()
    hooks = []
    if prof:
        src.add("S", _split(prof.get("SUPPORTING_VIDEO_URLS"))[:1][0] if _split(prof.get("SUPPORTING_VIDEO_URLS")) else "",
                f"Storelli internal proof — {prof.get('PROFILE_NAME','winning profile')}")
        hooks = [h.strip() for h in str(prof.get("HOOK_TAGS", "")).split(",") if h.strip()]
    pain = _pain_phrase(text if _family(text) else " ".join(m.get("text", "") for m in context or []))
    inference = [
        f"Pain-confession & question hooks invite replies — our winning formats lean on "
        f"{', '.join(hooks[:2]) or 'Curiosity Gap + Demo'}.",
        "Do/Don't and myth hooks pull corrections in the comments.",
        f"First-person pain ('{pain}') prompts people to share their own story.",
    ]
    cta = "What's the worst turf burn you've had after a dive?"
    # Deterministic by design: guarantees the honest "no hard comment metric"
    # framing and the preferred "Try:" prompt-CTA shape (comments are inference).
    lead = ("Short answer: I don't have hard comment-level proof yet — we don't track comment "
            "counts. Based on the hooks/formats that work, I'd test pain-confession hooks first.")
    return _ceo(lead, bullets=[_short(b, 20) for b in inference],
                sections=[("Try", f"“{cta}”")], src_tail=_sources_tail(src), mode=mode)


def _skill_test_hypothesis(text, context, brain, gemini, mode) -> Optional[str]:
    subject = _resolve_subject(text, context, brain)
    if not subject:
        return _clarify()
    product = subject["product"]
    pain = _pain_phrase(f"{text} {subject.get('concept','')} {subject.get('title','')}")
    src = _Src()
    _subject_sources(subject, src, max_ext=1)
    lead = (f"This test tells us whether {pain}-led {product} content beats generic protection "
            f"education for this audience.")
    sections = [
        ("If it wins", f"pain-led hooks are our driver for {product} — make more, and standardize "
                       f"the {subject.get('structure','pain → protected replay → CTA')} structure."),
        ("If it loses", "generic protection/education is enough here — stop over-indexing on pain hooks "
                        "for this ICP."),
        ("Compare against", "a straight product-demo or education cut of the same idea (the control)."),
        ("Metric/learning", "watch saves, replies and comment sentiment — we have no hard comment "
                            "history yet, so that IS the learning target."),
        ("My move", "shoot the pain-led cut first; hold the demo cut as the control."),
    ]
    return _ceo(lead, sections=sections, src_tail=_sources_tail(src), mode=mode)


def _skill_concept_references(text, context, brain, gemini, mode) -> Optional[str]:
    subject = _resolve_subject(text, context, brain)
    if not subject:
        return _clarify()
    refs = subject.get("external_refs", [])[:3]
    if not refs:
        return (f"I don't have safe external execution references tied to '{subject['title']}' yet "
                "— run discovery/quality-review to grow the reference pool.")
    src = _Src()
    for u in subject.get("internal_urls", [])[:1]:
        src.add("S", u, f"Storelli internal proof — {subject.get('profile_name','winning profile')}")
    if subject.get("connection_name"):
        src.add("C", "", f"Semantic connection — {subject['connection_name'][:40]}")
    uses = ["the pain/wince beat and its timing", "the demo pacing and clean single-take rhythm",
            "the hook framing and on-screen text cadence"]
    bullets = []
    for i, r in enumerate(refs, 1):
        src.add("E", r["url"], f"External execution reference — {r['creator']}")
        bullets.append(f"[E{i}] {r['creator']} — useful for {uses[(i-1) % len(uses)]}; "
                       f"steal {_short(subject.get('steal',''), 8)}; don't copy {_short(subject.get('not_copy',''), 6)}")
    concept = subject.get("connection_name") or f"{subject['product']} concept"
    lead = f"Before shooting the {concept[:46]}, watch these execution references:"
    return _ceo(lead, bullets=bullets, sections=[("Structure to hit", subject.get("structure", ""))],
                src_tail=_sources_tail(src), mode=mode)


def _skill_idea_diagnosis(text, context, brain, gemini, mode) -> Optional[str]:
    subject = _resolve_subject(text, context, brain)
    if not subject:
        return _clarify()
    src = _Src()
    _subject_sources(subject, src, max_ext=1)
    weak = str(subject.get("weak", "")).strip() or \
        "the hook is too broad and the product's protective role isn't the hero beat"
    t = _lower(text)
    missing = ("a single concrete pain moment in the first 2 seconds" if "missing" in t or "broad" in t
               else "an explicit product-protection reveal tied to the pain")
    has_evidence = bool(subject.get("internal_urls"))
    verdict = "Revise then shoot" if has_evidence else "Keep as a low-cost test"
    fix = (f"open on {_pain_phrase(text + ' ' + subject.get('concept',''))}, then make the "
           f"{subject['product']} protection the turn, then CTA.")
    facts = (f"Question: diagnose the idea '{subject['title']}' ({subject['product']}).\n"
             f"Closest internal proof: {subject.get('profile_name') or 'none'} "
             f"(evidence {'present' if has_evidence else 'thin'}).\n"
             f"Known weakness: {weak}. Structure available: {subject.get('structure','')}.")
    obj = _synthesize(facts, src, gemini, mode)
    if obj:
        obj.setdefault("recommendation", verdict)
        return _render_strategy(obj, src, mode)
    return _ceo("Here's the honest diagnosis:",
                bullets=[f"Weakness: {_short(weak, 18)}", f"Missing: {missing}",
                         f"Product role: {'clear' if has_evidence else 'needs to be the hero beat'}"],
                sections=[("Verdict", f"{verdict} — Fix: {fix}")],
                src_tail=_sources_tail(src), mode=mode)


def _skill_calendar_doctor(text, context, brain, gemini, mode) -> str:
    ratings = [r for r in brain["calendar"] if _lower(r.get("SHOULD_RATE")) == "true"]
    if not ratings:
        return ("No calendar ratings yet — run `rate-calendar-ideas` first and I'll give you the "
                "revise / kill / move-up / wait triage.")

    def action(r):
        rec = _lower(r.get("RECOMMENDATION"))
        sc = _num(r.get("CALENDAR_IDEA_SCORE"))
        if rec in ("reject", "do not prioritize") or sc < 50:
            return "Kill"
        if rec in ("revise", "revise then shoot") or sc < 62:
            return "Revise"
        if sc >= 75:
            return "Move up"
        return "Wait"
    # Prioritize the most actionable: Kill/Move up/Revise before Wait, by |distance from 62|.
    order = {"Kill": 0, "Move up": 1, "Revise": 2, "Wait": 3}
    ranked = sorted(ratings, key=lambda r: (order[action(r)], -abs(_num(r.get("CALENDAR_IDEA_SCORE")) - 62)))[:3]
    src = _Src()
    bullets = []
    for r in ranked:
        a = action(r)
        title = str(r.get("CALENDAR_TITLE", ""))[:46]
        why = "too generic / thin hook" if a in ("Kill", "Revise") else \
              ("strong, shootable now" if a == "Move up" else "fine but not urgent")
        fix = "sharpen to one concrete pain moment + product turn" if a in ("Kill", "Revise") else \
              ("slot it next" if a == "Move up" else "hold for a lighter week")
        nid = src.add("N", r.get("NOTION_PAGE_URL", ""), f"Calendar — {title}")
        bullets.append(f"*{a}* — {title} ({int(_num(r.get('CALENDAR_IDEA_SCORE')))}): {why}. "
                       f"Fix: {fix} [{nid}]")
    return _ceo("Calendar triage (top 3):", bullets=bullets,
                sections=[("My move", "clear the Kills, ship the Move-up first.")],
                src_tail=_sources_tail(src), mode=mode)


def _skill_learning_to_action(text, context, brain, gemini, mode) -> str:
    learning, src = _top_learning(brain)
    profs = _active_profiles(brain)
    make_more = (profs[0].get("PROFILE_NAME", "") if profs else "pain-led protection demos")
    stop = "generic 'protective gear' education with no pain moment and no product turn."
    action = "standardize the winning hook+format and queue 2 variants this week."
    facts = (f"Question: what should we do because of our learnings?\n"
             f"Top learning (internal): {learning}\n"
             f"Winning format to lean on: {make_more}. What underperforms: {stop}")
    obj = _synthesize(facts, src, gemini, mode)
    if obj:
        return _render_strategy(obj, src, mode)
    return _ceo("What the data taught us → what to do:",
                bullets=[f"Learning: {_short(learning, 20)}",
                         f"Make more of: {_short(make_more, 14)}",
                         f"Stop: {_short(stop, 14)}"],
                sections=[("Do next", action)], src_tail=_sources_tail(src), mode=mode)


def _skill_content_gap(text, context, brain, gemini, mode) -> str:
    profs = _active_profiles(brain)
    ideas = brain["ideas"]
    covered_icp = {p.get("ICP", "").strip() for p in profs} | \
        {i.get("ICP", "").strip() for i in ideas if _num(i.get("IDEA_SCORE")) >= 70}
    icp_gaps = [icp for icp in _ICPS if icp not in covered_icp]
    fam_covered = {_family(p.get("PRODUCT", "")) for p in profs}
    thin_products = [lbl for fam, lbl in (("leggings", "Leggings/BodyShield"), ("gloves", "Gloves"))
                     if fam not in fam_covered]
    src = _Src()
    for p in profs[:1]:
        u = _split(p.get("SUPPORTING_VIDEO_URLS"))[:1]
        if u:
            src.add("S", u[0], f"Storelli internal proof — {p.get('PROFILE_NAME','')}")
    gaps = []
    if icp_gaps:
        gaps.append(f"Thin ICP coverage: {', '.join(icp_gaps)} content — little/no internal proof yet.")
    if thin_products:
        gaps.append(f"Thin product evidence: {', '.join(thin_products)} — needs more tested winners.")
    if not gaps:
        gaps.append("Coverage is broad; the thin spot is depth of proof per hook, not breadth.")
    next_test = (f"Run a {icp_gaps[0]}-facing pain→protection test next."
                 if icp_gaps else "Add a second winner in your weakest product family.")
    facts = ("Question: where are we thin / what's missing?\n"
             f"ICP coverage: {sorted(covered_icp)}. Missing ICPs: {icp_gaps}. "
             f"Product families with proof: {sorted(f for f in fam_covered if f)}.")
    obj = _synthesize(facts, src, gemini, mode)
    if obj:
        return _render_strategy(obj, src, mode)
    return _ceo("Where we're thin:", bullets=[_short(g, 20) for g in gaps],
                sections=[("Next test", next_test)], src_tail=_sources_tail(src), mode=mode)


def _skill_shot_brief(text, context, brain, gemini, mode) -> Optional[str]:
    subject = _resolve_subject(text, context, brain)
    if not subject:
        return _clarify()
    import semantic_connections as sc
    structure = subject.get("structure") or sc._DEFAULT_STRUCTURE
    product = subject["product"]
    pain = _pain_phrase(f"{text} {subject.get('concept','')} {subject.get('title','')}")
    stages = [s.strip() for s in re.split(r"→|->|\|", structure) if s.strip()]
    beats = []
    for stage in stages:
        sl = _lower(stage)
        if any(k in sl for k in ("curiosity", "hook", "fear", "risk", "pov")):
            beats.append(f"Hook (0–2s): open on a question about {pain}.")
        elif any(k in sl for k in ("demo", "story", "tutorial", "explain")):
            beats.append("Show the action in one clean take (a real diving save / rep).")
        elif any(k in sl for k in ("pain", "wince", "mistake")):
            beats.append(f"Cut to {pain} — the wince, tight close-up.")
        elif any(k in sl for k in ("protect", "replay", "product", "correction", "reveal", "gear")):
            beats.append(f"Product moment: reveal the protected replay wearing {product}.")
        elif "cta" in sl:
            beats.append("Land the one-line CTA over the product shot.")
        else:
            beats.append(f"{stage}.")
    if len(beats) < 4:
        beats.append("Proof beat: a quick before/after or confidence line.")
    beats = beats[:6]
    src = _Src()
    _subject_sources(subject, src, max_ext=1)
    ref = subject.get("external_refs", [])
    lines = [f"Shoot brief — *{subject['title'][:48]}*:", "",
             f"*Hook:* {pain.capitalize()} — one line, first 2 seconds.", "", "*Beats:*"]
    lines += [f"{i}. {b}" for i, b in enumerate(beats, 1)]      # numbered at line start
    lines += ["", f"*CTA:* Protect every dive — {product}."]
    if ref:
        lines.append(f"*Reference:* [E1] {ref[0]['creator']} — steal {_short(subject.get('steal',''), 10)}")
    lines.append(f"*Don't copy:* {_short(subject.get('not_copy',''), 14)}")
    body = "\n".join(lines)
    tail = _sources_tail(src)
    return st.compact_slack_response(body + ("\n\n" + tail if tail else ""), st.MODE_DEEP)


def _top_learning(brain) -> tuple:
    """A single learning line + its source. Prefers latest_learnings.md, else the
    top winning profile's performance signal."""
    src = _Src()
    try:
        import os
        import synthesizer
        if os.path.exists(synthesizer.LEARNINGS_PATH):
            with open(synthesizer.LEARNINGS_PATH, encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip("#* -\n")
                    if len(s) > 30:
                        src.add("N", "", "latest_learnings.md")
                        return s[:200], src
    except Exception:  # noqa: BLE001
        pass
    profs = _active_profiles(brain)
    if profs:
        p = profs[0]
        u = _split(p.get("SUPPORTING_VIDEO_URLS"))[:1]
        if u:
            src.add("S", u[0], f"Storelli internal proof — {p.get('PROFILE_NAME','')}")
        return (str(p.get("PERFORMANCE_SIGNAL", "")) or
                f"{p.get('HOOK_TAGS','')} + {p.get('FORMAT_TAGS','')} performs for "
                f"{p.get('PRODUCT','')}/{p.get('ICP','')}."), src
    return "Pain-led protection demos are our most reliable format so far.", src


def _clarify() -> str:
    return ("Which idea or product do you mean? Name it (or paste the Notion idea) and I'll "
            "reason from the evidence.")


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------
def detect_skill(text: str, context: Optional[list] = None) -> str:
    t = " " + _lower(text) + " "
    has_prior = bool(context)
    # A generic "take inspiration from" ask belongs to the semantic layer.
    if "take inspiration from" in t:
        return ""
    if any(k in t for k in ("shoot brief", "shot brief", "shoot-brief", "shot beats",
                            "shot list", "should gerald film", "editor look for",
                            "turn this into a shoot", "turn that into a shoot",
                            "turn it into a shoot", "into a shoot brief")):
        return "shot_brief"
    if "calendar" in t and any(k in t for k in ("revise", "kill", "move up", "move-up", "wait",
                                                "too generic", "what should we", "triage", "clean up")):
        return "calendar_doctor"
    if any(k in t for k in ("what are we missing", "missing in the calendar", "overdoing",
                            "enough parent", "evidence thin", "where is the evidence",
                            "products need more testing", "content gap", "gaps in")):
        return "content_gap"
    if any(k in t for k in ("watch before", "before shooting", "which references", "references map",
                            "map to this concept", "steal the pacing", "maps to the pain",
                            "which video", "what videos should we watch", "reference for this")):
        return "concept_references"
    if any(k in t for k in ("trying to learn", "what would success prove", "hypothesis",
                            "why should we test", "compare this against", "compare it against",
                            "what are we trying to learn", "what would failure", "what to compare")):
        return "test_hypothesis"
    if any(k in t for k in ("comment", "comments", "invite replies", "invite comments", "drive comments",
                            "ask a question in this", "more replies", "comment-driven", "reply-bait")):
        return "comment_drivers"
    if (("weak" in t and any(k in t for k in ("idea", "hook", "this", "concept", "it")))
            or any(k in t for k in ("what is missing", "what's missing", "hook too broad",
                                    "is this shootable", "product role clear", "is the product role",
                                    "diagnose", "what's wrong with", "why is it weak"))):
        return "idea_diagnosis"
    if any(k in t for k in ("because of our latest learnings", "what did the data", "what should we make more",
                            "what should we stop", "learning to action")) or \
            ("learnings" in t and any(k in t for k in ("what should we do", "what should we make",
                                                       "what should we stop", "act on"))):
        return "learning_to_action"
    # Follow-ups that map to a skill when there's prior context.
    if has_prior:
        if any(k in t for k in ("what videos should we use", "which videos should we use",
                                "videos should we use", "videos for this")):
            return "concept_references"
        if "make it more comment" in t or "more comment-driven" in t:
            return "comment_drivers"
    return ""


_SKILLS = {
    "comment_drivers": _skill_comment_drivers,
    "test_hypothesis": _skill_test_hypothesis,
    "concept_references": _skill_concept_references,
    "idea_diagnosis": _skill_idea_diagnosis,
    "calendar_doctor": _skill_calendar_doctor,
    "learning_to_action": _skill_learning_to_action,
    "content_gap": _skill_content_gap,
    "shot_brief": _skill_shot_brief,
}


def is_strategy_query(text: str, context: Optional[list] = None) -> bool:
    return bool(detect_skill(text, context))


def answer(text: str, context: Optional[list] = None, sheets=None, gemini="auto") -> Optional[str]:
    """Route a social-strategy question to the right skill and answer it from the
    brain. Read-only; returns None when no skill matches."""
    skill = detect_skill(text, context)
    if not skill:
        return None
    try:
        s = sheets
        if s is None:
            from inspiration_sheets import InspirationSheets
            s = InspirationSheets()
        brain = _load_brain(s)
    except Exception as e:  # noqa: BLE001
        log.warning("strategy skills: brain load failed: %s", e)
        return None
    mode = st.detect_response_mode(text)
    gem = _resolve_gemini(gemini)
    try:
        return _SKILLS[skill](text, context, brain, gem, mode)
    except Exception as e:  # noqa: BLE001 - never break the bot
        log.warning("strategy skill '%s' failed: %s", skill, e)
        return None
