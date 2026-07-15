"""Milestone — Notion Content Calendar idea rating.

Rates proposed Notion calendar ideas against Storelli INTERNAL evidence (active
winning profiles), EXTERNAL inspiration (reference only), and existing rated
ideas. Scores are deterministic and reproducible; an optional LLM adds narrative
(rationale / revision suggestion) but never the numbers. Ratings are stored in
the CONTENT_CALENDAR_IDEA_RATINGS Google Sheet tab — nothing is written back to
Notion, and no internal Storelli rows are touched.

External inspiration is a creative reference only: its view/follower counts are
NEVER used in scoring (only INSPIRATION_QUALITY_SCORE, a capped 0-100 signal),
so high external views alone cannot lift a weak idea.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

from idea_generator import copyright_recheck
from inspiration_scanner import _finalize_and_log_run, _new_run
from inspiration_sheets import InspirationSheets
from logger import get_logger

log = get_logger()

# CALENDAR_IDEA_SCORE weights (sum = 1.0).
W_EVIDENCE = 0.25
W_INSPIRATION = 0.20
W_PRODICP = 0.15      # avg(product_fit, icp_fit)
W_HOOKFMT = 0.15      # avg(hook_strength, format_fit)
W_SHOOT = 0.10
W_NOVELTY = 0.10
W_COPYRIGHT = 0.05

_GENERIC = ("game changer", "game-changer", "dominate", "unleash", "unbreakable",
            "inner keeper", "zero hesitation", "secret", "ultimate", "insane")
_VIDEO_FORMATS = ("reel", "short", "story", "tiktok", "video", "carousel")


def _num(v, default=0.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _clamp(v):
    return max(0.0, min(100.0, v))


# ---------------------------------------------------------------------------
# matching to internal profiles / inspiration / existing ideas
# ---------------------------------------------------------------------------
_LEGGINGS = ("bodyshield", "leggings", "pants", "sliders", "leg")
_GLOVES = ("glove",)


def _family(text: str) -> str:
    t = str(text or "").lower()
    if any(k in t for k in _LEGGINGS):
        return "leggings"
    if any(k in t for k in _GLOVES):
        return "gloves"
    return ""


def _tokens(text: str) -> set:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower()).split() if len(w) > 3}


def closest_profile(item: dict, profiles: list[dict]) -> tuple[Optional[dict], float]:
    """Best active profile for this calendar item + a 0..1 fit. Product family
    match dominates; ICP and keyword overlap refine."""
    actives = [p for p in profiles
               if str(p.get("ACTIVE", "")).strip().lower() == "true"
               and str(p.get("CONFIDENCE", "")).strip().lower() in ("medium", "high")]
    if not actives:
        return None, 0.0
    blob = f"{item.get('title', '')} {item.get('notes', '')} {item.get('product', '')} {item.get('icp', '')}"
    fam = _family(blob)
    best, best_fit = None, 0.0
    for p in actives:
        fit = 0.0
        if fam and fam == _family(p.get("PRODUCT", "")):
            fit += 0.6
        if item.get("icp") and item["icp"].lower() in str(p.get("ICP", "")).lower():
            fit += 0.2
        overlap = _tokens(blob) & _tokens(f"{p.get('HOOK_TAGS','')} {p.get('FORMAT_TAGS','')} "
                                          f"{p.get('PROBLEM_TAGS','')} {p.get('SOLUTION_TAGS','')}")
        fit += min(0.2, 0.05 * len(overlap))
        if fit > best_fit:
            best, best_fit = p, fit
    return best, round(best_fit, 3)


def matching_inspiration(item: dict, inspiration: list[dict], k: int = 3) -> list[dict]:
    """Top eligible inspiration references for this item (quality>=80, USE, Safe,
    Analyzed), same family, ranked by INSPIRATION_QUALITY_SCORE (NOT views)."""
    fam = _family(f"{item.get('title','')} {item.get('notes','')} {item.get('product','')}")
    elig = [r for r in inspiration
            if str(r.get("SAFETY_STATUS", "")).strip().lower() == "safe"
            and str(r.get("ANALYSIS_STATUS", "")).strip().lower() == "analyzed"
            and str(r.get("USE_FOR_IDEA_GEN", "")).strip().upper() == "TRUE"
            and _num(r.get("INSPIRATION_QUALITY_SCORE")) >= 80]
    if fam:
        same = [r for r in elig if _family(str(r.get("BEST_MATCHED_PROFILE_NAME", "")) or
                                            str(r.get("SUBCATEGORY", ""))) == fam]
        elig = same or elig
    elig.sort(key=lambda r: _num(r.get("INSPIRATION_QUALITY_SCORE")), reverse=True)
    return elig[:k]


def closest_rated_idea(item: dict, ideas: list[dict]) -> Optional[dict]:
    fam = _family(f"{item.get('title','')} {item.get('notes','')} {item.get('product','')}")
    pool = [i for i in ideas if not fam or _family(i.get("PRODUCT", "")) == fam]
    if not pool:
        return None
    toks = _tokens(f"{item.get('title','')} {item.get('notes','')}")
    def sim(i):
        it = _tokens(f"{i.get('IDEA_TITLE','')} {i.get('REFINED_IDEA_TITLE','')} {i.get('CONCEPT','')}")
        return len(toks & it)
    return max(pool, key=sim)


# ---------------------------------------------------------------------------
# deterministic sub-scores
# ---------------------------------------------------------------------------
def internal_evidence_fit(profile: Optional[dict], fit01: float) -> float:
    if not profile:
        return 35.0
    base = 92.0 if str(profile.get("CONFIDENCE", "")).lower() == "high" else 80.0
    return round(_clamp(35 + (base - 35) * min(1.0, 0.4 + fit01)), 1)


def inspiration_alignment(refs: list[dict]) -> float:
    if not refs:
        return 30.0
    return round(sum(_num(r.get("INSPIRATION_QUALITY_SCORE")) for r in refs) / len(refs), 1)


def _has_specific_signal(text: str) -> bool:
    t = str(text or "").lower()
    return bool(re.search(r"\d", t) or "?" in t or "how to" in t or "myth" in t
                or "mistake" in t or "vs" in t or ":" in t)


def hook_strength(item: dict) -> float:
    title = str(item.get("title", ""))
    s = 45.0
    if _has_specific_signal(title):
        s += 25
    if 15 <= len(title.strip()) <= 90:
        s += 15
    if any(g in title.lower() for g in _GENERIC):
        s -= 25
    return round(_clamp(s), 1)


def format_fit(item: dict) -> float:
    fmt = f"{item.get('platform','')} {item.get('asset_format','')}".lower()
    if any(v in fmt for v in _VIDEO_FORMATS):
        return 85.0
    if fmt.strip():
        return 60.0
    return 45.0


def shootability(item: dict) -> float:
    notes = str(item.get("notes", "")).strip()
    s = 40.0
    if len(notes) >= 40:
        s += 30
    elif len(notes) >= 12:
        s += 15
    if any(v in f"{item.get('asset_format','')}".lower() for v in _VIDEO_FORMATS):
        s += 10
    return round(_clamp(s), 1)


def product_icp_fit(item: dict, profile: Optional[dict]) -> tuple[float, float]:
    pf = 85.0 if (item.get("product") and profile) else 65.0 if item.get("product") else 45.0
    icf = 85.0 if (item.get("icp") and profile) else 60.0 if item.get("icp") else 45.0
    return pf, icf


def novelty(item: dict, closest_idea: Optional[dict]) -> float:
    if not closest_idea:
        return 70.0
    toks = _tokens(f"{item.get('title','')} {item.get('notes','')}")
    it = _tokens(f"{closest_idea.get('IDEA_TITLE','')} {closest_idea.get('CONCEPT','')}")
    if not toks:
        return 55.0
    overlap = len(toks & it) / max(1, len(toks))
    return round(_clamp(80 - overlap * 60), 1)   # very similar -> lower novelty


def calendar_idea_score(evidence, inspiration, product_fit, icp_fit, hook, fmt,
                        shoot, nov, copyright_safety) -> float:
    return round(
        W_EVIDENCE * evidence + W_INSPIRATION * inspiration
        + W_PRODICP * (product_fit + icp_fit) / 2
        + W_HOOKFMT * (hook + fmt) / 2
        + W_SHOOT * shoot + W_NOVELTY * nov + W_COPYRIGHT * copyright_safety, 1)


def recommendation(score: float, has_content: bool, copyright_safe: bool) -> str:
    if not has_content:
        return "Needs More Info"
    if not copyright_safe:
        return "Reject"
    if score >= 72:
        return "Keep"
    if score >= 55:
        return "Revise"
    return "Reject"


def strategic_priority(score: float, profile: Optional[dict]) -> float:
    bump = 8 if (profile and str(profile.get("CONFIDENCE", "")).lower() == "high") else 0
    return round(_clamp(score + bump), 1)


# ---------------------------------------------------------------------------
# per-item rating
# ---------------------------------------------------------------------------
def _rating_id(item: dict) -> str:
    basis = f"{item.get('page_id','')}|{item.get('title','')}|{item.get('notes','')}"
    return "CR-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def rate_item(item: dict, profiles: list[dict], inspiration: list[dict],
              ideas: list[dict], gemini=None) -> dict:
    text = f"{item.get('title','')} {item.get('notes','')}".strip()
    has_content = len(re.sub(r"[^a-z0-9]", "", text.lower())) >= 8
    copyright_safe, why = copyright_recheck(text)

    profile, fit01 = closest_profile(item, profiles)
    refs = matching_inspiration(item, inspiration)
    close_idea = closest_rated_idea(item, ideas)

    evidence = internal_evidence_fit(profile, fit01)
    inspiration_score = inspiration_alignment(refs)
    pf, icf = product_icp_fit(item, profile)
    hook = hook_strength(item)
    fmt = format_fit(item)
    shoot = shootability(item)
    nov = novelty(item, close_idea)
    copyright_safety = 100.0 if copyright_safe else 15.0
    score = calendar_idea_score(evidence, inspiration_score, pf, icf, hook, fmt,
                                shoot, nov, copyright_safety)
    rec = recommendation(score, has_content, copyright_safe)
    prio = strategic_priority(score, profile)

    rationale, revision, risk = _narrative(item, profile, refs, close_idea, rec,
                                           copyright_safe, why, gemini)

    internal_urls = ";".join(u for u in str(profile.get("SUPPORTING_VIDEO_URLS", "") if profile else "").split(";") if u.strip())
    external_urls = ";".join(str(r.get("POST_URL", "")).strip() for r in refs if str(r.get("POST_URL", "")).strip())

    return {
        "RATING_ID": _rating_id(item), "RATED_AT": _now_iso(),
        "NOTION_PAGE_ID": item.get("page_id", ""), "NOTION_PAGE_URL": item.get("url", ""),
        "CALENDAR_TITLE": item.get("title", ""), "CALENDAR_STATUS": item.get("status", ""),
        "PLATFORM": item.get("platform", ""), "PRODUCT": item.get("product", ""),
        "ICP": item.get("icp", ""), "PROPOSED_IDEA_TEXT": text[:1500],
        "HAS_CAMERA_EMOJI": "TRUE" if item.get("has_camera_emoji") else "FALSE",
        "SHOULD_RATE": "TRUE", "EXCLUSION_REASON": "",
        "CLOSEST_WINNING_PROFILE_ID": profile.get("PROFILE_ID", "") if profile else "",
        "CLOSEST_WINNING_PROFILE_NAME": profile.get("PROFILE_NAME", "") if profile else "",
        "CLOSEST_RATED_IDEA_ID": close_idea.get("IDEA_ID", "") if close_idea else "",
        "CLOSEST_RATED_IDEA_TITLE": (close_idea.get("REFINED_IDEA_TITLE")
                                     or close_idea.get("IDEA_TITLE", "")) if close_idea else "",
        "INTERNAL_EVIDENCE_URLS": internal_urls,
        "EXTERNAL_REFERENCE_URLS": external_urls,
        "CALENDAR_IDEA_SCORE": score, "INTERNAL_EVIDENCE_FIT_SCORE": evidence,
        "INSPIRATION_ALIGNMENT_SCORE": inspiration_score, "PRODUCT_FIT_SCORE": pf,
        "ICP_FIT_SCORE": icf, "HOOK_STRENGTH_SCORE": hook, "FORMAT_FIT_SCORE": fmt,
        "SHOOTABILITY_SCORE": shoot, "NOVELTY_SCORE": nov,
        "COPYRIGHT_SAFETY_SCORE": copyright_safety, "STRATEGIC_PRIORITY_SCORE": prio,
        "RECOMMENDATION": rec, "RATIONALE": rationale[:1200],
        "REVISION_SUGGESTION": revision[:800], "RISK_NOTES": risk[:400],
        "REVIEW_STATUS": "Rated",
    }


def excluded_row(item: dict, reason: str) -> dict:
    text = f"{item.get('title','')} {item.get('notes','')}".strip()
    return {
        "RATING_ID": _rating_id(item), "RATED_AT": _now_iso(),
        "NOTION_PAGE_ID": item.get("page_id", ""), "NOTION_PAGE_URL": item.get("url", ""),
        "CALENDAR_TITLE": item.get("title", ""), "CALENDAR_STATUS": item.get("status", ""),
        "PLATFORM": item.get("platform", ""), "PRODUCT": item.get("product", ""),
        "ICP": item.get("icp", ""), "PROPOSED_IDEA_TEXT": text[:1500],
        "HAS_CAMERA_EMOJI": "TRUE" if item.get("has_camera_emoji") else "FALSE",
        "SHOULD_RATE": "FALSE", "EXCLUSION_REASON": reason, "REVIEW_STATUS": "Excluded",
    }


def _narrative(item, profile, refs, close_idea, rec, copyright_safe, why, gemini):
    prof_name = profile.get("PROFILE_NAME", "") if profile else "no strong internal match"
    default_rationale = (
        f"{rec}: closest internal winning profile is '{prof_name}'. "
        f"{'Aligns with ' + str(len(refs)) + ' safe high-quality external reference(s) (inspiration only, not proof).' if refs else 'No strong external reference found.'}")
    default_revision = ("Sharpen the hook to a concrete pain/number and tie it explicitly to the "
                        "Storelli product; specify 2-3 shootable beats.")
    risk = "" if copyright_safe else f"Copyright/footage risk: {why}."
    if gemini is None:
        return default_rationale, default_revision, risk or "No copyright/famous-player risk detected."
    try:
        prompt = (
            "You are a Storelli creative strategist rating a proposed content-calendar idea. "
            "Internal winning profiles are the PROOF; external inspiration is reference only "
            "(never proof, never cite external views). Be concrete.\n\n"
            f"Idea: {item.get('title','')} — {item.get('notes','')}\n"
            f"Closest internal winning profile: {prof_name}\n"
            f"Recommendation (fixed): {rec}\n\n"
            "Respond ONLY as JSON: {\"rationale\":str,\"revision_suggestion\":str,\"risk_notes\":str}")
        from analyzer import parse_model_json
        out = parse_model_json(gemini.summarize_findings(prompt))
        return (str(out.get("rationale") or default_rationale),
                str(out.get("revision_suggestion") or default_revision),
                (risk + " " + str(out.get("risk_notes", ""))).strip())
    except Exception as e:  # noqa: BLE001
        log.warning("calendar narrative LLM failed: %s", e)
        return default_rationale, default_revision, risk


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
def rate_calendar_ideas(sheets: Optional[InspirationSheets] = None, gemini="auto",
                        limit: int = 10, db_id: Optional[str] = None,
                        exclude_camera: Optional[bool] = None) -> dict:
    """`gemini`: "auto" builds a real client for narrative (production default);
    None disables the LLM (deterministic narrative); or pass an injected client."""
    import notion_calendar
    sheets = sheets or InspirationSheets()
    try:
        sheets.ensure_calendar_ratings_tab()
    except Exception as e:  # noqa: BLE001
        log.warning("ensure calendar tab failed (continuing): %s", e)

    if gemini == "auto":
        try:
            from gemini_client import GeminiClient
            gemini = GeminiClient()
        except Exception:  # noqa: BLE001
            gemini = None

    profiles = sheets.read_profiles()
    inspiration = sheets.read_content_rows()
    ideas = sheets.read_ideas()

    ratable, excluded = notion_calendar.read_ratable_calendar_items(
        db_id=db_id, limit=max(limit, 50), exclude_camera=exclude_camera)
    ratable = ratable[:limit]

    run = _new_run("CalendarRatings", "calendar-rater")
    run["POSTS_DISCOVERED"] = len(ratable) + len(excluded)
    rows = [rate_item(item, profiles, inspiration, ideas, gemini) for item, _ in ratable]
    rows += [excluded_row(item, reason) for item, reason in excluded]

    errors: list[str] = []
    created = updated = 0
    try:
        created, updated = sheets.upsert_calendar_ratings(rows)
    except Exception as e:  # noqa: BLE001
        errors.append(f"upsert failed: {e}")

    run["POSTS_ADDED"] = created
    run["POSTS_ANALYZED"] = len(ratable)
    run["POSTS_SKIPPED_EXISTING"] = len(excluded)
    run["POSTS_SHORTLISTED"] = sum(1 for r in rows if r.get("RECOMMENDATION") == "Keep")
    run["_ratings"] = rows
    log.info("Calendar ratings: rated=%d excluded=%d (created=%d updated=%d)",
             len(ratable), len(excluded), created, updated)
    return _finalize_and_log_run(sheets, run, errors, failed=len(errors), total=1)


def print_calendar_summary(run: dict) -> None:
    rows = [r for r in run.get("_ratings", []) if r.get("SHOULD_RATE") == "TRUE"]
    print("\nContent calendar idea rating complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Items rated:            {run.get('POSTS_ANALYZED')}")
    print(f"Items excluded:         {run.get('POSTS_SKIPPED_EXISTING')}")
    print(f"Keep recommendation:    {run.get('POSTS_SHORTLISTED')}")
    for r in sorted(rows, key=lambda x: _num(x.get("CALENDAR_IDEA_SCORE")), reverse=True)[:10]:
        print(f"  [{r['CALENDAR_IDEA_SCORE']}] {r['RECOMMENDATION']:<14} {r['CALENDAR_TITLE'][:50]}")
