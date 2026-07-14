"""Milestone 3A — Storelli Winning Format Profiles (internal evidence only).

Builds reusable creative profiles from the COMPLETED, tagged Storelli evidence
base: internal rows with a real taxonomy tag AND a recognized PERFORMANCE
bucket. A profile summarizes the dominant creative pattern (hook/format/visual/
problem/solution/funnel) among the "Great" performers for a (Product, ICP)
group, with an internal sample size and a confidence grade.

Hard rule — external inspiration is NEVER proof:
- Only internal rows feed a profile. Any row that is external/inspiration
  (SOURCE_TYPE=EXTERNAL_INSPIRATION or a Source Type reference value) is dropped
  defensively before anything is counted — external views / follower ratio /
  priority score can never contribute to a profile's sample size or confidence.
- Row 8's permanent content failure (and any failed/untagged row) is naturally
  excluded: it is not `is_analyzed`, so it never enters the analyzed set.

This module reads the internal POC sheet + correlations; it only WRITES to the
WINNING_FORMAT_PROFILES tab. It never modifies internal completed rows.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

import performance
import taxonomy
from inspiration_scanner import _finalize_and_log_run, _new_run
from inspiration_sheets import InspirationSheets
from logger import get_logger

log = get_logger()

MIN_SAMPLE = 3                 # min supporting internal Great rows for a profile
HIGH_MIN_SAMPLE = 5           # High confidence needs at least this many...
HIGH_MIN_GREAT_RATE = 0.50    # ...and this Great-rate within the group

# layer -> WINNING_FORMAT_PROFILES tag column
LAYER_TO_PROFILE_COLUMN = {
    "hook": "HOOK_TAGS",
    "format": "FORMAT_TAGS",
    "visual_style": "VISUAL_STYLE_TAGS",
    "problem_type": "PROBLEM_TAGS",
    "solution_type": "SOLUTION_TAGS",
    "funnel_stage": "FUNNEL_STAGE_TAGS",
}


def _is_external(row: dict) -> bool:
    """Defensive: never let an external/inspiration row into internal evidence."""
    if str(row.get("SOURCE_TYPE", "")).strip().upper() == "EXTERNAL_INSPIRATION":
        return True
    return performance.is_reference_row(row)


def _dominant_tags(winners: list[dict], layer: str, threshold_ratio: float = 0.5):
    """Labels present in >= threshold of the winning rows for this layer, most
    common first. Single-label layers return at most one (the mode)."""
    sidx = taxonomy.signal_index()
    counts: Counter = Counter()
    for r in winners:
        for col, meta in sidx.items():
            if meta["layer"] == layer and str(r.get(col, "")).strip() == "1":
                counts[meta["label"]] += 1
    if not counts:
        return []
    need = max(1, round(threshold_ratio * len(winners)))
    picked = [label for label, n in counts.most_common() if n >= need]
    if layer in taxonomy.SINGLE_LABEL_LAYERS:
        picked = picked[:1] or [counts.most_common(1)[0][0]]
    return picked


def _supporting_signal_ids(winners: list[dict]) -> list[str]:
    """Signal-column keys that a majority of winners share — these are the
    Signal-Library-basis identifiers backing the profile."""
    sidx = taxonomy.signal_index()
    counts = Counter()
    for r in winners:
        for col in sidx:
            if str(r.get(col, "")).strip() == "1":
                counts[col] += 1
    need = max(1, round(0.5 * len(winners)))
    return [c for c, n in counts.most_common() if n >= need]


def _confidence(sample: int, great_rate: float) -> str:
    if sample >= HIGH_MIN_SAMPLE and great_rate >= HIGH_MIN_GREAT_RATE:
        return "High"
    if sample >= MIN_SAMPLE:
        return "Medium"
    return "Low"


def build_profiles(analyzed_rows: list[dict], buckets: dict,
                   min_sample: int = MIN_SAMPLE,
                   now_iso: str = "") -> list[dict]:
    """Pure: build profile dicts from internal analyzed rows + performance
    buckets. `results` (correlations) are not required here — sample size and
    within-group Great-rate carry the evidence strength."""
    internal = [r for r in analyzed_rows if not _is_external(r)]

    all_by_group: dict = defaultdict(list)
    win_by_group: dict = defaultdict(list)
    for r in internal:
        bucket = buckets.get(r["_row"], "")
        if not bucket:
            continue
        product = str(r.get("Product", "")).strip() or "(unspecified)"
        icp = str(r.get("ICP", "")).strip() or "(any)"
        key = (product, icp)
        all_by_group[key].append(r)
        if performance.is_positive(bucket):
            win_by_group[key].append(r)

    profiles = []
    for key, winners in sorted(win_by_group.items(), key=lambda kv: -len(kv[1])):
        product, icp = key
        sample = len(winners)
        if sample < min_sample:
            continue
        group_total = len(all_by_group[key]) or sample
        great_rate = sample / group_total
        confidence = _confidence(sample, great_rate)

        tags = {layer: _dominant_tags(winners, layer)
                for layer in LAYER_TO_PROFILE_COLUMN}
        urls = [str(r.get("LINK", "")).strip() for r in winners
                if str(r.get("LINK", "")).strip()][:10]

        top_hook = (tags["hook"] or ["—"])[0]
        top_format = (tags["format"] or ["—"])[0]
        profile = {
            "PROFILE_ID": _profile_id(product, icp),
            "ACTIVE": "TRUE" if confidence in ("High", "Medium") else "FALSE",
            "PROFILE_NAME": f"{product} / {icp}: {top_hook} + {top_format}",
            "PRODUCT": product,
            "ICP": icp,
            "INTERNAL_SAMPLE_SIZE": sample,
            "PERFORMANCE_SIGNAL": (f"Great in {sample}/{group_total} internal videos "
                                   f"({great_rate:.0%} Great rate)"),
            "CONFIDENCE": confidence,
            "SUPPORTING_LEARNING_IDS": ";".join(_supporting_signal_ids(winners)),
            "SUPPORTING_VIDEO_URLS": ";".join(urls),
            "LAST_REFRESHED_AT": now_iso,
            "NOTES": ("Built from internal Storelli evidence only (completed/tagged rows "
                      "with a performance bucket). External inspiration excluded — never proof."),
        }
        for layer, column in LAYER_TO_PROFILE_COLUMN.items():
            profile[column] = ", ".join(tags[layer])
        profiles.append(profile)
    return profiles


def _profile_id(product: str, icp: str) -> str:
    return f"WFP-{taxonomy.slug(product)}-{taxonomy.slug(icp)}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def build_winning_profiles(sheets: Optional[InspirationSheets] = None) -> dict:
    """Read internal evidence, build profiles, upsert into
    WINNING_FORMAT_PROFILES, and log the run (RUN_TYPE=Profiles)."""
    from main import compute_findings
    from sheets_client import SheetsClient

    internal_sheets = SheetsClient()
    internal_sheets.validate_columns()
    analyzed, buckets, _results = compute_findings(internal_sheets)

    sheets = sheets or InspirationSheets()
    run = _new_run("Profiles", "internal-evidence")
    run["POSTS_DISCOVERED"] = len(analyzed)     # internal analyzed rows considered

    profiles = build_profiles(analyzed, buckets, now_iso=_now_iso())
    errors: list[str] = []
    created = updated = 0
    try:
        created, updated = sheets.upsert_profiles(profiles)
    except Exception as e:  # noqa: BLE001
        errors.append(f"upsert failed: {e}")

    run["POSTS_ADDED"] = created
    run["POSTS_ANALYZED"] = updated              # reuse field: profiles updated
    run["POSTS_SHORTLISTED"] = sum(1 for p in profiles if p["ACTIVE"] == "TRUE")
    run["_profiles"] = profiles                  # for the caller's report (not a sheet col)
    log.info("Winning profiles: %d built (%d created, %d updated) from %d internal rows",
             len(profiles), created, updated, len(analyzed))
    return _finalize_and_log_run(sheets, run, errors, failed=len(errors), total=1)


def print_profiles_summary(run: dict) -> None:
    profiles = run.get("_profiles", [])
    print("\nWinning Format Profiles built (internal evidence only).\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Internal rows considered: {run.get('POSTS_DISCOVERED')}")
    print(f"Profiles created:       {run.get('POSTS_ADDED')}")
    print(f"Profiles updated:       {run.get('POSTS_ANALYZED')}")
    print(f"Active profiles:        {run.get('POSTS_SHORTLISTED')}")
    for p in profiles:
        print(f"\n  [{p['CONFIDENCE']}] {p['PROFILE_NAME']}  (ACTIVE={p['ACTIVE']})")
        print(f"    id={p['PROFILE_ID']} sample={p['INTERNAL_SAMPLE_SIZE']} :: {p['PERFORMANCE_SIGNAL']}")
        print(f"    hook={p['HOOK_TAGS']} | format={p['FORMAT_TAGS']} | visual={p['VISUAL_STYLE_TAGS']}")
        print(f"    problem={p['PROBLEM_TAGS']} | solution={p['SOLUTION_TAGS']} | funnel={p['FUNNEL_STAGE_TAGS']}")
