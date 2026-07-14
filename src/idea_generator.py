"""Milestone 4A — Rated Creative Idea Generation.

Generates Storelli-specific creative ideas by ADAPTING high-quality external
creative mechanisms onto internal Storelli Winning Format Profiles, then rates
each idea with a transparent scoring system and a self-critique gate.

Non-negotiable rules:
- Every idea MUST be anchored to an active internal winning profile (internal
  evidence). External inspiration is used ONLY as execution reference and is
  NEVER Storelli proof. An idea with no internal evidence is never written.
- Internal evidence is cited as [S#], external inspiration as [E#] — separately.
- No copying of captions/scripts/footage; no famous players, match/broadcast
  footage, fan edits, or off-domain/tactical/body-armor/beauty content.
- Writes only to the INSPIRATION_IDEAS tab. Never modifies internal Storelli
  rows or the winning profiles.

This is idea suggestion + rating (Milestone 4A). It does not change Slack.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from inspiration_discovery import (FAMOUS_PLAYERS, LEAGUE_TERMS,
                                   OFF_DOMAIN_TERMS)

# Match-footage phrases to block in GENERATED idea copy. Deliberately NARROWER
# than the scraper's MATCH_TERMS: the bare words "highlight(s)"/"compilation"/
# "goal" are legitimate in marketing copy ("this highlights the protection"), so
# we only block phrases that clearly mean using real match/broadcast footage.
IDEA_MATCH_TERMS = {
    "match highlights", "full match", "match footage", "game footage",
    "broadcast footage", "broadcast clip", "broadcast video", "tv footage",
    "fan edit", "celebrity edit", "save compilation", "goal compilation",
    "matchday footage", "real match", "live match",
}
from inspiration_scanner import _finalize_and_log_run, _new_run
from inspiration_sheets import (IDEA_SCORE_COLUMNS, SOURCE_TYPE_EXTERNAL,
                                InspirationSheets)
import taxonomy
from logger import get_logger

log = get_logger()

IDEAS_PER_PROFILE = 5
MIN_IDEA_SCORE = 55.0             # self-critique floor: below this, drop the idea
QUALITY_MIN = 80.0

_GENERIC_HOOKS = {"", "check this out", "watch this", "you won't believe this",
                  "amazing", "must watch", "wow"}


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------
def _num(v, default=0.0):
    try:
        s = str(v).strip()
        return float(s) if s != "" else default
    except (TypeError, ValueError):
        return default


def eligible_inspiration(row: dict) -> bool:
    return (str(row.get("SOURCE_TYPE", "")).strip() == SOURCE_TYPE_EXTERNAL
            and str(row.get("SAFETY_STATUS", "")).strip().lower() == "safe"
            and str(row.get("ANALYSIS_STATUS", "")).strip().lower() == "analyzed"
            and str(row.get("USE_FOR_IDEA_GEN", "")).strip().upper() == "TRUE"
            and _num(row.get("INSPIRATION_QUALITY_SCORE")) >= QUALITY_MIN
            and str(row.get("FAMOUS_PLAYER_RISK", "")).strip() == "Low"
            and str(row.get("MATCH_FOOTAGE_RISK", "")).strip() == "Low"
            and str(row.get("OFF_DOMAIN_RISK", "")).strip() == "Low"
            and bool(str(row.get("CREATIVE_MECHANISM", "")).strip()))


def eligible_profiles(profiles: list[dict]) -> list[dict]:
    out = []
    for p in profiles:
        if str(p.get("ACTIVE", "")).strip().lower() not in ("true", "1", "yes"):
            continue
        if str(p.get("CONFIDENCE", "")).strip().lower() not in ("medium", "high"):
            continue
        if not _num(p.get("INTERNAL_SAMPLE_SIZE")):
            continue
        if not (str(p.get("SUPPORTING_LEARNING_IDS", "")).strip()
                or str(p.get("SUPPORTING_VIDEO_URLS", "")).strip()):
            continue
        out.append(p)
    return out


def top_refs_for_profile(profile: dict, inspiration_rows: list[dict], k: int = 4) -> list[dict]:
    """Eligible external rows that best match this profile, highest quality first.
    Prefers rows whose BEST_MATCHED_PROFILE_ID is this profile, then any other
    eligible on-domain row."""
    pid = str(profile.get("PROFILE_ID", "")).strip()
    elig = [r for r in inspiration_rows if eligible_inspiration(r)]
    matched = [r for r in elig if str(r.get("BEST_MATCHED_PROFILE_ID", "")).strip() == pid]
    others = [r for r in elig if str(r.get("BEST_MATCHED_PROFILE_ID", "")).strip() != pid]
    matched.sort(key=lambda r: _num(r.get("INSPIRATION_QUALITY_SCORE")), reverse=True)
    others.sort(key=lambda r: _num(r.get("INSPIRATION_QUALITY_SCORE")), reverse=True)
    return (matched + others)[:k]


# ---------------------------------------------------------------------------
# copyright re-check on generated idea text
# ---------------------------------------------------------------------------
def copyright_recheck(text: str) -> tuple[bool, str]:
    t = str(text or "").lower()
    for label, bank in (("famous player", FAMOUS_PLAYERS), ("match/broadcast footage", IDEA_MATCH_TERMS),
                        ("league/competition", LEAGUE_TERMS), ("off-domain", OFF_DOMAIN_TERMS)):
        h = next((x for x in bank if x and x in t), None)
        if h:
            return False, f"idea text references {label} '{h.strip()}'"
    return True, ""


# ---------------------------------------------------------------------------
# scoring (deterministic IDEA_SCORE; evidence/inspiration/safety are anchored,
# subjective dims come from the model but are clamped)
# ---------------------------------------------------------------------------
def _clamp(v):
    return max(0.0, min(100.0, _num(v)))


def evidence_fit_from_profile(profile: dict) -> float:
    conf = str(profile.get("CONFIDENCE", "")).strip().lower()
    base = 92.0 if conf == "high" else 80.0 if conf == "medium" else 60.0
    sample = _num(profile.get("INTERNAL_SAMPLE_SIZE"))
    return round(min(100.0, base + min(6.0, max(0.0, sample - 3))), 1)


def compute_idea_scores(profile: dict, refs: list[dict], sub: dict,
                        copyright_ok: bool) -> dict:
    evidence_fit = evidence_fit_from_profile(profile)
    avg_quality = (sum(_num(r.get("INSPIRATION_QUALITY_SCORE")) for r in refs) / len(refs)
                   if refs else 0.0)
    inspiration_fit = round(0.5 * avg_quality + 0.5 * _clamp(sub.get("inspiration_fit")), 1)
    product_fit = _clamp(sub.get("product_fit"))
    icp_fit = _clamp(sub.get("icp_fit"))
    execution = _clamp(sub.get("execution_clarity"))
    novelty = _clamp(sub.get("novelty"))
    feasibility = _clamp(sub.get("feasibility"))
    copyright_safety = 100.0 if copyright_ok else 15.0
    strategic = _clamp(sub.get("strategic_priority"))

    idea_score = round(
        0.25 * evidence_fit + 0.20 * inspiration_fit + 0.15 * product_fit
        + 0.10 * icp_fit + 0.10 * execution + 0.10 * novelty
        + 0.05 * feasibility + 0.05 * copyright_safety, 1)
    return {
        "IDEA_SCORE": idea_score,
        "EVIDENCE_FIT_SCORE": evidence_fit,
        "INSPIRATION_FIT_SCORE": inspiration_fit,
        "PRODUCT_FIT_SCORE": product_fit,
        "ICP_FIT_SCORE": icp_fit,
        "EXECUTION_CLARITY_SCORE": execution,
        "NOVELTY_SCORE": novelty,
        "FEASIBILITY_SCORE": feasibility,
        "COPYRIGHT_SAFETY_SCORE": copyright_safety,
        "STRATEGIC_PRIORITY_SCORE": strategic,
    }


def self_critique_pass(idea: dict, scores: dict) -> tuple[bool, str]:
    """Deterministic self-critique gate applied after the model's own critique.
    Rejects generic hooks, copyright hits, missing structure, and sub-threshold
    ideas."""
    hook = str(idea.get("hook", "")).strip()
    if hook.lower() in _GENERIC_HOOKS or len(hook) < 12:
        return False, "hook too generic/short"
    if not str(idea.get("concept", "")).strip():
        return False, "no concept"
    if not (idea.get("shot_list")):
        return False, "no shot list"
    text = " ".join(str(idea.get(k, "")) for k in
                    ("idea_title", "hook", "concept", "storelli_adaptation", "cta"))
    ok, why = copyright_recheck(text)
    if not ok:
        return False, why
    if str(idea.get("verdict", "keep")).strip().lower() == "drop":
        return False, "model self-critique verdict: drop"
    if scores["IDEA_SCORE"] < MIN_IDEA_SCORE:
        return False, f"IDEA_SCORE {scores['IDEA_SCORE']} < {int(MIN_IDEA_SCORE)}"
    return True, ""


# ---------------------------------------------------------------------------
# prompt + generation
# ---------------------------------------------------------------------------
def _profile_pattern(profile: dict) -> str:
    parts = []
    for col, label in (("HOOK_TAGS", "hook"), ("FORMAT_TAGS", "format"),
                       ("VISUAL_STYLE_TAGS", "visual"), ("PROBLEM_TAGS", "problem"),
                       ("SOLUTION_TAGS", "solution"), ("FUNNEL_STAGE_TAGS", "funnel")):
        v = str(profile.get(col, "")).strip()
        if v:
            parts.append(f"{label}: {v}")
    return "; ".join(parts)


def build_generation_prompt(profile: dict, refs: list[dict], n: int,
                            s_cites: list[str], e_cites: list[dict]) -> str:
    e_lines = "\n".join(
        f"[E{i+1}] mechanism='{c['mechanism']}' hook='{c['hook']}' format='{c['format']}' "
        f"(quality {c['quality']}, url {c['url']})" for i, c in enumerate(e_cites))
    s_lines = "\n".join(f"[S{i+1}] {u}" for i, u in enumerate(s_cites))
    return (
        "You are a Storelli creative strategist. Generate Storelli-specific short-form "
        "video ideas by ADAPTING external creative MECHANISMS onto a proven internal "
        "winning format. Do NOT copy scripts, captions, or footage. Do NOT use famous "
        "players, match/broadcast footage, fan edits, or off-domain content.\n\n"
        f"{taxonomy.PRODUCT_CONTEXT}\n\n"
        f"INTERNAL WINNING PROFILE (this is the PROOF the format works for Storelli):\n"
        f"- product: {profile.get('PRODUCT')}\n- icp: {profile.get('ICP')}\n"
        f"- pattern: {_profile_pattern(profile)}\n"
        f"- confidence: {profile.get('CONFIDENCE')} (internal sample {profile.get('INTERNAL_SAMPLE_SIZE')})\n"
        f"- internal evidence videos:\n{s_lines}\n\n"
        f"EXTERNAL INSPIRATION (execution reference ONLY — NOT proof, do not copy):\n{e_lines}\n\n"
        f"Generate {n} DISTINCT ideas for product '{profile.get('PRODUCT')}' / ICP "
        f"'{profile.get('ICP')}'. For each, run a self-critique (too generic? really maps "
        "to Storelli? evidence real? external used only as inspiration? any copyright/"
        "famous-player/match risk? shootable? hook specific? meaningfully different from "
        "the reference?). Revise once; if still weak set verdict='drop'.\n\n"
        "In idea_rationale, cite internal evidence as [S#] and external inspiration as [E#] "
        "SEPARATELY. Never claim external views prove Storelli performance.\n\n"
        "Respond with ONLY JSON: {\"ideas\":[{"
        "\"idea_title\":str,\"hook\":str,\"format\":str,\"concept\":str,"
        "\"storelli_adaptation\":str,\"shot_list\":[str],\"cta\":str,"
        "\"idea_rationale\":str (with [S#]/[E#]),\"self_critique\":str,\"risk_notes\":str,"
        "\"recommended_shoot_priority\":\"High|Medium|Low\",\"verdict\":\"keep|drop\","
        "\"inspiration_fit\":0-100,\"product_fit\":0-100,\"icp_fit\":0-100,"
        "\"execution_clarity\":0-100,\"novelty\":0-100,\"feasibility\":0-100,"
        "\"strategic_priority\":0-100}]}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_ideas(profiles: list[dict], inspiration_rows: list[dict], gemini,
                per_profile: int = IDEAS_PER_PROFILE) -> list[dict]:
    """Generate + score + self-critique ideas. `gemini` must expose
    summarize_findings(prompt)->json text. Returns kept idea row dicts.

    No active profile => no ideas (external inspiration alone can never produce
    an idea)."""
    from analyzer import parse_model_json

    profs = eligible_profiles(profiles)
    ideas = []
    counter = 0
    for profile in profs:
        refs = top_refs_for_profile(profile, inspiration_rows, k=4)
        if not refs:
            continue   # need at least one safe, high-quality external reference
        s_urls = [u for u in str(profile.get("SUPPORTING_VIDEO_URLS", "")).split(";") if u.strip()][:4]
        e_cites = [{
            "url": str(r.get("POST_URL", "")).strip(),
            "source_id": str(r.get("SOURCE_ID", "")).strip(),
            "mechanism": str(r.get("CREATIVE_MECHANISM", "")).strip(),
            "hook": str(r.get("HOOK_TAGS", "")).strip(),
            "format": str(r.get("FORMAT_TAGS", "")).strip(),
            "quality": str(r.get("INSPIRATION_QUALITY_SCORE", "")).strip(),
        } for r in refs]
        prompt = build_generation_prompt(profile, refs, per_profile, s_urls, e_cites)

        try:
            raw = gemini.summarize_findings(prompt)
            payload = parse_model_json(raw)
        except Exception as e:  # noqa: BLE001 - one profile must not abort the run
            log.warning("idea generation failed for %s: %s", profile.get("PROFILE_ID"), e)
            continue

        for idea in (payload.get("ideas") or []):
            if not isinstance(idea, dict):
                continue
            copyright_ok, _ = copyright_recheck(" ".join(
                str(idea.get(k, "")) for k in
                ("idea_title", "hook", "concept", "storelli_adaptation", "cta", "idea_rationale")))
            scores = compute_idea_scores(profile, refs, idea, copyright_ok)
            keep, reason = self_critique_pass(idea, scores)
            if not keep:
                log.info("dropped idea '%s' (%s)", idea.get("idea_title"), reason)
                continue
            counter += 1
            ideas.append(_idea_row(profile, refs, idea, scores, e_cites, s_urls, counter))
    return ideas


def _idea_row(profile: dict, refs: list[dict], idea: dict, scores: dict,
              e_cites: list[dict], s_urls: list[str], n: int) -> dict:
    now = _now_iso()
    shot_list = idea.get("shot_list") or []
    if isinstance(shot_list, list):
        shot_list = " | ".join(str(x) for x in shot_list)
    ext_urls = ";".join(c["url"] for c in e_cites if c["url"])
    ext_ids = ";".join(c["source_id"] for c in e_cites if c["source_id"])
    row = {
        "IDEA_ID": f"IDEA-{taxonomy.slug(str(profile.get('PRODUCT','')))}-{n:03d}",
        "CREATED_AT": now, "LAST_UPDATED_AT": now,
        "REQUEST_CONTEXT": "Milestone 4A rated idea generation",
        "PRODUCT": profile.get("PRODUCT", ""), "ICP": profile.get("ICP", ""),
        "IDEA_TITLE": str(idea.get("idea_title", "")).strip(),
        "HOOK": str(idea.get("hook", "")).strip(),
        "FORMAT": str(idea.get("format", "")).strip(),
        "CONCEPT": str(idea.get("concept", "")).strip(),
        "STORELLI_ADAPTATION": str(idea.get("storelli_adaptation", "")).strip(),
        "SHOT_LIST": shot_list,
        "CTA": str(idea.get("cta", "")).strip(),
        "EXTERNAL_REFERENCE_URL": (e_cites[0]["url"] if e_cites else ""),
        "EXTERNAL_SOURCE_ID": (e_cites[0]["source_id"] if e_cites else ""),
        "EXTERNAL_REFERENCE_URLS": ext_urls,
        "EXTERNAL_SOURCE_IDS": ext_ids,
        "INTERNAL_EVIDENCE_IDS": str(profile.get("SUPPORTING_LEARNING_IDS", "")),
        "INTERNAL_EVIDENCE_URLS": ";".join(s_urls),
        "SOURCE_PROFILE_ID": profile.get("PROFILE_ID", ""),
        "SOURCE_PROFILE_NAME": profile.get("PROFILE_NAME", ""),
        "MATCH_REASON": str(idea.get("idea_rationale", "")).strip()[:900],
        "IDEA_RATIONALE": str(idea.get("idea_rationale", "")).strip()[:1500],
        "SELF_CRITIQUE": str(idea.get("self_critique", "")).strip()[:900],
        "RISK_NOTES": str(idea.get("risk_notes", "")).strip()[:500]
                      or "External inspiration used as execution reference only; not Storelli proof.",
        "RECOMMENDED_SHOOT_PRIORITY": str(idea.get("recommended_shoot_priority", "Medium")).strip(),
        "CONFIDENCE": profile.get("CONFIDENCE", ""),
        "STATUS": "Proposed", "OWNER": "",
    }
    row.update(scores)
    return row


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
def generate_ideas(sheets: Optional[InspirationSheets] = None, gemini=None,
                   max_total: int = 20) -> dict:
    sheets = sheets or InspirationSheets()
    try:
        sheets.ensure_idea_columns(IDEA_SCORE_COLUMNS)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure idea columns failed (continuing): %s", e)

    if gemini is None:
        from gemini_client import GeminiClient
        gemini = GeminiClient()

    profiles = sheets.read_profiles()
    inspiration = sheets.read_content_rows()
    run = _new_run("Ideas", "gemini-strategist")

    ideas = build_ideas(profiles, inspiration, gemini)[:max_total]
    run["POSTS_DISCOVERED"] = len(eligible_profiles(profiles))
    errors: list[str] = []
    written = 0
    try:
        written = sheets.append_ideas(ideas)
    except Exception as e:  # noqa: BLE001
        errors.append(f"append failed: {e}")

    run["POSTS_ADDED"] = written
    run["POSTS_SHORTLISTED"] = sum(
        1 for i in ideas if str(i.get("RECOMMENDED_SHOOT_PRIORITY", "")).lower() == "high")
    run["_ideas"] = ideas
    log.info("Idea generation: %d ideas written from %d active profiles",
             written, run["POSTS_DISCOVERED"])
    return _finalize_and_log_run(sheets, run, errors, failed=len(errors), total=1)


def print_ideas_summary(run: dict) -> None:
    ideas = run.get("_ideas", [])
    print("\nRated creative idea generation complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Active profiles used:   {run.get('POSTS_DISCOVERED')}")
    print(f"Ideas written:          {run.get('POSTS_ADDED')}")
    print(f"High shoot-priority:    {run.get('POSTS_SHORTLISTED')}")
    for i in sorted(ideas, key=lambda x: _num(x.get("IDEA_SCORE")), reverse=True)[:10]:
        print(f"\n  [{i['IDEA_SCORE']}] {i['IDEA_TITLE']}  ({i['PRODUCT']} / {i['ICP']})")
        print(f"    hook: {i['HOOK']}")
        print(f"    strategic_priority={i['STRATEGIC_PRIORITY_SCORE']} shoot={i['RECOMMENDED_SHOOT_PRIORITY']}")
