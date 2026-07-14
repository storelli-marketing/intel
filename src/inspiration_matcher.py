"""Milestone 3B — Match external inspiration to Storelli Winning Format Profiles.

Scores each SAFE, ANALYZED external inspiration row against the ACTIVE
(Medium/High) winning profiles on taxonomy/profile fit, estimates novelty
(same strategic mechanism, fresh execution), combines into a FINAL_SCORE, and
shortlists the strongest references for later idea work.

Hard boundaries:
- Matching READS the winning profiles and WRITES only to the external
  INSPIRATION_CONTENT rows. It never modifies WINNING_FORMAT_PROFILES proof
  fields, and never touches internal Storelli rows.
- Discovery PRIORITY_SCORE / views / follower ratio is ONLY a secondary ranking
  signal (15% of FINAL_SCORE). It is never Storelli evidence and can never enter
  profiles, the Signal Library, Marketing Learnings, or correlations.

Not in scope: idea generation, idea scoring.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from inspiration_discovery import (FAMOUS_PLAYERS, LEAGUE_TERMS, MATCH_TERMS,
                                   OFF_DOMAIN_TERMS)
from inspiration_scanner import _finalize_and_log_run, _new_run, _now_iso
from inspiration_sheets import (CONTENT_MATCH_COLUMNS, SOURCE_TYPE_EXTERNAL,
                                InspirationSheets)
from logger import get_logger

log = get_logger()

SHORTLIST_MATCH_MIN = 60.0
SHORTLIST_FINAL_MIN = 60.0
MATCHED_PROFILE_MIN = 40.0     # a profile is a "match" (listed) at/above this

# MATCH_SCORE layer weights (sum = 1.0).
W_FORMAT = 0.25
W_HOOK = 0.20
W_PROBSOL = 0.15
W_VISUAL = 0.15
W_FUNNEL = 0.10
W_PRODICP = 0.10
W_CONTEXT = 0.05

# FINAL_SCORE weights.
F_MATCH = 0.70
F_NOVELTY = 0.15
F_PRIORITY = 0.15


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tags(cell) -> set:
    return {t.strip().lower() for t in str(cell or "").split(",") if t.strip()}


def _coverage(ext: set, prof: set) -> float:
    """Fraction of the profile's tags exhibited by the external row (how well the
    external matches the winning *pattern*). Empty profile layer -> 0 (neutral)."""
    if not prof:
        return 0.0
    return len(ext & prof) / len(prof)


def _token_overlap(a: str, b: str) -> float:
    ta = {t for t in str(a or "").lower().replace("/", " ").replace(",", " ").split() if t}
    tb = {t for t in str(b or "").lower().replace("/", " ").replace(",", " ").split() if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _num(v, default=None):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------
def eligible_external(row: dict) -> bool:
    if str(row.get("SOURCE_TYPE", "")).strip() != SOURCE_TYPE_EXTERNAL:
        return False
    if str(row.get("SAFETY_STATUS", "")).strip().lower() != "safe":
        return False
    if str(row.get("ANALYSIS_STATUS", "")).strip().lower() != "analyzed":
        return False
    return True


def active_profiles(profiles: list[dict]) -> list[dict]:
    out = []
    for p in profiles:
        if str(p.get("ACTIVE", "")).strip().lower() not in ("true", "1", "yes"):
            continue
        if str(p.get("CONFIDENCE", "")).strip().lower() not in ("medium", "high"):
            continue
        if not _num(p.get("INTERNAL_SAMPLE_SIZE"), 0):
            continue
        if not (str(p.get("SUPPORTING_VIDEO_URLS", "")).strip()
                or str(p.get("SUPPORTING_LEARNING_IDS", "")).strip()):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# scoring (pure)
# ---------------------------------------------------------------------------
def _layer_overlaps(row: dict, profile: dict) -> dict:
    def ov(rc, pc):
        return _coverage(_tags(row.get(rc)), _tags(profile.get(pc)))
    return {
        "format": ov("FORMAT_TAGS", "FORMAT_TAGS"),
        "hook": ov("HOOK_TAGS", "HOOK_TAGS"),
        "visual": ov("VISUAL_STYLE_TAGS", "VISUAL_STYLE_TAGS"),
        "problem": ov("PROBLEM_TAGS", "PROBLEM_TAGS"),
        "solution": ov("SOLUTION_TAGS", "SOLUTION_TAGS"),
        "funnel": ov("FUNNEL_STAGE_TAGS", "FUNNEL_STAGE_TAGS"),
    }


def product_icp_relevance(row: dict, profile: dict) -> float:
    prod = _token_overlap(row.get("TARGET_PRODUCT", ""), profile.get("PRODUCT", ""))
    icp = _token_overlap(row.get("TARGET_ICP", ""), profile.get("ICP", ""))
    return max(prod, icp)   # any product OR icp alignment counts


def context_fit(row: dict, profile: dict) -> float:
    hints = " ".join([str(row.get("REASON_FOR_ADDING", "")),
                      str(row.get("REASON_FOR_QUERY", "")),
                      str(row.get("SHOULD_FIND", ""))])
    target = " ".join([str(profile.get("PROFILE_NAME", "")),
                       str(profile.get("PRODUCT", "")), str(profile.get("ICP", ""))])
    return _token_overlap(hints, target)


def match_score(row: dict, profile: dict) -> tuple[float, dict]:
    ov = _layer_overlaps(row, profile)
    probsol = (ov["problem"] + ov["solution"]) / 2
    prodicp = product_icp_relevance(row, profile)
    ctx = context_fit(row, profile)
    s = (W_FORMAT * ov["format"] + W_HOOK * ov["hook"] + W_PROBSOL * probsol
         + W_VISUAL * ov["visual"] + W_FUNNEL * ov["funnel"]
         + W_PRODICP * prodicp + W_CONTEXT * ctx)
    return round(s * 100, 1), {**ov, "probsol": probsol, "prodicp": prodicp, "context": ctx}


def novelty_score(row: dict, profile: dict) -> float:
    """Same strategic mechanism (problem/solution/funnel), fresh execution
    (hook/format/visual) => high. Near-copy or no shared mechanism => low."""
    ov = _layer_overlaps(row, profile)
    mechanism = (ov["problem"] + ov["solution"] + ov["funnel"]) / 3
    execution = (ov["hook"] + ov["format"] + ov["visual"]) / 3
    return round(mechanism * (1 - execution) * 100, 1)


def final_score(match: float, novelty: float, priority01) -> float:
    p = _num(priority01, None)
    if p is None:
        # No discovery priority (e.g. manual-queue rows): renormalize match+novelty.
        return round((F_MATCH * match + F_NOVELTY * novelty) / (F_MATCH + F_NOVELTY), 1)
    return round(F_MATCH * match + F_NOVELTY * novelty + F_PRIORITY * (p * 100), 1)


def looks_unsafe_content(row: dict) -> tuple[bool, str]:
    """Belt-and-suspenders copyright check at shortlist time (in case a manual
    row skipped discovery filtering). Caption/handle only."""
    text = " ".join([str(row.get("CAPTION", "")), str(row.get("HANDLE", ""))]).lower()

    def hit(bank):
        return next((t for t in bank if t and t in text), None)
    for label, bank in (("famous player", FAMOUS_PLAYERS), ("match/highlight", MATCH_TERMS),
                        ("league/competition", LEAGUE_TERMS), ("off-domain", OFF_DOMAIN_TERMS)):
        h = hit(bank)
        if h:
            return True, f"{label}: '{h.strip()}'"
    return False, ""


def _match_confidence(match: float, best_profile: dict) -> str:
    pc = str(best_profile.get("CONFIDENCE", "")).strip().lower()
    if match >= 75 and pc == "high":
        return "High"
    if match >= SHORTLIST_MATCH_MIN:
        return "Medium"
    return "Low"


def match_row(row: dict, profiles: list[dict]) -> dict:
    """Pure: compute all match fields for one external row. Returns the
    writeback cell dict."""
    now = _now_iso()
    actives = active_profiles(profiles)
    base = {"LAST_UPDATED_AT": now}

    if not actives:
        return {**base, "SHORTLISTED": "FALSE",
                "SHORTLIST_REASON": "No active winning profiles to match against.",
                "MATCH_SCORE": "", "NOVELTY_SCORE": "", "FINAL_SCORE": "",
                "MATCHED_PROFILE_IDS": "", "BEST_MATCHED_PROFILE_ID": "",
                "BEST_MATCHED_PROFILE_NAME": "", "MATCH_CONFIDENCE": "Low",
                "MATCH_EXPLANATION": "no active profiles"}

    scored = []
    for p in actives:
        ms, brk = match_score(row, p)
        scored.append((ms, brk, p))
    scored.sort(key=lambda t: t[0], reverse=True)
    best_ms, best_brk, best = scored[0]

    matched = [p for ms, _, p in scored if ms >= MATCHED_PROFILE_MIN]
    novelty = novelty_score(row, best)
    priority = row.get("PRIORITY_SCORE")
    final = final_score(best_ms, novelty, priority)

    # Overlap explanation.
    hits = [f"{k} {int(round(v*100))}%" for k, v in
            (("format", best_brk["format"]), ("hook", best_brk["hook"]),
             ("problem/solution", best_brk["probsol"]), ("visual", best_brk["visual"]),
             ("funnel", best_brk["funnel"])) if v > 0]
    explanation = f"Best fit '{best.get('PROFILE_NAME','')}' via " + (", ".join(hits) or "weak overlap")

    unsafe, unsafe_why = looks_unsafe_content(row)
    safe = (str(row.get("SAFETY_STATUS", "")).strip().lower() == "safe"
            and str(row.get("ANALYSIS_STATUS", "")).strip().lower() == "analyzed")

    if not safe:
        shortlisted, reason = "FALSE", "Not Safe+Analyzed."
    elif unsafe:
        shortlisted, reason = "FALSE", f"Excluded ({unsafe_why})."
    elif best_ms < SHORTLIST_MATCH_MIN:
        shortlisted, reason = "FALSE", f"MATCH_SCORE {best_ms} < {int(SHORTLIST_MATCH_MIN)}."
    elif final < SHORTLIST_FINAL_MIN:
        shortlisted, reason = "FALSE", f"FINAL_SCORE {final} < {int(SHORTLIST_FINAL_MIN)}."
    else:
        shortlisted = "TRUE"
        reason = (f"Strong fit to '{best.get('PROFILE_NAME','')}' "
                  f"(match {best_ms}, final {final}); adaptable mechanism.")

    return {
        **base,
        "MATCHED_PROFILE_IDS": ";".join(p.get("PROFILE_ID", "") for p in matched),
        "MATCHED_INTERNAL_LEARNING_IDS": str(best.get("SUPPORTING_LEARNING_IDS", "")),
        "MATCH_SCORE": best_ms,
        "NOVELTY_SCORE": novelty,
        "FINAL_SCORE": final,
        "BEST_MATCHED_PROFILE_ID": best.get("PROFILE_ID", ""),
        "BEST_MATCHED_PROFILE_NAME": best.get("PROFILE_NAME", ""),
        "MATCH_CONFIDENCE": _match_confidence(best_ms, best),
        "MATCH_EXPLANATION": explanation,
        "SHORTLISTED": shortlisted,
        "SHORTLIST_REASON": reason,
    }


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
def match_inspiration(sheets: Optional[InspirationSheets] = None) -> dict:
    sheets = sheets or InspirationSheets()
    try:
        sheets.ensure_content_columns(CONTENT_MATCH_COLUMNS)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure match columns failed (continuing): %s", e)

    profiles = active_profiles(sheets.read_profiles())
    rows = sheets.read_content_rows()
    targets = [r for r in rows if eligible_external(r)]

    run = _new_run("Match", "internal-profiles")
    run["POSTS_DISCOVERED"] = len(targets)
    log.info("Inspiration match: %d eligible external row(s) vs %d active profile(s)",
             len(targets), len(profiles))
    errors: list[str] = []
    matched = shortlisted = 0

    # Score everything in memory first (no API calls), then write in ONE batch.
    writes = []
    for r in targets:
        cells = match_row(r, profiles)
        writes.append((r["_row"], cells))
        if str(cells.get("BEST_MATCHED_PROFILE_ID", "")).strip():
            matched += 1
        if cells.get("SHORTLISTED") == "TRUE":
            shortlisted += 1
    try:
        sheets.update_content_cells_bulk(writes)
    except Exception as e:  # noqa: BLE001
        run["POSTS_FAILED"] = len(writes)
        errors.append(f"bulk write failed: {e}")

    run["POSTS_ANALYZED"] = matched
    run["POSTS_SHORTLISTED"] = shortlisted
    return _finalize_and_log_run(sheets, run, errors,
                                 failed=run["POSTS_FAILED"], total=len(targets))


def print_match_summary(run: dict) -> None:
    print("\nInspiration matching complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"External rows matched:  {run.get('POSTS_DISCOVERED')}")
    print(f"With a best profile:    {run.get('POSTS_ANALYZED')}")
    print(f"Shortlisted:            {run.get('POSTS_SHORTLISTED')}")
    if run.get("ERROR_SUMMARY"):
        print(f"Errors:                 {run.get('ERROR_SUMMARY')}")
