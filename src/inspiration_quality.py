"""Inspiration Candidate Quality Review (quality-control layer, NOT idea scoring).

Evaluates each SAFE, ANALYZED external inspiration candidate for whether it is
genuinely useful to Storelli: is the creative mechanism adaptable without copying
the footage, is it free of famous-player/match/highlight/off-domain risk, and is
it worth saving for future idea generation.

Boundaries (unchanged across the Inspiration Layer):
- Writes only to external INSPIRATION_CONTENT rows. Never touches internal
  Storelli rows or WINNING_FORMAT_PROFILES.
- Views / follower ratio is a PRIORITIZATION signal only (15% weight). It is
  never Storelli proof and can never enter correlations / Signal Library /
  Marketing Learnings.
- This is quality control, not idea generation or idea scoring.
"""
from __future__ import annotations

from typing import Optional

from inspiration_discovery import (FAMOUS_PLAYERS, LEAGUE_TERMS, MATCH_TERMS,
                                   OFF_DOMAIN_TERMS)
from inspiration_scanner import _finalize_and_log_run, _new_run, _now_iso
from inspiration_sheets import (CONTENT_QUALITY_COLUMNS, SOURCE_TYPE_EXTERNAL,
                                InspirationSheets)
from logger import get_logger

log = get_logger()

QUALITY_MIN_FOR_USE = 70.0
COPYRIGHT_RISK_MAX_FOR_USE = 30.0
FULL_VIDEO_TOP_N = 5     # attempt media inspection for the top-N by FINAL_SCORE

REVIEW_FULL = "Full Video"
REVIEW_METADATA = "Metadata Only"

# Storelli-domain relevance vocabulary (goalkeeper / soccer / protection / youth).
STORELLI_TERMS = {
    "goalkeeper", "keeper", "goalie", "soccer", "football", "diving", "landing",
    "turf", "bruise", "injury", "prevent", "prehab", "protection", "protective",
    "gear", "pads", "compression", "confidence", "fear", "youth", "coach",
    "training", "drill", "technique", "save", "gloves", "shin",
}
# Formats/hooks that adapt well without copying footage.
ADAPTABLE_FORMATS = {"tutorial", "demo", "do / don't", "story", "pov", "comparison"}
ADAPTABLE_HOOKS = {"education", "fear / risk", "curiosity gap", "authority", "aspiration"}


def _tags(cell) -> set:
    return {t.strip().lower() for t in str(cell or "").split(",") if t.strip()}


def _num(v, default=None):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def _blob(row: dict) -> str:
    return " ".join([str(row.get("CAPTION", "")), str(row.get("HANDLE", "")),
                     str(row.get("HASHTAGS", ""))]).lower()


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------
def eligible_for_review(row: dict) -> bool:
    return (str(row.get("SOURCE_TYPE", "")).strip() == SOURCE_TYPE_EXTERNAL
            and str(row.get("SAFETY_STATUS", "")).strip().lower() == "safe"
            and str(row.get("ANALYSIS_STATUS", "")).strip().lower() == "analyzed")


# ---------------------------------------------------------------------------
# component scores (pure)
# ---------------------------------------------------------------------------
def creative_mechanism(row: dict) -> str:
    hook = (list(_tags(row.get("HOOK_TAGS"))) or [""])[0]
    fmt = (list(_tags(row.get("FORMAT_TAGS"))) or [""])[0]
    problem = (list(_tags(row.get("PROBLEM_TAGS"))) or [""])[0]
    solution = (list(_tags(row.get("SOLUTION_TAGS"))) or [""])[0]
    if not (fmt and (problem or solution)):
        return ""      # no clear mechanism
    core = f"{problem} -> {solution}".strip(" ->") if (problem or solution) else ""
    parts = [p for p in [hook.title() if hook else "", fmt.title() if fmt else "", core] if p]
    return " | ".join(parts)


def mechanism_clarity(row: dict) -> float:
    present = sum(bool(_tags(row.get(c))) for c in
                  ("HOOK_TAGS", "FORMAT_TAGS", "PROBLEM_TAGS", "SOLUTION_TAGS", "FUNNEL_STAGE_TAGS"))
    return round(present / 5, 4)


def adaptability_score(row: dict) -> float:
    fmts = _tags(row.get("FORMAT_TAGS"))
    hooks = _tags(row.get("HOOK_TAGS"))
    has_problem = bool(_tags(row.get("PROBLEM_TAGS")))
    has_solution = bool(_tags(row.get("SOLUTION_TAGS")))
    s = 0.0
    if fmts & ADAPTABLE_FORMATS:
        s += 0.45
    if hooks & ADAPTABLE_HOOKS:
        s += 0.25
    if has_problem and has_solution:
        s += 0.30      # a clear problem->solution transfers as a mechanism
    elif has_problem or has_solution:
        s += 0.12
    return round(min(1.0, s) * 100, 1)


def _keyword_relevance(row: dict) -> float:
    text = _blob(row)
    hits = sum(1 for t in STORELLI_TERMS if t in text)
    return min(1.0, hits / 4)


def storelli_relevance_score(row: dict) -> float:
    match = _num(row.get("MATCH_SCORE"), 0.0) or 0.0
    kw = _keyword_relevance(row)
    return round(min(100.0, 0.5 * match + 0.5 * kw * 100), 1)


def risk_assessment(row: dict) -> tuple:
    """Returns (copyright_risk_score 0-100, famous_risk, match_risk,
    offdomain_risk) — labels are Low/High. Caption/handle only, no face recog."""
    text = _blob(row)

    def hit(bank):
        return any(t and t in text for t in bank)
    famous = hit(FAMOUS_PLAYERS)
    match = hit(MATCH_TERMS) or hit(LEAGUE_TERMS)
    offdomain = hit(OFF_DOMAIN_TERMS)
    risk = 0
    if famous:
        risk = max(risk, 90)
    if match:
        risk = max(risk, 85)
    if offdomain:
        risk = max(risk, 80)
    if risk == 0:
        risk = 10
    return (risk,
            "High" if famous else "Low",
            "High" if match else "Low",
            "High" if offdomain else "Low")


def ratio_signal(row: dict) -> float:
    r = _num(row.get("VIEW_FOLLOWER_RATIO"), None)
    if r is None:
        p = _num(row.get("PRIORITY_SCORE"), None)
        return min(1.0, p) if p is not None else 0.0
    return round(min(1.0, r / 5.0), 4)


def quality_score(adaptability: float, relevance: float, clarity: float,
                  ratio: float, copyright_risk: float) -> float:
    base = (0.35 * adaptability + 0.30 * relevance
            + 0.20 * clarity * 100 + 0.15 * ratio * 100)
    penalty = max(0.0, copyright_risk - COPYRIGHT_RISK_MAX_FOR_USE)
    return round(max(0.0, min(100.0, base - penalty)), 1)


# ---------------------------------------------------------------------------
# single-row review (pure)
# ---------------------------------------------------------------------------
def review_row(row: dict, *, review_method: str = REVIEW_METADATA,
               note_extra: str = "") -> dict:
    adapt = adaptability_score(row)
    relevance = storelli_relevance_score(row)
    clarity = mechanism_clarity(row)
    ratio = ratio_signal(row)
    crisk, famous, matchr, offd = risk_assessment(row)
    mechanism = creative_mechanism(row)
    quality = quality_score(adapt, relevance, clarity, ratio, crisk)

    safe = (str(row.get("SAFETY_STATUS", "")).strip().lower() == "safe"
            and str(row.get("ANALYSIS_STATUS", "")).strip().lower() == "analyzed")
    use = (safe and quality >= QUALITY_MIN_FOR_USE
           and crisk <= COPYRIGHT_RISK_MAX_FOR_USE
           and famous == "Low" and matchr == "Low" and offd == "Low"
           and bool(mechanism))

    reasons = []
    if not safe:
        reasons.append("not Safe+Analyzed")
    if quality < QUALITY_MIN_FOR_USE:
        reasons.append(f"quality {quality} < {int(QUALITY_MIN_FOR_USE)}")
    if crisk > COPYRIGHT_RISK_MAX_FOR_USE:
        reasons.append(f"copyright risk {crisk}")
    if famous != "Low":
        reasons.append("famous-player risk")
    if matchr != "Low":
        reasons.append("match-footage risk")
    if offd != "Low":
        reasons.append("off-domain risk")
    if not mechanism:
        reasons.append("no clear mechanism")
    # The review method is ALWAYS recorded so a metadata-only review can never
    # silently claim full-video confidence, even when USE_FOR_IDEA_GEN is TRUE.
    method_note = ("full-video inspected" if review_method == REVIEW_FULL
                   else "metadata-based review")
    decision = "USE_FOR_IDEA_GEN=TRUE" if use else "not selected: " + "; ".join(reasons)
    note = f"[{method_note}] {decision}"
    if note_extra:
        note = f"{note} | {note_extra}"

    return {
        "QUALITY_REVIEW_STATUS": "Reviewed",
        "REVIEW_METHOD": review_method,
        "CREATIVE_MECHANISM": mechanism,
        "ADAPTABILITY_SCORE": adapt,
        "STORELLI_RELEVANCE_SCORE": relevance,
        "COPYRIGHT_RISK_SCORE": crisk,
        "FAMOUS_PLAYER_RISK": famous,
        "MATCH_FOOTAGE_RISK": matchr,
        "OFF_DOMAIN_RISK": offd,
        "INSPIRATION_QUALITY_SCORE": quality,
        "QUALITY_REVIEW_NOTES": note[:480],
        "USE_FOR_IDEA_GEN": "TRUE" if use else "FALSE",
        "LAST_UPDATED_AT": _now_iso(),
    }


# ---------------------------------------------------------------------------
# best-effort full-video inspection (bounded, never fails the run)
# ---------------------------------------------------------------------------
def _inspect_media(url: str) -> tuple:
    """Download the actual media with yt-dlp to confirm it is a real, accessible
    video (dead/removed links fail). Returns (ok, note). Never raises."""
    import os
    import tempfile
    try:
        import yt_dlp
    except ImportError:
        return False, "yt-dlp unavailable"
    import config
    tmp = tempfile.mkdtemp(prefix="qr_")
    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            "outtmpl": os.path.join(tmp, "v.%(ext)s"), "format": "mp4/best"}
    if config.YTDLP_COOKIES_PATH:
        opts["cookiefile"] = config.YTDLP_COOKIES_PATH   # only used for IG URLs
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url.strip(), download=True)
        files = [f for f in os.listdir(tmp) if f.startswith("v.")]
        ok = bool(files)
        dur = info.get("duration") if isinstance(info, dict) else None
        note = f"media downloaded (duration {dur}s)" if ok else "no media produced"
        for f in files:
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        return ok, note
    except Exception as e:  # noqa: BLE001 - one bad video must not abort the run
        return False, f"download failed: {type(e).__name__}"


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
def quality_review_inspiration(sheets: Optional[InspirationSheets] = None,
                               full_video_top_n: int = FULL_VIDEO_TOP_N,
                               enable_full_video: bool = True) -> dict:
    sheets = sheets or InspirationSheets()
    try:
        sheets.ensure_content_columns(CONTENT_QUALITY_COLUMNS)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure quality columns failed (continuing): %s", e)

    rows = sheets.read_content_rows()
    targets = [r for r in rows if eligible_for_review(r)]

    # Choose the top-N by FINAL_SCORE (fallback PRIORITY_SCORE) for media check.
    def rank_key(r):
        return (_num(r.get("FINAL_SCORE"), 0.0) or 0.0,
                _num(r.get("PRIORITY_SCORE"), 0.0) or 0.0)
    ranked = sorted(targets, key=rank_key, reverse=True)
    full_video_rows = {id(r) for r in ranked[:full_video_top_n]} if enable_full_video else set()

    run = _new_run("QualityReview", "quality-control")
    run["POSTS_DISCOVERED"] = len(targets)
    log.info("Quality review: %d eligible candidate(s); full-video attempts on top %d",
             len(targets), len(full_video_rows))

    used = full_full = 0
    writes = []
    for r in targets:
        method = REVIEW_METADATA
        note_extra = ""
        if id(r) in full_video_rows:
            ok, note = _inspect_media(str(r.get("POST_URL", "")))
            if ok:
                method = REVIEW_FULL
                full_full += 1
            note_extra = note
        cells = review_row(r, review_method=method, note_extra=note_extra)
        writes.append((r["_row"], cells))
        if cells["USE_FOR_IDEA_GEN"] == "TRUE":
            used += 1

    errors: list[str] = []
    try:
        sheets.update_content_cells_bulk(writes)
    except Exception as e:  # noqa: BLE001
        errors.append(f"bulk write failed: {e}")
        run["POSTS_FAILED"] = len(writes)

    run["POSTS_ANALYZED"] = len(targets)
    run["POSTS_SHORTLISTED"] = used
    run["_full_video_count"] = full_full
    return _finalize_and_log_run(sheets, run, errors,
                                 failed=run["POSTS_FAILED"], total=len(targets))


def print_quality_summary(run: dict) -> None:
    print("\nInspiration quality review complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Candidates reviewed:    {run.get('POSTS_DISCOVERED')}")
    print(f"USE_FOR_IDEA_GEN=TRUE:  {run.get('POSTS_SHORTLISTED')}")
    print(f"Full-video inspected:   {run.get('_full_video_count', 0)}")
    if run.get("ERROR_SUMMARY"):
        print(f"Errors:                 {run.get('ERROR_SUMMARY')}")
