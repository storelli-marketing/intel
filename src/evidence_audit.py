"""Parents / Youth Internal Evidence Gap Audit.

Audits the INTERNAL Storelli evidence base (analyzed rows, winning profiles,
latest_learnings.md, calendar ratings) for Parents/youth signals, scores the
evidence gaps, and — when internal proof is thin — proposes labelled
EVIDENCE-BUILDING TESTS (never proven ideas). It also answers Slack questions
about missing proof with a disciplined, source-linked decision trace.

Hard rules honoured here:
- Internal performance proof comes ONLY from Storelli analyzed videos / winning
  profiles. External inspiration is execution reference only, never proof, and
  can never close an internal evidence gap.
- No Parents/youth winning profile is created without sufficient REAL internal
  evidence. This module never writes profiles or internal rows; it only writes
  the EVIDENCE_GAPS audit artifact.
- Evidence-building tests are explicitly labelled tests, not recommendations.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Optional

import decision_trace as dt
import slack_response_style as st
from inspiration_sheets import InspirationSheets
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"

# A coherent product×ICP cluster needs at least this many internal "Great"
# winners to justify a winning profile (matches how winning_profiles is built).
_PROFILE_MIN_GREAT = 3
_PROFILE_MIN_SAMPLE = 4

_PARENTS_KW = ("parent", "youth", "kid", "child", "young", "mom", "dad", "school",
               "junior", "academy", "son", "daughter", "u8", "u9", "u10", "u11", "u12")


def _norm(v) -> str:
    return str(v or "").strip()


def _perf(row) -> str:
    return _norm(row.get("PERFORMANCE")).lower()


def _is_parents(row) -> bool:
    icp = _norm(row.get("ICP")).lower()
    if icp == "parents" or "youth" in icp:
        return True
    blob = " ".join(_norm(row.get(k)) for k in ("ICP", "Product", "Storytelling structure", "ID")).lower()
    return any(k in blob for k in _PARENTS_KW)


def _signals(row, prefix) -> list[str]:
    return [k.replace(prefix, "").replace("_", " ") for k in row
            if k.startswith(prefix) and _norm(row.get(k)) == "1"]


# ---------------------------------------------------------------------------
# Part A — internal audit (pure; inject rows for tests)
# ---------------------------------------------------------------------------
def audit(internal_rows: list[dict], profiles: Optional[list] = None,
          calendar: Optional[list] = None, learnings_text: str = "") -> dict:
    profiles = profiles or []
    calendar = calendar or []
    parents = [r for r in internal_rows if _is_parents(r)]
    great = [r for r in parents if _perf(r) == "great"]
    good = [r for r in parents if _perf(r) == "ok"]
    weak = [r for r in parents if _perf(r) in ("underdog", "weak")]

    from collections import Counter
    products = Counter(_norm(r.get("Product")) or "(unspecified)" for r in parents)
    hooks = Counter(h for r in great for h in _signals(r, "signal_hook_"))
    formats = Counter(f for r in great for f in _signals(r, "signal_format_"))
    best_urls = [_norm(r.get("LINK")) for r in great if _norm(r.get("LINK"))]

    # Largest single product×ICP=Parents cluster of Great winners (profile needs one).
    great_by_product = Counter(_norm(r.get("Product")) for r in great if _norm(r.get("Product")))
    top_cluster_great = max(great_by_product.values()) if great_by_product else 0

    has_parents_profile = any("parent" in _norm(p.get("ICP")).lower()
                              or "youth" in _norm(p.get("PROFILE_NAME")).lower() for p in profiles)
    parents_calendar = [r for r in calendar
                        if any(k in (_norm(r.get("ICP")) + _norm(r.get("CALENDAR_TITLE"))
                                     + _norm(r.get("PRODUCT"))).lower() for k in _PARENTS_KW)]
    learnings_hits = [ln.strip() for ln in (learnings_text or "").splitlines()
                      if any(k in ln.lower() for k in _PARENTS_KW)]

    return {
        "parents_rows": len(parents), "great": len(great), "good": len(good), "weak": len(weak),
        "products": dict(products), "hooks": dict(hooks), "formats": dict(formats),
        "best_urls": best_urls[:3], "top_cluster_great": top_cluster_great,
        "has_parents_profile": has_parents_profile,
        "parents_calendar": len(parents_calendar), "learnings_hits": len(learnings_hits),
    }


def profile_justified(a: dict) -> tuple[bool, str]:
    """A Parents/youth winning profile is justified ONLY with real, sufficient
    internal evidence — never forced. Returns (justified, reason)."""
    if a["top_cluster_great"] >= _PROFILE_MIN_GREAT and a["parents_rows"] >= _PROFILE_MIN_SAMPLE:
        return True, (f"{a['top_cluster_great']} Great winners in one product cluster "
                      f"across {a['parents_rows']} Parents rows — a coherent, sufficient signal.")
    return False, (f"only {a['great']} Great across {a['parents_rows']} Parents rows "
                   f"(largest single-product cluster = {a['top_cluster_great']} Great); "
                   f"below the {_PROFILE_MIN_GREAT}-Great / {_PROFILE_MIN_SAMPLE}-sample bar. "
                   "Real but thin signal — a hypothesis to test, not proof.")


# ---------------------------------------------------------------------------
# helpers for per-product evidence (Head Guard / Sliders gaps)
# ---------------------------------------------------------------------------
def _product_evidence(rows: list[dict], *keywords) -> dict:
    kw = [k.lower() for k in keywords]
    hit = [r for r in rows if any(k in _norm(r.get("Product")).lower() for k in kw)]
    great = [r for r in hit if _perf(r) == "great"]
    return {"count": len(hit), "great": len(great),
            "urls": [_norm(r.get("LINK")) for r in great if _norm(r.get("LINK"))][:2]}


# ---------------------------------------------------------------------------
# Part B — evidence gaps
# ---------------------------------------------------------------------------
def _gap_id(name: str) -> str:
    return "GAP-" + hashlib.sha1(name.encode()).hexdigest()[:8]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def evidence_gaps(a: dict, internal_rows: list[dict], external_use_count: int = 0) -> list[dict]:
    """Deterministic gap rows. External counts are recorded as *references only*
    and never raise internal evidence or confidence."""
    justified, reason = profile_justified(a)
    hg = _product_evidence(internal_rows, "head guard", "exoshield")
    sl = _product_evidence(internal_rows, "slider")
    gaps = [
        {"GAP_NAME": "Parents / Youth Safety Proof", "PRODUCT": "BodyShield GK Leggings / Pants & Leggings",
         "ICP": "Parents", "CURRENT_INTERNAL_EVIDENCE_COUNT": a["parents_rows"], "GREAT_COUNT": a["great"],
         "BEST_INTERNAL_URLS": ";".join(a["best_urls"]),
         "EXISTING_EXTERNAL_REFERENCES": f"{external_use_count} refs (execution reference only, not proof)",
         "CURRENT_CONFIDENCE": "None" if not justified else "Emerging",
         "WHY_IT_MATTERS": "Parents are the buyers for youth gear; a proven pillar unlocks a new ICP.",
         "WHAT_PROOF_IS_MISSING": reason,
         "RECOMMENDED_TESTS": "Parent POV Hesitate; Before/After Turf-Burn; Coach's Warning",
         "PRIORITY": "High", "STATUS": "Open"},
        {"GAP_NAME": "Head Guard / ExoShield Proof", "PRODUCT": "ExoShield Head Guards", "ICP": "Parents / Youth",
         "CURRENT_INTERNAL_EVIDENCE_COUNT": hg["count"], "GREAT_COUNT": hg["great"],
         "BEST_INTERNAL_URLS": ";".join(hg["urls"]),
         "EXISTING_EXTERNAL_REFERENCES": "off-domain protective-gear refs only (reference)",
         "CURRENT_CONFIDENCE": "None",
         "WHY_IT_MATTERS": "Head protection is a parent safety trigger; almost no internal proof yet.",
         "WHAT_PROOF_IS_MISSING": f"only {hg['great']} Great of {hg['count']} Head Guard rows — need a coherent winner set.",
         "RECOMMENDED_TESTS": "Head Guard Reaction: The Save That Scared Mom",
         "PRIORITY": "Medium", "STATUS": "Open"},
        {"GAP_NAME": "Sliders Proof", "PRODUCT": "Sliders", "ICP": "Adult Amateur / Aspiring Pro",
         "CURRENT_INTERNAL_EVIDENCE_COUNT": sl["count"], "GREAT_COUNT": sl["great"],
         "BEST_INTERNAL_URLS": ";".join(sl["urls"]),
         "EXISTING_EXTERNAL_REFERENCES": "thin", "CURRENT_CONFIDENCE": "None",
         "WHY_IT_MATTERS": "Sliders are an untested product line with no winning profile.",
         "WHAT_PROOF_IS_MISSING": f"{sl['count']} internal rows, {sl['great']} Great — not enough to anchor a concept.",
         "RECOMMENDED_TESTS": "Slide-tackle turf-burn demo (Adult Amateur first)",
         "PRIORITY": "Low", "STATUS": "Open"},
        {"GAP_NAME": "Parent Trust / Buyer Objection Proof", "PRODUCT": "Cross-product", "ICP": "Parents",
         "CURRENT_INTERNAL_EVIDENCE_COUNT": a["parents_rows"], "GREAT_COUNT": a["great"],
         "BEST_INTERNAL_URLS": ";".join(a["best_urls"]),
         "EXISTING_EXTERNAL_REFERENCES": "coach-led / parent-advice refs (reference only)",
         "CURRENT_CONFIDENCE": "None",
         "WHY_IT_MATTERS": "Purchase objection handling for parents is unproven; drives conversion.",
         "WHAT_PROOF_IS_MISSING": "no internal content tests a parent buying-objection angle with a measured result.",
         "RECOMMENDED_TESTS": "Coach's Warning; 'What Parents Keep Telling Us' proof cut",
         "PRIORITY": "Medium", "STATUS": "Open"},
        {"GAP_NAME": "Youth Injury-Prevention Proof", "PRODUCT": "BodyShield / Head Guard", "ICP": "Youth",
         "CURRENT_INTERNAL_EVIDENCE_COUNT": a["parents_rows"], "GREAT_COUNT": a["great"],
         "BEST_INTERNAL_URLS": ";".join(a["best_urls"]),
         "EXISTING_EXTERNAL_REFERENCES": "physio / injury-prevention refs (off-domain, reference)",
         "CURRENT_CONFIDENCE": "None",
         "WHY_IT_MATTERS": "Injury-prevention is the strongest parent emotional trigger; unproven for Storelli.",
         "WHAT_PROOF_IS_MISSING": "no measured internal winner ties youth injury-prevention to a Storelli product.",
         "RECOMMENDED_TESTS": "Parent POV Hesitate; Back-to-School Safety Checklist",
         "PRIORITY": "High", "STATUS": "Open"},
    ]
    for g in gaps:
        g["GAP_ID"] = _gap_id(g["GAP_NAME"])
        g["AUDITED_AT"] = _now()
    return gaps


# ---------------------------------------------------------------------------
# Part C — evidence-building test plan (labelled tests, NOT proven ideas)
# ---------------------------------------------------------------------------
def evidence_building_tests() -> list[dict]:
    return [
        {"label": "evidence-building test", "test_name": "Parent POV: The Moment They Hesitate",
         "product": "BodyShield GK Leggings", "icp": "Parents",
         "hypothesis": "Parent-facing injury-prevention content builds more trust than a generic product demo.",
         "structure": "Parent fear → child contact/turf moment → protection explanation → confidence CTA.",
         "success_proves": "Parents/youth safety is a viable Storelli content pillar.",
         "failure_means": "The parent angle needs stronger product-demo proof, or isn't yet resonant.",
         "kpi_proxy": "saves + comment-likelihood inferred (comments not tracked yet)",
         "internal_proof_needed": "≥3 Great parent-POV cuts on one product to anchor a profile.",
         "external_refs": "coach-led youth-education refs (reference only)",
         "shootability": "High — one parent VO + one training clip.", "risk": "Avoid fear-mongering; keep it protective, not scary."},
        {"label": "evidence-building test", "test_name": "Before/After: The Turf-Burn a Parent Never Sees",
         "product": "BodyShield NoBurn GK Leggings", "icp": "Parents",
         "hypothesis": "A before/after protection reveal converts parents better than a talking-head explainer.",
         "structure": "Bare-knee turf burn → wince → protected replay in NoBurn → clarity CTA.",
         "success_proves": "Before/after is the winning parent format (leverages our 1 existing Great).",
         "failure_means": "Before/after doesn't travel to the parent buyer; try coach-trust framing.",
         "kpi_proxy": "conversion-fit / clarity (proxy)",
         "internal_proof_needed": "2–3 more Great before/after cuts for Parents on leggings.",
         "external_refs": "protection before/after gear-test refs (reference only)",
         "shootability": "High — reuses the proven before/after beat.", "risk": "Keep the injury tasteful; no graphic wounds."},
        {"label": "evidence-building test", "test_name": "Coach's Warning: What Every Youth Keeper Skips",
         "product": "BodyShield / Pants & Leggings", "icp": "Parents",
         "hypothesis": "A trusted coach voice lowers parent buying objections better than the brand voice.",
         "structure": "Coach authority → common youth mistake → protection fix → 'ask your keeper' CTA.",
         "success_proves": "Coach-trust is a repeatable parent conversion lever.",
         "failure_means": "Authority alone doesn't convert; parents need the pain moment first.",
         "kpi_proxy": "saves / shareability proxy",
         "internal_proof_needed": "≥3 Great coach-led parent cuts with a measured lift.",
         "external_refs": "coach-breaks-down-mistakes refs (reference only)",
         "shootability": "Medium — needs a credible coach on camera.", "risk": "Coach must be authentic, not scripted-salesy."},
        {"label": "evidence-building test", "test_name": "Head Guard Reaction: The Save That Scared Mom",
         "product": "ExoShield Head Guards", "icp": "Parents",
         "hypothesis": "A reaction-format head-impact moment proves Head Guard demand with parents.",
         "structure": "Curiosity gap → head-contact save → reaction → protection reveal → CTA.",
         "success_proves": "Head Guard has a parent pillar (leverages our 1 Great ExoShield cut).",
         "failure_means": "Head Guard interest is niche; refocus parent spend on leggings.",
         "kpi_proxy": "engagement proxy; comments inferred",
         "internal_proof_needed": "≥3 Great Head Guard parent cuts (currently ~1).",
         "external_refs": "reaction-format protective refs (reference only)",
         "shootability": "Medium — needs a real head-contact clip.", "risk": "Never imply concussion protection claims."},
        {"label": "evidence-building test", "test_name": "Back-to-School Safety Checklist",
         "product": "Youth Gear (BodyShield + Head Guard)", "icp": "Parents",
         "hypothesis": "A seasonal safety-checklist drives parent awareness at season start.",
         "structure": "Season-start hook → 3-item safety checklist → protection proof → shop CTA.",
         "success_proves": "Seasonal parent windows are a reliable awareness driver.",
         "failure_means": "Seasonality is weak; parent demand is year-round or product-led.",
         "kpi_proxy": "awareness-fit; saves proxy",
         "internal_proof_needed": "a measured seasonal lift vs a baseline parent cut.",
         "external_refs": "back-to-school / parent-advice refs (reference only)",
         "shootability": "High — checklist carousel or quick reel.", "risk": "Timeboxed; loses relevance off-season."},
    ]


def control_cut() -> dict:
    """The baseline the parent/coach angles are measured against — a generic
    product demo of the SAME product, with no parent/coach/fear framing."""
    return {"label": "control", "test_name": "CONTROL — The Demo: BodyShield GK Leggings",
            "product": "BodyShield GK Leggings", "icp": "Adult Amateur / General",
            "hypothesis": "Baseline generic product demo — the parent/coach cuts are measured against this.",
            "structure": "Product/benefit hook → demo dive/slide → protection proof → CTA.",
            "success_proves": "Baseline engagement for a non-parent demo of the same product.",
            "failure_means": "n/a — this is the control, not a hypothesis.",
            "kpi_proxy": "saves / conversion-fit (proxy)",
            "internal_proof_needed": "Tag as the non-Parents control; do NOT count it toward the Parents cluster.",
            "external_refs": "generic product-demo refs (reference only)",
            "shootability": "High — same keeper/location/length as the treatment cuts.",
            "risk": "Keep it truly generic (no parent/coach angle) so the A/B stays clean."}


_TRACKER_ANGLES = ["Parent POV", "Before/After", "Coach-Trust"]


def _tracker_row(t: dict, angle: str, is_control: str) -> dict:
    return {
        "TEST_ID": "PT-" + hashlib.sha1(t["test_name"].encode()).hexdigest()[:8],
        "TEST_NAME": t["test_name"], "ANGLE": angle, "IS_CONTROL": is_control,
        "PRODUCT": t["product"], "ICP": t["icp"], "HYPOTHESIS": t["hypothesis"],
        "STORY_STRUCTURE": t["structure"], "SUCCESS_PROVES": t["success_proves"],
        "KPI_PROXY": t["kpi_proxy"], "STATUS": "Planned",
        "SHOT_DATE": "", "POST_URL": "", "PERFORMANCE_GRADE": "", "SAVES_OR_KPI": "",
        "ENGAGEMENT_NOTE": "",
        "NOTES": ("Tag ICP=Parents on upload; stack ①② on BodyShield leggings to build the cluster."
                  if is_control == "FALSE"
                  else "Baseline: tag as the non-Parents control; measure the parent cuts against it."),
    }


def test_tracker_rows() -> list[dict]:
    """The 3 treatment cuts + the control, as tracker rows (results left blank)."""
    rows = [_tracker_row(t, _TRACKER_ANGLES[i], "FALSE")
            for i, t in enumerate(evidence_building_tests()[:3])]
    rows.append(_tracker_row(control_cut(), "Control (baseline)", "TRUE"))
    return rows


# Canonical string to log in the SAVES_OR_KPI column — keeps the 4 videos
# comparable. All rates are read at the SAME age and normalized by views.
KPI_LOG_FORMAT = "views=..; retention=..%; saves=..; shares=..; comments=..; parent_intent=.."


def logging_convention() -> dict:
    """The measurement protocol so PERFORMANCE_GRADE stays defensible and the
    parent cuts are comparable to the control. We don't auto-track engagement, so
    this is a manual, consistent hand-logging convention."""
    return {
        "window": "Read all 4 at the SAME age — 7 days after posting.",
        "same_platform": "Compare within one platform (IG vs IG, TikTok vs TikTok) — never across.",
        "format": KPI_LOG_FORMAT,
        "fields": [
            "views = total plays at 7 days",
            "retention% = avg watch time ÷ video length (IG 'watched full', TikTok avg watch)",
            "saves / shares / comments = raw counts (normalize by views when comparing)",
            "parent_intent = # comments showing parent/buyer intent (\"where do I buy for my son\", fit/size Qs)",
        ],
        "primary_kpi": {
            "Parent POV": "saves/1k + parent_intent (comment-likelihood proxy)",
            "Before/After": "saves/1k + profile/link taps (conversion-fit)",
            "Coach-Trust": "shares/1k (shareability proxy)",
            "Control": "saves/1k (its own baseline)",
        },
        "grade_rule": [
            "Great = beats the CONTROL ≥1.3× on its primary KPI AND retention ≥ control",
            "Ok = within ±30% of the control",
            "Underdog = clearly below the control",
        ],
        "note": "Normalize by views (per-1,000) before comparing — reach differs between cuts.",
    }


def render_logging_convention(mode: str = None) -> str:
    c = logging_convention()
    lines = ["*How to log the 4 Parents tests* (so grades stay comparable):", "",
             f"• *Window:* {c['window']}",
             f"• *Same platform:* {c['same_platform']}",
             f"• *Log into SAVES_OR_KPI as:* `{c['format']}`",
             "  " + "; ".join(c["fields"][1:3]), "",
             "*Primary KPI per cut:*"]
    lines += [f"• {k} → {v}" for k, v in c["primary_kpi"].items()]
    lines += ["", "*Grade vs the control:*"] + [f"• {g}" for g in c["grade_rule"]]
    lines += ["", f"_{c['note']}_"]
    return st.compact_slack_response("\n".join(lines), mode or st.MODE_DEEP)


def seed_test_tracker(sheets: Optional[InspirationSheets] = None) -> tuple[int, int]:
    """Create-if-absent seed of the PARENTS_EVIDENCE_TESTS tab. Never overwrites
    logged results. Returns (created, skipped_existing)."""
    s = sheets or InspirationSheets()
    try:
        s.ensure_evidence_test_tracker_tab()
        return s.seed_evidence_tests(test_tracker_rows())
    except Exception as e:  # noqa: BLE001
        log.warning("evidence test tracker seed failed: %s", e)
        return 0, 0


# ---------------------------------------------------------------------------
# Part D — Slack answers (disciplined, source-linked trace)
# ---------------------------------------------------------------------------
_PROOF_KW = ("what proof are we missing", "proof are we missing", "what proof do we",
             "evidence gap", "evidence-building", "create proof", "how do we create proof",
             "what would we need to prove", "before scaling", "before we scale")


def is_evidence_gap_query(text: str, context: Optional[list] = None) -> bool:
    t = (text or "").lower()
    if any(k in t for k in _PROOF_KW):
        return True
    if _wants_logging(t) and any(k in t for k in ("parent", "youth", "test", "tests")):
        return True
    if any(k in t for k in ("parent", "youth")) and any(
            k in t for k in ("content", "test", "tests", "proof", "scale", "scaling",
                             "make", "should we", "pillar", "evidence", "prove")):
        return True
    return False


def _wants_logging(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ("how do we log", "how to log", "logging convention",
                                "how do we measure", "how to measure", "how do we grade",
                                "how to grade", "what do we record", "track results",
                                "measure the parents", "log the parents", "kpi convention"))


def _wants_test_plan(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ("test", "tests", "how do we create proof", "how do we prove",
                                "what would we need to prove", "run"))


def _load_audit(sheets=None) -> tuple[dict, list, str]:
    """Load real internal evidence for a live answer. Read-only."""
    try:
        from sheets_client import SheetsClient
        internal = SheetsClient().read_rows()
    except Exception as e:  # noqa: BLE001
        log.warning("evidence audit: internal rows unavailable: %s", e)
        internal = []
    s = sheets or InspirationSheets()
    profiles = _safe(s, "read_profiles")
    calendar = _safe(s, "read_calendar_ratings")
    learnings = _read_learnings()
    return audit(internal, profiles, calendar, learnings), internal, learnings


def _safe(sheets, name):
    try:
        return getattr(sheets, name)()
    except Exception:  # noqa: BLE001
        return []


def _read_learnings() -> str:
    try:
        import synthesizer
        if os.path.exists(synthesizer.LEARNINGS_PATH):
            with open(synthesizer.LEARNINGS_PATH, encoding="utf-8") as fh:
                return fh.read()
    except Exception:  # noqa: BLE001
        pass
    return ""


def answer_evidence_gap(text: str, context: Optional[list] = None, sheets=None) -> Optional[str]:
    mode = st.detect_response_mode(text)
    if _wants_logging(text):
        return render_logging_convention(mode)
    a, _internal, _learn = _load_audit(sheets)
    if _wants_test_plan(text):
        return _render_test_plan(a, mode)
    return _render_verdict(a, mode)


def _render_verdict(a: dict, mode: str) -> str:
    justified, reason = profile_justified(a)
    s_rows, refs = [], []
    for i, u in enumerate(a.get("best_urls", [])[:2], 1):
        tag = f"S{i}"
        s_rows.append((tag, u, "Storelli internal proof — Parents 'Great'"))
        refs.append(tag)
    steps = [
        dt.step("Internal proof", f"{a['great']} Great of {a['parents_rows']} Parents rows — too thin",
                refs, "internal", "Thin"),
        dt.step("External refs", "useful, not proof", [], "external", "Medium"),
        dt.step("Gap", "no Parents winning profile" if not a["has_parents_profile"] else "profile emerging",
                [], "risk", "Thin"),
        dt.step("Test needed", "parent-safety proof angle", [], "verdict", "Medium"),
    ]
    lead = ("Parents/youth is interesting, but not proven yet."
            if not justified else "Parents/youth is emerging — close to a profile, verify with tests.")
    move = "Run 3 Parents/youth evidence-building tests before scaling the pillar."
    sources = st.compact_sources(s_rows)
    return st.render_ceo_summary(lead, why=dt.bullets(steps), move=move,
                                 sources=(f"{sources}\n{_NOT_PROOF}" if sources else _NOT_PROOF), mode=mode)


def _render_test_plan(a: dict, mode: str) -> str:
    tests = evidence_building_tests()[:3]
    lead = "Before scaling Parents, run these evidence-building *tests* (not proven ideas):"
    why = [f"*Test {i}:* {t['test_name']} — proves: {dt._trim_value(t['success_proves'], 12)}"
           for i, t in enumerate(tests, 1)]
    move = ("Shoot the Parent POV test first; a Parents winning profile is justified only if "
            "≥3 Great cluster on one product.")
    s_rows = [(f"S{i}", u, "Storelli internal proof — Parents 'Great'")
              for i, u in enumerate(a.get("best_urls", [])[:2], 1)]
    sources = st.compact_sources(s_rows)
    return st.render_ceo_summary(lead, why=why, move=move,
                                 sources=(f"{sources}\n{_NOT_PROOF}" if sources else _NOT_PROOF),
                                 mode=st.MODE_DEEP)


# ---------------------------------------------------------------------------
# audit orchestrator (writes ONLY the EVIDENCE_GAPS artifact) — CLI entry
# ---------------------------------------------------------------------------
def run_audit(sheets: Optional[InspirationSheets] = None, write: bool = True,
              internal_rows: Optional[list] = None) -> dict:
    """Audit internal Parents/youth evidence and write the EVIDENCE_GAPS artifact.
    NEVER writes profiles or internal Storelli rows. `internal_rows` is injectable
    for tests; otherwise the internal Storelli sheet is read (read-only)."""
    s = sheets or InspirationSheets()
    if internal_rows is None:
        a, internal, _learn = _load_audit(s)
    else:
        internal = internal_rows
        a = audit(internal, _safe(s, "read_profiles"), _safe(s, "read_calendar_ratings"),
                  _read_learnings())
    ext_use = 0
    try:
        ext_use = sum(1 for r in s.read_content_rows()
                      if str(r.get("USE_FOR_IDEA_GEN", "")).strip().upper() == "TRUE")
    except Exception:  # noqa: BLE001
        pass
    gaps = evidence_gaps(a, internal, ext_use)
    created = updated = 0
    tracker_created = tracker_existing = 0
    if write:
        try:
            s.ensure_evidence_gaps_tab()
            created, updated = s.upsert_evidence_gaps(gaps)
        except Exception as e:  # noqa: BLE001
            log.warning("evidence gaps write failed: %s", e)
        tracker_created, tracker_existing = seed_test_tracker(s)   # create-if-absent
    justified, reason = profile_justified(a)
    return {"audit": a, "gaps": gaps, "tests": evidence_building_tests(),
            "profile_justified": justified, "reason": reason,
            "created": created, "updated": updated,
            "tracker_created": tracker_created, "tracker_existing": tracker_existing}


# ---------------------------------------------------------------------------
# Coverage ranking — which slices we actually have evidence for.
#
# This answers a family of questions the brain previously fell through on:
# "which product do we have the least evidence for?", "what does this account
# never make?", "if we had to double down on one product, which?". The key
# distinction it preserves is NO DATA (never tried) vs LOW SAMPLE (tried, too
# few to read) vs ACTIVELY WEAK (tried enough, and it underperforms) — collapsing
# those three into one "gap" is how a brain talks people out of untested ideas
# and into ones that already failed.
# ---------------------------------------------------------------------------
NO_DATA = "NO DATA"
LOW_SAMPLE = "LOW SAMPLE"
ACTIVELY_WEAK = "ACTIVELY WEAK"
SUPPORTED_SLICE = "SUPPORTED"

_MIN_READABLE = 4          # below this a slice cannot be read at all


def _slice_state(total: int, positives: int) -> str:
    if total == 0:
        return NO_DATA
    if total < _MIN_READABLE:
        return LOW_SAMPLE
    if positives == 0:
        return ACTIVELY_WEAK
    return SUPPORTED_SLICE


def _field_value(row: dict, field: str) -> str:
    """Read a column case-insensitively — the POC header uses 'Product'/'ICP'
    while callers name them PRODUCT/ICP."""
    want = field.strip().lower()
    for k, v in row.items():
        if str(k).strip().lower() == want:
            return _norm(v)
    return ""


def canonical_slices(value: str) -> list:
    """Split a free-text Product/ICP cell into canonical slice names.

    The POC field is hand-typed, so the same product arrives as "ExoShield Head
    Guard" and "ExoShield Head Guards", and one row often names two products
    ("BodyShield GK 3/4 Undershirt and BodyShield NoBurn GK Leggings"). Treating
    those as distinct single-post slices makes every product look like n=1 and
    understates evidence we actually have — so aggregate on a canonical form, and
    count a multi-product row toward each product it names (it IS evidence for
    both).
    """
    raw = _norm(value)
    if not raw:
        return []
    # NB: a slash between digits is a size ("GK 3/4 Leggings"), not a list
    # separator — splitting there invents a product called "BodyShield GK 3".
    parts = [p for p in re.split(r"\s*(?:,|;|\band\b|\+|&|(?<!\d)/(?!\d))\s*", raw)
             if p.strip()]
    out, seen = [], set()
    for part in parts:
        words = part.strip().split()
        if not words:
            continue
        # de-pluralize the final word so Guard/Guards collapse
        if len(words[-1]) > 3 and words[-1].lower().endswith("s") \
                and not words[-1].lower().endswith("ss"):
            words[-1] = words[-1][:-1]
        name = " ".join(w for w in words)
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def coverage_by(rows: list, field: str, universe=None) -> list:
    """Evidence coverage per distinct value of `field` (e.g. PRODUCT, ICP).

    `universe` optionally lists values we EXPECT to exist, so a value with no
    rows at all is reported as NO DATA instead of silently missing from the
    ranking. Returns dicts sorted weakest-evidence-first.
    """
    import performance
    buckets = performance.buckets_for_rows(rows)
    tally: dict = {}
    for r in rows:
        names = canonical_slices(_field_value(r, field))
        positive = performance.is_positive(buckets.get(r.get("_row"), ""))
        for key in (names or ["(unspecified)"]):
            t = tally.setdefault(key, {"slice": key, "total": 0, "positive": 0})
            t["total"] += 1
            if positive:
                t["positive"] += 1
    for expected in (universe or []):
        tally.setdefault(_norm(expected), {"slice": _norm(expected), "total": 0,
                                           "positive": 0})
    out = []
    for t in tally.values():
        t["state"] = _slice_state(t["total"], t["positive"])
        out.append(t)
    order = {NO_DATA: 0, LOW_SAMPLE: 1, ACTIVELY_WEAK: 2, SUPPORTED_SLICE: 3}
    return sorted(out, key=lambda t: (order[t["state"]], t["total"], t["slice"]))


_COVERAGE_KW = ("least evidence", "most evidence", "never make", "never made",
                "never tried", "not making", "double down", "least data",
                "thinnest", "best covered", "worst covered", "least tested",
                "coverage")


def is_coverage_query(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    if any(k in t for k in _COVERAGE_KW):
        return True
    return ("evidence" in t or "data" in t) and any(
        k in t for k in ("which product", "which icp", "which audience",
                         "least", "most", "weakest", "strongest"))


def answer_coverage(text: str, sheets=None) -> Optional[str]:
    """Rank slices by how much evidence we actually have, weakest first."""
    import taxonomy
    from sheets_client import SheetsClient
    t = (text or "").lower()
    sc = sheets or SheetsClient()
    sc.validate_columns()
    rows = [r for r in sc.read_rows() if SheetsClient.is_analyzed(r)]
    if not rows:
        return None
    field = "ICP" if ("icp" in t or "audience" in t or "who" in t) else "PRODUCT"
    ranked = coverage_by(rows, field)
    if not ranked:
        return None
    wants_strongest = any(k in t for k in ("most evidence", "double down", "best covered",
                                          "strongest"))
    label = field.lower()

    if wants_strongest:
        best = [r for r in ranked if r["state"] == SUPPORTED_SLICE]
        if not best:
            return (f"Nothing has enough evidence to double down on yet — every "
                    f"{label} we've analyzed is still under {_MIN_READABLE} readable "
                    f"posts or has no positive result. I'd rather we build the sample "
                    f"first than commit on this.")
        pick = max(best, key=lambda r: (r["positive"], r["total"]))
        runner = [r for r in best if r["slice"] != pick["slice"]]
        parts = [f"*{pick['slice']}* — it has the most evidence behind it: "
                 f"{pick['positive']} strong of {pick['total']} analyzed posts."]
        if runner:
            r2 = max(runner, key=lambda r: (r["positive"], r["total"]))
            parts.append(f"*Closest alternative:* {r2['slice']} "
                         f"({r2['positive']} of {r2['total']}).")
        parts.append("Associated with performance, not causal — and it reflects what "
                     "we've *shot*, not everything that could work.")
        return " ".join(parts)

    real = [r for r in ranked if r["slice"] != "(unspecified)"] or ranked
    ranked = real
    weakest = ranked[0]
    lines = [f"Weakest evidence by {label}:"]
    for r in ranked[:5]:
        if r["state"] == NO_DATA:
            note = "no posts at all — untested, not disproven"
        elif r["state"] == LOW_SAMPLE:
            note = (f"{r['total']} post(s) — too few to read either way")
        elif r["state"] == ACTIVELY_WEAK:
            note = (f"{r['total']} posts, none landed — this one we've actually "
                    f"tried and it underperformed")
        else:
            note = f"{r['positive']} strong of {r['total']} — readable"
        lines.append(f"  • *{r['slice']}* — {r['state']}: {note}")
    lines.append(f"The one I'd fix first is *{weakest['slice']}* — "
                 + ("we've never shot it, so there's nothing to learn from yet."
                    if weakest["state"] == NO_DATA
                    else "the sample is too thin to trust in either direction."))
    lines.append("_NO DATA and ACTIVELY WEAK are different problems: the first "
                 "needs a first test, the second needs a different approach._")
    return "\n".join(lines)


def coverage_by_signal(rows: list, layer: Optional[str] = None) -> list:
    """Evidence coverage per TAXONOMY OPTION (hook, format, visual style, ...).

    Unlike product coverage this has a real universe: every option the taxonomy
    defines. So an option with zero posts is genuinely NEVER MADE — territory we
    have not tested — which is a different fact from an option we shot several
    times that never landed.
    """
    import performance
    import taxonomy
    buckets = performance.buckets_for_rows(rows)
    out = []
    layers = {layer: taxonomy.LAYERS[layer]} if layer in taxonomy.LAYERS else taxonomy.LAYERS
    for lyr, options in layers.items():
        for opt in options:
            col = taxonomy.column_for(lyr, opt)
            total = positive = 0
            for r in rows:
                if str(r.get(col, "")).strip() != "1":
                    continue
                total += 1
                if performance.is_positive(buckets.get(r.get("_row"), "")):
                    positive += 1
            out.append({"slice": opt, "layer": lyr, "total": total,
                        "positive": positive, "state": _slice_state(total, positive)})
    order = {NO_DATA: 0, LOW_SAMPLE: 1, ACTIVELY_WEAK: 2, SUPPORTED_SLICE: 3}
    return sorted(out, key=lambda t: (order[t["state"]], t["total"], t["slice"]))


_NEVER_KW = ("never make", "never made", "never tried", "never shot", "not making",
             "haven't tried", "havent tried", "have we never", "untested territory")


def is_never_made_query(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    return any(k in t for k in _NEVER_KW)


def answer_never_made(text: str, sheets=None) -> Optional[str]:
    """What territory we have genuinely never shot — kept strictly separate from
    what we shot and it failed."""
    from sheets_client import SheetsClient
    sc = sheets or SheetsClient()
    sc.validate_columns()
    rows = [r for r in sc.read_rows() if SheetsClient.is_analyzed(r)]
    if not rows:
        return None
    cov = coverage_by_signal(rows)
    absent = [c for c in cov if c["state"] == NO_DATA]
    weak = [c for c in cov if c["state"] == ACTIVELY_WEAK]
    thin = [c for c in cov if c["state"] == LOW_SAMPLE]
    lines = []
    if absent:
        names = ", ".join(f"*{c['slice']}* ({c['layer'].replace('_', ' ')})"
                          for c in absent[:6])
        lines.append(f"Never shot at all — {len(absent)} option(s) in the taxonomy "
                     f"have zero posts: {names}. That's untested, not disproven.")
    else:
        lines.append("Nothing in the taxonomy is completely untouched — every hook, "
                     "format and style has at least one post. So the honest answer is "
                     "that we don't have absent territory, we have *thin* territory.")
    if thin:
        names = ", ".join(f"{c['slice']} ({c['total']})" for c in thin[:5])
        lines.append(f"*Barely tested* (under {_MIN_READABLE} posts, unreadable either "
                     f"way): {names}.")
    if weak:
        names = ", ".join(f"{c['slice']} ({c['total']} posts, none landed)"
                          for c in weak[:5])
        lines.append(f"*Different problem — tried and it didn't work:* {names}. "
                     f"Re-testing these the same way is not a gap to fill.")
    lines.append("_Untested and failed look identical in a gap list and need opposite "
                 "responses — the first needs a first attempt, the second needs a new "
                 "angle or a decision to stop._")
    return "\n\n".join(lines)


def _named_taxonomy_options(text: str) -> list:
    import taxonomy

    def norm(v):
        return re.sub(r"\s*([/&+-])\s*", r"\1",
                      re.sub(r"\s+", " ", str(v).strip().lower()))

    hay = norm(text)
    found = []
    for lyr, options in taxonomy.LAYERS.items():
        for opt in options:
            if len(norm(opt)) >= 3 and re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(norm(opt))}(?![A-Za-z0-9])", hay):
                found.append((lyr, opt))
    return found


_DURATION_KW = ("longer", "shorter", "duration", "how long", "length")


def is_comparison_query(text: str) -> bool:
    """Cheap text-only test — must be true before we touch the Sheet."""
    t = " " + (text or "").lower() + " "
    comparative = any(k in t for k in ("compare", " vs ", "versus", "against",
                                       "beat", "better", "worse", "outperform"))
    if not comparative:
        return False
    return len(_named_taxonomy_options(t)) >= 2 or any(k in t for k in _DURATION_KW)


def compare_slices(text: str, sheets=None) -> Optional[str]:
    """Head-to-head on a named dimension: 'Raw/UGC vs Polished', 'longer vs
    shorter reels'. Reports the direction if the samples support one, and says
    plainly when they don't — 'too close to call on this sample' is a real answer,
    'both are good' is not."""
    from sheets_client import SheetsClient
    t = (text or "").lower()
    if not is_comparison_query(t):
        return None
    sc = sheets or SheetsClient()
    sc.validate_columns()
    rows = [r for r in sc.read_rows() if SheetsClient.is_analyzed(r)]
    if not rows:
        return None
    named = _named_taxonomy_options(t)
    if len(named) >= 2:
        cov = {(c["layer"], c["slice"]): c for c in coverage_by_signal(rows)}
        picked = [cov[k] for k in named[:2] if k in cov]
        if len(picked) < 2:
            return None
        return _render_head_to_head(picked[0], picked[1])

    if any(k in t for k in ("longer", "shorter", "duration", "length", "how long")):
        return _compare_duration(rows)
    return None


def _rate(c) -> Optional[float]:
    return (c["positive"] / c["total"]) if c["total"] else None


def _render_head_to_head(a: dict, b: dict) -> str:
    ra, rb = _rate(a), _rate(b)
    head = (f"*{a['slice']}*: {a['positive']} strong of {a['total']} · "
            f"*{b['slice']}*: {b['positive']} strong of {b['total']}.")
    if a["total"] < _MIN_READABLE or b["total"] < _MIN_READABLE:
        thin = a if a["total"] <= b["total"] else b
        rich = b if thin is a else a
        if thin["total"] == 0:
            return (f"{head} We don't have a single {thin['slice']} post, so there's "
                    f"nothing to compare it against — too few to read a direction, and "
                    f"that's an untested gap rather than a verdict. If you want the "
                    f"comparison, {thin['slice']} needs shooting at least "
                    f"{_MIN_READABLE} times; {rich['slice']} is already readable.")
        return (f"{head} I can't tell them apart on this sample — {thin['slice']} has "
                f"only {thin['total']} post(s), too few to read a direction from. "
                f"Not a tie, just not measurable yet: a few more {thin['slice']} posts "
                f"and it becomes answerable.")
    lead, trail = (a, b) if (ra or 0) >= (rb or 0) else (b, a)
    gap = abs((ra or 0) - (rb or 0))
    if gap < 0.10:
        return (f"{head} Too close to call — {gap * 100:.0f} points apart on samples this "
                f"size is noise, so I wouldn't choose between them on the data. "
                f"Pick on production cost instead.")
    return (f"{head} *{lead['slice']}* is ahead — {(_rate(lead) or 0) * 100:.0f}% of its "
            f"posts landed versus {(_rate(trail) or 0) * 100:.0f}% for {trail['slice']}. "
            f"Associated with performance, not causal, and it reflects the posts we "
            f"happened to shoot.")


def _compare_duration(rows: list) -> Optional[str]:
    """Longer vs shorter, using the real DURATION_SECONDS values we have."""
    import performance
    buckets = performance.buckets_for_rows(rows)
    have = []
    for r in rows:
        raw = _field_value(r, "DURATION_SECONDS") or _field_value(r, "duration seconds")
        try:
            secs = float(re.sub(r"[^\d.]", "", raw))
        except (TypeError, ValueError):
            continue
        if secs > 0:
            have.append((secs, performance.is_positive(buckets.get(r.get("_row"), ""))))
    if len(have) < 2 * _MIN_READABLE:
        return (f"I can't answer that yet — only {len(have)} analyzed post(s) have a "
                f"duration recorded, which isn't enough to compare long against short. "
                f"Duration now arrives automatically on new posts, so this becomes "
                f"answerable as the library fills in.")
    have.sort()
    mid = len(have) // 2
    short, long_ = have[:mid], have[mid:]
    cut = have[mid][0]
    s_rate = sum(1 for _, p in short if p) / len(short)
    l_rate = sum(1 for _, p in long_ if p) / len(long_)
    head = (f"Split at {cut:.0f}s: under it, {sum(1 for _, p in short if p)} strong of "
            f"{len(short)}; over it, {sum(1 for _, p in long_ if p)} of {len(long_)}.")
    if abs(s_rate - l_rate) < 0.10:
        return (f"{head} No real difference — length isn't what separates our posts, "
                f"so I'd choose duration on what the idea needs, not on this data.")
    winner = "Shorter" if s_rate > l_rate else "Longer"
    return (f"{head} {winner} reels do better for us "
            f"({max(s_rate, l_rate) * 100:.0f}% vs {min(s_rate, l_rate) * 100:.0f}%). "
            f"Associated with performance, not causal.")
