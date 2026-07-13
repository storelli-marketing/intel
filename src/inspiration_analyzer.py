"""Inspiration Layer — Milestone 2: external inspiration analysis.

Tags EXTERNAL_INSPIRATION rows in INSPIRATION_CONTENT with the Storelli creative
taxonomy so they can LATER be matched against internal winning formats. This
module does NOT do matching, winning-format profiles, idea generation, or idea
scoring — only tagging.

Hard boundary (enforced structurally + defensively)
---------------------------------------------------
External inspiration is NOT Storelli proof. It is read/written only in the
INSPIRATION_CONTENT tab; the internal learning pipeline never reads that tab.
Every row processed here must carry SOURCE_TYPE=EXTERNAL_INSPIRATION, and none
of these tags ever enter performance buckets, correlations, the Signal Library,
Marketing Learnings, or any "what works for Storelli" calculation. External
engagement (likes/views/comments) is metadata only and is NEVER used to infer
performance or to raise confidence.

Analysis inputs, in order of preference
---------------------------------------
1. caption + thumbnail + structural metadata + human queue context
   (REASON_FOR_ADDING / TARGET_PRODUCT / TARGET_ICP).
2. the individual reel download (yt-dlp/cookies) ONLY when
   config.INSPIRATION_FULL_VIDEO_ANALYSIS is true.

Confidence
----------
  LOW    only caption / limited metadata          -> ANALYSIS_STATUS Needs Review
  MEDIUM caption + thumbnail + useful metadata     -> ANALYSIS_STATUS Analyzed
  HIGH   only when full video/rich media analyzed  -> ANALYSIS_STATUS Analyzed
Never HIGH without a full-video analysis; low-information rows never get a fake
HIGH.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import config
import taxonomy
from inspiration_scanner import _new_run, _finalize_and_log_run, _now_iso
from inspiration_sheets import SOURCE_TYPE_EXTERNAL, InspirationSheets
from logger import get_logger

log = get_logger()

TAXONOMY_VERSION = "storelli-taxonomy-v1"

# taxonomy layer -> INSPIRATION_CONTENT tag column
LAYER_TO_TAG_COLUMN = {
    "hook": "HOOK_TAGS",
    "format": "FORMAT_TAGS",
    "visual_style": "VISUAL_STYLE_TAGS",
    "problem_type": "PROBLEM_TAGS",
    "solution_type": "SOLUTION_TAGS",
    "conversion": "CONVERSION_TAGS",
    "offer": "OFFER_TAGS",
    "product_presence": "PRODUCT_PRESENCE_TAGS",
    "funnel_stage": "FUNNEL_STAGE_TAGS",
}

# ANALYSIS_STATUS values (lowercased) that are still eligible for (re)analysis.
_ELIGIBLE_STATUSES = {"", "not analyzed", "queued", "needs review"}
# Values that explicitly opt a row OUT of analysis.
_SKIP_STATUSES = {"skipped", "test", "ignore", "ignored"}

CONF_LOW, CONF_MEDIUM, CONF_HIGH = "LOW", "MEDIUM", "HIGH"


# ---------------------------------------------------------------------------
# Eligibility (SOURCE_TYPE guard lives here)
# ---------------------------------------------------------------------------
def eligible_for_analysis(row: dict) -> bool:
    """True only for external-inspiration rows that still need tagging."""
    if str(row.get("SOURCE_TYPE", "")).strip() != SOURCE_TYPE_EXTERNAL:
        return False                                   # hard SOURCE_TYPE guard
    if not str(row.get("POST_URL", "")).strip():
        return False
    analysis = str(row.get("ANALYSIS_STATUS", "")).strip().lower()
    scrape = str(row.get("SCRAPE_STATUS", "")).strip().lower()
    if analysis in _SKIP_STATUSES or scrape in _SKIP_STATUSES:
        return False
    return analysis in _ELIGIBLE_STATUSES


# ---------------------------------------------------------------------------
# Information assessment & confidence (engagement is NEVER an input here)
# ---------------------------------------------------------------------------
def _has(row: dict, key: str) -> bool:
    return bool(str(row.get(key, "")).strip())


def has_caption(row: dict) -> bool:
    return _has(row, "CAPTION")


def has_thumbnail(row: dict) -> bool:
    return _has(row, "THUMBNAIL_URL")


def has_structural_metadata(row: dict) -> bool:
    """Structural (non-engagement) metadata that helps tagging. Deliberately
    excludes LIKE/VIEW/COMMENT counts so external engagement can never raise
    confidence or be treated as evidence."""
    if _has(row, "PUBLISHED_AT") or _has(row, "DURATION_SECONDS"):
        return True
    ptype = str(row.get("POST_TYPE", "")).strip().lower()
    return bool(ptype and ptype != "unknown")


def decide_confidence(row: dict, full_video_analyzed: bool) -> str:
    """HIGH only when a full-video analysis actually happened. Otherwise MEDIUM
    when caption + thumbnail + structural metadata are all present, else LOW."""
    if full_video_analyzed:
        return CONF_HIGH
    if has_caption(row) and has_thumbnail(row) and has_structural_metadata(row):
        return CONF_MEDIUM
    return CONF_LOW


def status_for_confidence(confidence: str) -> str:
    """LOW-confidence rows are flagged for a human (Needs Review); MEDIUM/HIGH
    are Analyzed."""
    return "Needs Review" if confidence == CONF_LOW else "Analyzed"


# ---------------------------------------------------------------------------
# Prompt building + tag validation
# ---------------------------------------------------------------------------
def _taxonomy_block() -> str:
    lines = []
    for layer, labels in taxonomy.LAYERS.items():
        kind = "choose 0+" if layer in taxonomy.MULTI_LABEL_LAYERS else "choose exactly 1"
        lines.append(f"- {layer} ({kind}): {', '.join(labels)}")
    return "\n".join(lines)


def build_metadata_prompt(row: dict) -> str:
    """Text prompt for caption/metadata-based tagging. Includes human queue
    context as *hints only*, and explicitly forbids performance judgment and use
    of engagement metrics."""
    caption = str(row.get("CAPTION", "")).strip() or "(no caption)"
    handle = str(row.get("HANDLE", "")).strip() or "(unknown)"
    post_type = str(row.get("POST_TYPE", "")).strip() or "(unknown)"
    published = str(row.get("PUBLISHED_AT", "")).strip() or "(unknown)"
    duration = str(row.get("DURATION_SECONDS", "")).strip() or "(unknown)"
    reason = str(row.get("REASON_FOR_ADDING", "")).strip() or "(none)"
    tproduct = str(row.get("TARGET_PRODUCT", "")).strip() or "(none)"
    ticp = str(row.get("TARGET_ICP", "")).strip() or "(none)"
    return (
        "You are tagging an EXTERNAL competitor/creator social post against the "
        "Storelli creative taxonomy. This is inspiration only — it is NOT "
        "evidence of what works for Storelli.\n\n"
        f"{taxonomy.PRODUCT_CONTEXT}\n\n"
        "Post metadata:\n"
        f"- handle: {handle}\n"
        f"- post_type: {post_type}\n"
        f"- published_at: {published}\n"
        f"- duration_seconds: {duration}\n"
        f"- caption: {caption}\n\n"
        "Human curator context (HINTS ONLY — do not treat as ground truth, and "
        "do not let it fabricate tags you cannot justify from the content):\n"
        f"- reason_for_adding: {reason}\n"
        f"- target_product: {tproduct}\n"
        f"- target_icp: {ticp}\n\n"
        "Rules:\n"
        "1. Tag ONLY what the caption/metadata actually support. If unsure for a "
        "layer, return an empty list for it rather than guessing.\n"
        "2. Do NOT judge performance or virality. Ignore any like/view/comment "
        "counts entirely — they are not provided and must not be inferred.\n"
        "3. Use only the exact labels listed below.\n\n"
        "Taxonomy layers and allowed labels:\n"
        f"{_taxonomy_block()}\n\n"
        "Respond with ONLY a JSON object mapping each layer key "
        f"({', '.join(taxonomy.LAYERS)}) to an array of chosen labels "
        "(single-label layers: 0 or 1 element; multi-label: 0+). No prose."
    )


def validate_tags(raw: dict) -> dict:
    """Map a model response to canonical taxonomy labels per layer. Drops unknown
    labels; enforces at most one label for single-label layers. Returns
    {layer: [labels]}."""
    out: dict[str, list] = {}
    raw = raw or {}
    for layer, labels in taxonomy.LAYERS.items():
        canonical_by_slug = {taxonomy.slug(x): x for x in labels}
        got = raw.get(layer, [])
        if isinstance(got, str):
            got = [got]
        elif not isinstance(got, (list, tuple)):
            got = []
        chosen = []
        for item in got:
            canon = canonical_by_slug.get(taxonomy.slug(str(item)))
            if canon and canon not in chosen:
                chosen.append(canon)
        if layer in taxonomy.SINGLE_LABEL_LAYERS:
            chosen = chosen[:1]
        out[layer] = chosen
    return out


def _tags_to_cells(tags: dict) -> dict:
    return {LAYER_TO_TAG_COLUMN[layer]: ", ".join(vals)
            for layer, vals in tags.items() if layer in LAYER_TO_TAG_COLUMN}


# ---------------------------------------------------------------------------
# Single-row analysis
# ---------------------------------------------------------------------------
def analyze_row(row: dict, gemini, *, full_video_enabled: bool) -> dict:
    """Analyze one eligible row. Returns the writeback cell dict (never raises;
    failures come back as ANALYSIS_STATUS=Failed with an ERROR_MESSAGE)."""
    now = _now_iso()
    base = {"TAXONOMY_VERSION": TAXONOMY_VERSION, "LAST_UPDATED_AT": now}

    if gemini is None:
        # No model available — do not fake tags. Flag for a human.
        return {**base, "ANALYSIS_CONFIDENCE": CONF_LOW,
                "ANALYSIS_STATUS": "Needs Review",
                "ERROR_MESSAGE": "Gemini not configured; metadata-only tagging unavailable."}

    full_video_analyzed = False
    try:
        if full_video_enabled:
            text, full_video_analyzed = _full_video_generate(row, gemini)
        else:
            text = gemini.summarize_findings(build_metadata_prompt(row))

        from analyzer import parse_model_json
        tags = validate_tags(parse_model_json(text))
        cells = _tags_to_cells(tags)

        any_tags = any(v for v in tags.values())
        confidence = decide_confidence(row, full_video_analyzed)
        if not any_tags:
            # Model produced nothing usable → needs a human, low confidence.
            confidence = CONF_LOW
            return {**base, **cells, "ANALYSIS_CONFIDENCE": confidence,
                    "ANALYSIS_STATUS": "Needs Review",
                    "ERROR_MESSAGE": "No taxonomy tags could be derived from available inputs."}

        return {**base, **cells, "ANALYSIS_CONFIDENCE": confidence,
                "ANALYSIS_STATUS": status_for_confidence(confidence),
                "ERROR_MESSAGE": ""}
    except Exception as e:  # noqa: BLE001 - one row must not abort the run
        return {**base, "ANALYSIS_STATUS": "Failed",
                "ERROR_MESSAGE": f"{type(e).__name__}: {e}"[:400]}


def _full_video_generate(row: dict, gemini) -> tuple[str, bool]:
    """Download + upload + generate over the actual reel. Returns (text,
    analyzed=True). Reuses the existing GeminiClient acquisition helpers so
    cookies/limits are unchanged."""
    import os
    url = str(row.get("POST_URL", "")).strip()
    path = gemini._download(url)
    try:
        uploaded = gemini._upload_and_wait(path)
        text = gemini._generate([uploaded, build_metadata_prompt(row)])
        return text, True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def analyze_inspiration(sheets: Optional[InspirationSheets] = None,
                        gemini=None,
                        full_video_enabled: Optional[bool] = None) -> dict:
    """Tag every eligible EXTERNAL_INSPIRATION row in INSPIRATION_CONTENT.

    Returns a run-summary dict (also logged to INSPIRATION_RUNS, RUN_TYPE=Analyze).
    """
    sheets = sheets or InspirationSheets()
    if full_video_enabled is None:
        full_video_enabled = config.INSPIRATION_FULL_VIDEO_ANALYSIS

    # Lazily build Gemini; missing key must not crash — rows get Needs Review.
    if gemini is None:
        try:
            from gemini_client import GeminiClient
            gemini = GeminiClient()
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini unavailable for inspiration analysis: %s", e)
            gemini = None

    run = _new_run("Analyze", "gemini-metadata" if not full_video_enabled else "gemini-video")
    rows = sheets.read_content_rows()
    targets = [r for r in rows if eligible_for_analysis(r)]
    run["POSTS_DISCOVERED"] = len(targets)
    log.info("Inspiration analysis: %d eligible external row(s), full_video=%s",
             len(targets), full_video_enabled)
    errors: list[str] = []

    for r in targets:
        # Re-assert the SOURCE_TYPE guard right before any write.
        if str(r.get("SOURCE_TYPE", "")).strip() != SOURCE_TYPE_EXTERNAL:
            continue
        cells = analyze_row(r, gemini, full_video_enabled=full_video_enabled)
        try:
            sheets.update_content_cells(r["_row"], cells)
        except Exception as e:  # noqa: BLE001
            run["POSTS_FAILED"] += 1
            errors.append(f"row {r['_row']}: write failed: {e}")
            continue

        status = cells.get("ANALYSIS_STATUS")
        if status == "Failed":
            run["POSTS_FAILED"] += 1
            if cells.get("ERROR_MESSAGE"):
                errors.append(f"row {r['_row']}: {cells['ERROR_MESSAGE']}")
        else:
            run["POSTS_ANALYZED"] += 1
            if status == "Needs Review":
                run["POSTS_SHORTLISTED"] += 0  # (no shortlist yet; kept explicit)

    return _finalize_and_log_run(sheets, run, errors,
                                 failed=run["POSTS_FAILED"],
                                 total=len(targets))


def print_analyze_summary(run: dict) -> None:
    print("\nExternal inspiration analysis complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Provider:               {run.get('PROVIDER')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Eligible rows:          {run.get('POSTS_DISCOVERED')}")
    print(f"Analyzed:               {run.get('POSTS_ANALYZED')}")
    print(f"Failed:                 {run.get('POSTS_FAILED')}")
    if run.get("ERROR_SUMMARY"):
        print(f"Errors:                 {run.get('ERROR_SUMMARY')}")
