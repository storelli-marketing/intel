"""Social Analytics + Creative Test Planning layer (read-only).

The missing abstraction between the raw sheet and the Slack brain:

    raw metrics -> normalized analytics cohorts -> creative pattern extraction
                -> evidence / inspiration / bridge -> recommended tests

This module answers three concrete team questions extremely well, and — just as
importantly — refuses to invent data it doesn't have:

  1. "trial reels vs standard reels ... in terms of demographic?"  -> compare_trial_vs_standard
  2. "how many seconds long are our highest-performing reels?"     -> analyze_winning_reel_duration
  3. "give me a list of 20 ideas we should test"                   -> generate_creative_test_plan

Hard rules (inherited from the rest of the brain, enforced here too):
  * Internal Storelli rows are PROOF; external inspiration is execution
    reference only, never proof.
  * Metrics are never invented. If a field (demographics, duration, comments)
    isn't in the data, we say so plainly and name the exact field/backfill to add.
  * KPI outcomes aren't tracked, so every KPI line is an inferred / proxy bet.
  * Read-only: never writes the Sheet, never writes Notion, never triggers
    analysis, never touches cookies, never runs analyze-all.

The analytics functions read the *internal* POC worksheet through the existing
`SheetsClient` (same read-only path `social_brain` already uses); the creative
test plan draws on the existing brain (winning profiles, semantic connections,
refined ideas, calendar ratings, latest_learnings.md) via `InspirationSheets`.
Both data sources are fail-soft: an unreachable sheet degrades to an honest
"I can't reach that data" answer, never a crash or a fabricated one.
"""
from __future__ import annotations

import re
from typing import Optional

import config
import decision_trace as dt
import performance
import slack_response_style as st
import taxonomy
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"

# ---------------------------------------------------------------------------
# metric field vocabulary — each logical field maps to the sheet column-name
# aliases we recognize (case-insensitive, exact match on the header cell).
# ---------------------------------------------------------------------------
# Each logical field -> recognized column-name aliases (case-insensitive, exact
# cell match). We keep BOTH the short backward-compatible names (duration,
# followers, age, gender, location, follower split) AND the recommended
# production column names (DURATION_SECONDS, FOLLOWERS_AT_POST, *_SPLIT, ...) so
# adding the recommended columns lights the feature up with no further change.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("link", "url", "post url", "post_url", "reel url", "permalink"),
    "date": ("date", "posted", "post date", "published", "publish date", "posted at",
             "post_date"),
    "performance_label": ("performance", "performance label", "perf"),
    "views": ("views", "view count", "view_count", "plays", "play count"),
    "likes": ("likes", "like count", "like_count"),
    "comments": ("comments", "comment count", "comment_count"),
    "saves": ("saves", "saved", "bookmarks", "save count"),
    "shares": ("shares", "share count", "sends", "share_count"),
    "engagement_rate": ("engagement rate", "engagement_rate", "er", "engagement %",
                        "engagement percent", "engagement"),
    "followers": ("followers", "follower count", "follower_count", "audience size",
                  "followers_at_post", "followers at post",
                  "followers_at_measurement", "followers at measurement"),
    "duration": ("duration", "length", "seconds", "video length", "duration (s)",
                 "duration_sec", "runtime", "video_length", "reel length",
                 "duration_seconds", "duration seconds"),
    "post_type": ("post type", "post_type", "type", "content type", "media type", "media_type"),
    "reel_type": ("reel type", "reel_type", "trial", "trial reel", "trial/standard",
                  "distribution", "trial vs standard", "trial_or_standard"),
    "product": ("product",),
    "icp": ("icp",),
    "storytelling_structure": ("storytelling structure", "story structure", "storytelling",
                               "structure"),
    # demographics (IG audience-export fields) — almost never present today.
    "demo_age": ("age", "age range", "audience age", "age group", "age_split", "age split"),
    "demo_gender": ("gender", "audience gender", "sex", "gender_split", "gender split"),
    "demo_location": ("location", "country", "city", "top location", "audience location",
                      "geo", "location_split", "location split"),
    "demo_follower_split": ("follower vs non-follower", "non-follower reach", "from followers",
                            "follower_vs_nonfollower", "followers vs non-followers",
                            "non follower reach", "follower split", "follower_split",
                            "follower_nonfollower_split", "follower non-follower split",
                            "follower vs nonfollower split"),
    "demo_reach_segment": ("reach by audience segment", "audience segment", "reach by segment",
                           "audience breakdown", "reach_by_segment"),
    # optional funnel / distribution metrics (detected + reported; not part of the
    # core performance-metric hierarchy).
    "reach": ("reach",),
    "impressions": ("impressions", "impression count"),
    "profile_visits": ("profile visits", "profile_visits", "profile views"),
    "website_clicks": ("website clicks", "website_clicks", "link clicks", "link_clicks",
                       "external link taps"),
    "product_clicks": ("product clicks", "product_clicks", "product taps"),
    "trial_clicks": ("trial clicks", "trial_clicks", "trial cta clicks"),
    "qualified_dms": ("qualified dms", "qualified_dms", "qualified d ms", "qualified messages"),
}

_DEMO_FIELDS = ("demo_age", "demo_gender", "demo_location", "demo_follower_split",
                "demo_reach_segment")

_OPTIONAL_FIELDS = ("reach", "impressions", "profile_visits", "website_clicks",
                    "product_clicks", "trial_clicks", "qualified_dms")

# Missing-field -> the exact column name we recommend adding (Task 5 / schema plan).
_RECOMMENDED_COLUMNS = {
    "reel_type": "REEL_TYPE", "duration": "DURATION_SECONDS", "date": "POST_DATE",
    "timestamp": "POST_TIMESTAMP",
    "views": "VIEWS", "likes": "LIKES", "comments": "COMMENTS", "saves": "SAVES",
    "shares": "SHARES", "engagement_rate": "ENGAGEMENT_RATE", "followers": "FOLLOWERS_AT_POST",
    "demo_age": "AGE_SPLIT", "demo_gender": "GENDER_SPLIT", "demo_location": "LOCATION_SPLIT",
    "demo_follower_split": "FOLLOWER_NONFOLLOWER_SPLIT", "demo_reach_segment": "REACH_BY_SEGMENT",
}
_OPTIONAL_COLUMNS = ("REACH", "IMPRESSIONS", "PROFILE_VISITS", "WEBSITE_CLICKS",
                     "PRODUCT_CLICKS", "TRIAL_CLICKS", "QUALIFIED_DMS")

# Required metric columns to add to the POC tab, in insertion order.
_REQUIRED_METRIC_COLUMNS = ("REEL_TYPE", "DURATION_SECONDS", "POST_DATE",
                            "POST_TIMESTAMP", "VIEWS", "LIKES",
                            "COMMENTS", "SAVES", "SHARES", "ENGAGEMENT_RATE",
                            "FOLLOWERS_AT_POST", "AGE_SPLIT", "GENDER_SPLIT",
                            "LOCATION_SPLIT", "FOLLOWER_NONFOLLOWER_SPLIT")

# Manual IG-export paste tab (never written into POC automatically).
STAGING_TAB = "SOCIAL_METRICS_IMPORT_STAGING"
STAGING_COLUMNS = ("LINK", *_REQUIRED_METRIC_COLUMNS, *_OPTIONAL_COLUMNS,
                   "SOURCE", "IMPORTED_AT", "NOTES")

# Which staging columns validate as numbers vs demographic split strings vs text.
_NUMERIC_METRIC_COLUMNS = {"DURATION_SECONDS", "VIEWS", "LIKES", "COMMENTS", "SAVES", "SHARES",
                           "ENGAGEMENT_RATE", "FOLLOWERS_AT_POST", "REACH", "IMPRESSIONS",
                           "PROFILE_VISITS", "WEBSITE_CLICKS", "PRODUCT_CLICKS", "TRIAL_CLICKS",
                           "QUALIFIED_DMS"}
_SPLIT_METRIC_COLUMNS = {"AGE_SPLIT", "GENDER_SPLIT", "LOCATION_SPLIT",
                         "FOLLOWER_NONFOLLOWER_SPLIT"}
_TEXT_METRIC_COLUMNS = {"REEL_TYPE", "POST_DATE", "POST_TIMESTAMP", "SOURCE",
                        "IMPORTED_AT", "NOTES", "LINK"}

# Performance metric hierarchy (best first). Tier 1: engagement rate. Tier 2:
# saves/comments/shares. Tier 3: views/likes. Tier 4: the manual Great/Good/
# Weak label when no raw metric exists.
_METRIC_PRIORITY = ("engagement_rate", "saves", "comments", "shares", "views", "likes")
_PERF_RANK = {"great": 3, "good": 2, "ok": 1, "underdog": 0}

# Duration buckets (seconds).
_DURATION_BUCKETS = ((0, 5, "0–5 sec"), (6, 10, "6–10 sec"), (11, 15, "11–15 sec"),
                     (16, 30, "16–30 sec"), (31, 45, "31–45 sec"), (46, 10**9, "46+ sec"))


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------
def _lower(s) -> str:
    return str(s or "").strip().lower()


def _num(v):
    """Parse a possibly-messy numeric cell -> float, or None."""
    s = str(v if v is not None else "").strip().replace(",", "").replace("%", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _row_get_ci(row: dict, *aliases) -> str:
    """Case-insensitive metadata lookup by any alias."""
    lower = {str(k).strip().lower(): k for k in row}
    for a in aliases:
        actual = lower.get(a.strip().lower())
        if actual is not None and str(row[actual]).strip() != "":
            return str(row[actual]).strip()
    return ""


def _column_for_field(columns: list[str], field: str) -> Optional[str]:
    """Return the actual sheet column name backing `field`, or None."""
    have = {str(c).strip().lower(): str(c).strip() for c in (columns or [])}
    for alias in _FIELD_ALIASES.get(field, ()):
        if alias in have:
            return have[alias]
    return None


def _parse_seconds(v) -> Optional[float]:
    """Parse a duration cell into seconds. Accepts 12, '12', '12s', '0:12',
    '00:01:03'. Returns None when unparseable/absent."""
    s = str(v or "").strip().lower().replace("sec", "").replace("s", "").strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        secs = 0.0
        for n in nums:
            secs = secs * 60 + n
        return secs
    return _num(s)


def _duration_bucket(seconds: float) -> str:
    for lo, hi, label in _DURATION_BUCKETS:
        if lo <= seconds <= hi:
            return label
    return "46+ sec"


# ---------------------------------------------------------------------------
# data access (read-only, fail-soft) — the single indirection point so tests
# can inject fixtures and the golden harness can run fully offline.
# ---------------------------------------------------------------------------
def _internal_sheet() -> tuple[list[dict], list[str], str]:
    """Return (rows, column_names, error). Never raises.

    `column_names` are the metadata header cells actually present in the POC
    worksheet (this is how we know which raw-metric fields exist). `error` is a
    short string when the sheet is unreachable/unconfigured, else ''.
    """
    try:
        from sheets_client import SheetsClient
        sheets = SheetsClient()
        sheets.validate_columns()
        rows = sheets.read_rows()
        columns = list(sheets.meta_col.keys())
        return rows, columns, ""
    except Exception as e:  # noqa: BLE001 - Slack never sees a crash; degrade honestly
        log.warning("social_analytics: internal sheet unavailable: %s", e)
        return [], [], f"{type(e).__name__}: {e}"


def _brain():
    """Read-only snapshot of the existing brain (profiles / connections / ideas /
    calendar ratings / ad-hoc evals). Fail-soft -> empty lists."""
    def _rd(sheets, name):
        try:
            return getattr(sheets, name)()
        except Exception:  # noqa: BLE001
            return []
    try:
        from inspiration_sheets import InspirationSheets
        s = InspirationSheets()
    except Exception as e:  # noqa: BLE001
        log.warning("social_analytics: brain unavailable: %s", e)
        return {"profiles": [], "connections": [], "ideas": [], "calendar": []}
    return {
        "profiles": _rd(s, "read_profiles"),
        "connections": _rd(s, "read_semantic_connections"),
        "ideas": _rd(s, "read_ideas"),
        "calendar": _rd(s, "read_calendar_ratings"),
    }


# ---------------------------------------------------------------------------
# Content-audit tab — a coarse video-length proxy (buckets, not seconds) that
# already exists for ~half the analyzed reels. Read-only; single network
# indirection so tests/golden run offline.
# ---------------------------------------------------------------------------
_CONTENT_AUDIT_TAB = "Content audit"
_VIDEOLENGTH_PREFIX = "overall_videolength_"     # matched case-insensitively


def _read_named_worksheet(title: str) -> Optional[list[list[str]]]:
    """Read-only get_all_values() of a named tab in the configured spreadsheet.
    Never raises; returns None when unconfigured/unreachable/absent."""
    if not (config.GOOGLE_SHEET_ID and config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH):
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        return sh.worksheet(title).get_all_values()
    except Exception as e:  # noqa: BLE001
        log.warning("social_analytics: could not read tab %r: %s", title, e)
        return None


def _videolength_label(header: str) -> str:
    """'overall_videoLength_< 10 sec' -> '< 10 sec'."""
    return header.strip()[len(_VIDEOLENGTH_PREFIX):].strip() or header.strip()


def content_audit_duration_buckets(links: Optional[set] = None) -> dict:
    """Map LINK -> coarse video-length bucket label from the Content audit tab's
    `overall_videoLength_*` one-hot columns. Read-only; fail-soft -> {}.

    This is an *exact-duration proxy* (buckets, never seconds); it never becomes
    a fabricated seconds value."""
    vals = _read_named_worksheet(_CONTENT_AUDIT_TAB)
    if not vals or len(vals) < 2:
        return {}
    header = vals[0]
    link_idx = next((i for i, h in enumerate(header)
                     if str(h).strip().lower() in ("link", "url")), None)
    bucket_cols = [(i, _videolength_label(h)) for i, h in enumerate(header)
                   if str(h).strip().lower().startswith(_VIDEOLENGTH_PREFIX)]
    if link_idx is None or not bucket_cols:
        return {}
    want = set(links) if links else None
    out: dict[str, str] = {}
    for r in vals[1:]:
        if link_idx >= len(r):
            continue
        link = str(r[link_idx]).strip()
        if not link or (want is not None and link not in want):
            continue
        for ci, label in bucket_cols:
            if ci < len(r) and str(r[ci]).strip() == "1":
                out[link] = label
                break
    return out


# ---------------------------------------------------------------------------
# Part A — metrics audit
# ---------------------------------------------------------------------------
def detect_available_metrics(columns: Optional[list[str]] = None,
                             rows: Optional[list[dict]] = None) -> dict:
    """Report which logical metric fields exist in the current data.

    Returns {field: {"available": bool, "column": <sheet header or None>}}.
    `hook`/`format` are derived from the taxonomy tags (always modelled in the
    POC sheet), so they're reported available whenever the sheet is readable
    and any row carries a real (1) taxonomy tag.
    """
    if columns is None or rows is None:
        rows, columns, _err = _internal_sheet()
    out: dict[str, dict] = {}
    for field in _FIELD_ALIASES:
        col = _column_for_field(columns, field)
        out[field] = {"available": bool(col), "column": col}
    # hook / format come from the AI taxonomy layers, not a raw column.
    tagged = any(str(r.get(c, "")).strip() == "1"
                 for r in (rows or []) for c in taxonomy.all_signal_columns())
    for layer in ("hook", "format"):
        out[layer] = {"available": bool(tagged), "column": f"taxonomy:{layer}"}
    return out


def audit_metrics_schema(rows: Optional[list[dict]] = None,
                         columns: Optional[list[str]] = None) -> dict:
    """Full honest audit of what the internal data does / does not carry.

    Returns a structured dict (available / missing lists, demographics flag,
    duration flag, chosen performance metric, row count, error) plus a
    human-readable `report`.
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
    else:
        err = ""
    avail = detect_available_metrics(columns, rows)
    rows = rows or []
    n = len(rows)
    tagged = sum(1 for r in rows if any(str(r.get(c, "")).strip() == "1"
                                        for c in taxonomy.all_signal_columns()))

    # Everything the audit is asked to look for, in report order.
    order = ["url", "date", "performance_label", "views", "likes", "comments",
             "saves", "shares", "engagement_rate", "followers", "duration",
             "post_type", "reel_type", "product", "icp", "hook", "format",
             "storytelling_structure", "demo_age", "demo_gender", "demo_location",
             "demo_follower_split", "demo_reach_segment", *(_OPTIONAL_FIELDS)]
    available = [f for f in order if avail.get(f, {}).get("available")]
    missing = [f for f in order if not avail.get(f, {}).get("available")]

    # Per-field fill coverage (%) — offline, computed from the rows we have.
    coverage: dict[str, int] = {}
    for f in available:
        col = avail[f]["column"]
        if col and col.startswith("taxonomy:"):
            coverage[f] = round(100 * tagged / n) if n else 0
        elif col:
            filled = sum(1 for r in rows if str(r.get(col, "")).strip() != "")
            coverage[f] = round(100 * filled / n) if n else 0

    demographics_present = any(avail.get(f, {}).get("available") for f in _DEMO_FIELDS)
    duration_present = avail.get("duration", {}).get("available", False)
    trial_classifiable = _can_classify_reel_type(avail)
    missing_demographics = [_RECOMMENDED_COLUMNS[f] for f in _DEMO_FIELDS
                            if not avail.get(f, {}).get("available")]
    recommended_missing = [_RECOMMENDED_COLUMNS[f] for f in _RECOMMENDED_COLUMNS
                           if not avail.get(f, {}).get("available")]

    result = {
        "ok": not err,
        "error": err,
        "row_count": n,
        "available": available,
        "missing": missing,
        "fields": avail,
        "coverage": coverage,
        "demographics_present": demographics_present,
        # comparison is possible only when at least one split column exists AND
        # trial/standard cohorts can be separated.
        "demographic_comparison_possible": demographics_present and trial_classifiable,
        "missing_demographics": missing_demographics,
        "duration_present": duration_present,
        "reel_type_classifiable": trial_classifiable,
        "recommended_missing": recommended_missing,
        "chosen_metric": choose_metric(avail),
    }
    result["report"] = _render_audit_report(result)
    return result


def _render_audit_report(a: dict) -> str:
    if a["error"]:
        return f"Can't reach the analysis sheet right now ({a['error']})."
    lines = [f"Metrics audit — {a['row_count']} internal rows.",
             "Available: " + (", ".join(a["available"]) or "(none)"),
             "Missing: " + (", ".join(a["missing"]) or "(none)"),
             f"Demographics present: {'yes' if a['demographics_present'] else 'no'}",
             f"Duration present: {'yes' if a['duration_present'] else 'no'}",
             f"Trial vs standard classifiable: {'yes' if a['reel_type_classifiable'] else 'no'}",
             f"Best available performance metric: {a['chosen_metric']}"]
    return "\n".join(lines)


def _can_classify_reel_type(avail: dict) -> bool:
    """Trial vs standard is classifiable when there's an explicit reel/post-type
    column (a keyword-in-text fallback exists too, but that's decided per row)."""
    return bool(avail.get("reel_type", {}).get("available")
                or avail.get("post_type", {}).get("available"))


# ---------------------------------------------------------------------------
# Part A — reel-type classification + metric selection
# ---------------------------------------------------------------------------
def classify_reel_type(row: dict) -> str:
    """Classify a row as 'trial' | 'standard' | 'unknown'.

    Prefers an explicit Reel Type / Post Type / Trial column; falls back to a
    'trial' keyword in the storytelling-structure / notes / title text. Never
    guesses — an ambiguous row is 'unknown'.
    """
    explicit = _row_get_ci(row, "reel type", "reel_type", "trial", "trial reel",
                           "trial/standard", "distribution", "post type", "post_type",
                           "type")
    e = _lower(explicit)
    if e:
        if "trial" in e:
            return "trial"
        if any(k in e for k in ("standard", "normal", "regular", "organic", "default")):
            return "standard"
    text = " ".join(_lower(row.get(k, "")) for k in
                    ("Storytelling structure", "Notes", "notes", "Title", "title", "Caption"))
    if re.search(r"\btrial\b", text):
        return "trial"
    return "unknown"


def choose_metric(avail: dict) -> str:
    """Pick the best available performance metric per the hierarchy; falls back
    to the manual performance label when no raw metric exists."""
    for m in _METRIC_PRIORITY:
        if avail.get(m, {}).get("available"):
            return m
    return "performance_label"


def _metric_value(row: dict, metric: str) -> Optional[float]:
    """Numeric performance value for a row under the chosen metric. For
    'performance_label' it ranks Great>Good>Ok>Underdog."""
    if metric == "performance_label":
        return _PERF_RANK.get(_lower(row.get("PERFORMANCE", "")))
    aliases = _FIELD_ALIASES.get(metric, (metric,))
    return _num(_row_get_ci(row, *aliases))


# ---------------------------------------------------------------------------
# Part A / C — top performers + duration
# ---------------------------------------------------------------------------
def find_top_performing_posts(rows: Optional[list[dict]] = None,
                              columns: Optional[list[str]] = None,
                              limit: int = 5) -> list[dict]:
    """Rank INTERNAL posts by the best available metric and return the top ones.

    External / inspiration rows are excluded (never Storelli proof). Each result
    carries the link, the metric used, its value, and grouping fields.
    """
    if rows is None or columns is None:
        rows, columns, _err = _internal_sheet()
    avail = detect_available_metrics(columns, rows)
    metric = choose_metric(avail)
    scored = []
    for r in rows or []:
        if performance.is_reference_row(r):
            continue
        link = str(r.get("LINK", "")).strip()
        if not link:
            continue
        val = _metric_value(r, metric)
        if val is None:
            continue
        scored.append({
            "link": link,
            "metric": metric,
            "value": val,
            "performance": str(r.get("PERFORMANCE", "")).strip(),
            "product": str(r.get("Product", "")).strip(),
            "icp": str(r.get("ICP", "")).strip(),
            "structure": str(r.get("Storytelling structure", "")).strip(),
            "_row": r.get("_row"),
        })
    scored.sort(key=lambda d: d["value"], reverse=True)
    return scored[: max(1, limit)]


def analyze_winning_reel_duration(rows: Optional[list[dict]] = None,
                                  columns: Optional[list[str]] = None,
                                  audit_buckets: Optional[dict] = None) -> dict:
    """Analyze how long the highest-performing reels are, honestly.

    Cascade (never invents seconds):
      1. exact DURATION_SECONDS present -> median/avg/bucket (source='exact')
      2. else the Content audit coarse video-length buckets, if any of the top
         reels have one -> a clearly-labelled proxy read (source='content_audit_bucket')
      3. else -> duration missing + the exact backfill needed (source='none')

    `audit_buckets` (link->bucket label) can be injected for tests; when None and
    exact duration is missing it is read live from the Content audit tab.
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
    else:
        err = ""
    if err:
        return {"ok": False, "error": err, "duration_available": False, "source": "error"}

    avail = detect_available_metrics(columns, rows)
    metric = choose_metric(avail)
    duration_col = _column_for_field(columns, "duration")
    # "Winners": top ~quartile by the chosen metric, at least 5.
    n_internal = sum(1 for r in rows or [] if not performance.is_reference_row(r)
                     and str(r.get("LINK", "")).strip())
    top_n = max(5, round(n_internal * 0.25)) if n_internal else 5
    winners = find_top_performing_posts(rows, columns, limit=top_n)

    # ---- 1. exact seconds -------------------------------------------------
    durations, examples = [], []
    if duration_col:
        for w in winners:
            row = next((r for r in rows if r.get("_row") == w["_row"]), None)
            secs = _parse_seconds(row.get(duration_col)) if row else None
            if secs is None:
                continue
            durations.append(secs)
            examples.append({"link": w["link"], "seconds": secs, "performance": w["performance"]})
    if durations:
        durations_sorted = sorted(durations)
        n = len(durations_sorted)
        median = (durations_sorted[n // 2] if n % 2
                  else (durations_sorted[n // 2 - 1] + durations_sorted[n // 2]) / 2)
        avg = sum(durations_sorted) / n
        counts: dict[str, int] = {}
        for s in durations_sorted:
            counts[_duration_bucket(s)] = counts.get(_duration_bucket(s), 0) + 1
        return {"ok": True, "error": "", "duration_available": True, "source": "exact",
                "metric": metric, "count": n, "median": round(median, 1),
                "average": round(avg, 1),
                "common_bucket": max(counts.items(), key=lambda kv: kv[1])[0],
                "bucket_counts": counts,
                "examples": sorted(examples, key=lambda e: e["seconds"])[:3],
                "winners": winners}

    # ---- 2. Content audit coarse-bucket proxy -----------------------------
    winner_links = {w["link"] for w in winners if w["link"]}
    if audit_buckets is None:
        audit_buckets = content_audit_duration_buckets(winner_links)
    matched = {lk: b for lk, b in (audit_buckets or {}).items() if lk in winner_links}
    if matched:
        dist: dict[str, int] = {}
        for b in matched.values():
            dist[b] = dist.get(b, 0) + 1
        dominant = max(dist.items(), key=lambda kv: kv[1])[0]
        cov = round(100 * len(matched) / len(winner_links)) if winner_links else 0
        return {"ok": True, "error": "", "duration_available": False,
                "source": "content_audit_bucket", "metric": metric,
                "bucket_distribution": dist, "dominant_bucket": dominant,
                "coverage_pct": cov, "matched": len(matched),
                "total_winners": len(winner_links), "winners": winners,
                "backfill_field": "DURATION_SECONDS",
                "recommended_source": "yt-dlp metadata (info['duration'])"}

    # ---- 3. genuinely missing --------------------------------------------
    return {"ok": True, "error": "", "duration_available": False, "source": "none",
            "metric": metric, "winners": winners,
            "backfill_field": "DURATION_SECONDS",
            "recommended_source": "yt-dlp metadata (info['duration'])"}


# ---------------------------------------------------------------------------
# Part B — trial vs standard cohorts
# ---------------------------------------------------------------------------
def compare_trial_vs_standard(rows: Optional[list[dict]] = None,
                              columns: Optional[list[str]] = None) -> dict:
    """Compare trial vs standard reels on whatever dimensions actually exist.

    Honesty gates, in order:
      1. Can we even classify trial vs standard? If not, say so + name the field.
      2. Are demographic fields present? If not, say so and compare what IS
         available (performance split, hook/format mix, product mix, duration,
         sample size).
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
    else:
        err = ""
    if err:
        return {"ok": False, "error": err}

    avail = detect_available_metrics(columns, rows)
    internal = [r for r in rows or []
                if not performance.is_reference_row(r) and str(r.get("LINK", "")).strip()]
    cohorts = {"trial": [], "standard": [], "unknown": []}
    for r in internal:
        cohorts[classify_reel_type(r)].append(r)

    classifiable = bool(cohorts["trial"] or cohorts["standard"])
    demographics_present = any(avail.get(f, {}).get("available") for f in _DEMO_FIELDS)
    missing_demographics = [_RECOMMENDED_COLUMNS[f] for f in _DEMO_FIELDS
                            if not avail.get(f, {}).get("available")]

    result = {
        "ok": True, "error": "",
        "classifiable": classifiable,
        "demographics_present": demographics_present,
        "demographic_comparison_possible": demographics_present and classifiable,
        "missing_demographics": missing_demographics,
        "n_trial": len(cohorts["trial"]),
        "n_standard": len(cohorts["standard"]),
        "n_unknown": len(cohorts["unknown"]),
        "comparisons": {},
        "demographics": {},
        "available_dims": [],
    }
    if not classifiable:
        return result

    dims = result["comparisons"]
    dims["performance"] = {c: _perf_split(cohorts[c]) for c in ("trial", "standard")}
    dims["hook_format"] = {c: _hook_format_mix(cohorts[c]) for c in ("trial", "standard")}
    dims["product"] = {c: _group_mix(cohorts[c], "Product") for c in ("trial", "standard")}
    if avail.get("duration", {}).get("available"):
        dcol = _column_for_field(columns, "duration")
        dims["duration"] = {c: _avg_duration(cohorts[c], dcol) for c in ("trial", "standard")}
    # Demographic split comparison — only when the columns actually exist. Each
    # cohort's split is parsed and averaged; never fabricated.
    for field in _DEMO_FIELDS:
        if not avail.get(field, {}).get("available"):
            continue
        col = avail[field]["column"]
        comp = {c: _demo_compare(cohorts[c], col) for c in ("trial", "standard")}
        if comp["trial"] or comp["standard"]:
            result["demographics"][field] = comp
    result["available_dims"] = ["performance", "hook/format mix", "product mix"] + \
        (["duration"] if "duration" in dims else []) + \
        (["demographics"] if result["demographics"] else []) + ["sample size"]
    return result


def _perf_split(cohort: list[dict]) -> dict:
    out = {"Great": 0, "Good": 0, "Ok": 0, "Underdog": 0, "n": len(cohort)}
    for r in cohort:
        label = str(r.get("PERFORMANCE", "")).strip().title()
        if label in out:
            out[label] += 1
    return out


def _hook_format_mix(cohort: list[dict]) -> dict:
    idx = taxonomy.signal_index()
    counts: dict[str, int] = {}
    for r in cohort:
        for col, meta in idx.items():
            if meta["layer"] in ("hook", "format") and str(r.get(col, "")).strip() == "1":
                counts[meta["label"]] = counts.get(meta["label"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3])


def _group_mix(cohort: list[dict], key: str) -> dict:
    counts: dict[str, int] = {}
    for r in cohort:
        g = str(r.get(key, "")).strip() or "(unspecified)"
        counts[g] = counts.get(g, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3])


def _avg_duration(cohort: list[dict], dcol: str) -> Optional[float]:
    secs = [s for s in (_parse_seconds(r.get(dcol)) for r in cohort) if s is not None]
    return round(sum(secs) / len(secs), 1) if secs else None


# Parses IG-style split strings: "F 58% / M 42%", "18-24 34% / 25-34 41%",
# "US 60% / UK 12%" -> {label: pct}. Returns {} when nothing parses (never
# fabricates a demographic).
_SPLIT_PART_RE = re.compile(r"(.+?)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%")


def _parse_split(text) -> dict:
    out: dict[str, float] = {}
    for chunk in re.split(r"[/;,]", str(text or "")):
        m = _SPLIT_PART_RE.search(chunk)
        if not m:
            continue
        label = m.group(1).strip().strip("-–—:=•* ").strip()
        if label:
            out[label] = float(m.group(2))
    return out


def _demo_compare(cohort: list[dict], col: str) -> dict:
    """Average of a demographic split across a cohort -> {label: mean pct}."""
    agg: dict[str, float] = {}
    n = 0
    for r in cohort:
        parsed = _parse_split(r.get(col, ""))
        if parsed:
            n += 1
            for k, v in parsed.items():
                agg[k] = agg.get(k, 0.0) + v
    return {k: round(v / n, 1) for k, v in agg.items()} if n else {}


# ---------------------------------------------------------------------------
# Part D — creative test plan (analysis -> inspiration -> bridge -> hypothesis)
# ---------------------------------------------------------------------------
def _split_urls(cell) -> list[str]:
    return [u.strip() for u in re.split(r"[;\n,]", str(cell or "")) if u.strip().startswith("http")]


def _active_profiles(brain) -> list[dict]:
    return [p for p in brain.get("profiles", [])
            if _lower(p.get("ACTIVE")) == "true"
            and _lower(p.get("CONFIDENCE")) in ("medium", "high")]


def _product_family(text: str) -> str:
    """Loose product family so 'BodyShield' matches 'BodyShield GK Leggings' etc."""
    t = _lower(text)
    if any(k in t for k in ("bodyshield", "leggings", "pants", "slider", "shorts", " leg")):
        return "leggings"
    if "glove" in t:
        return "gloves"
    if any(k in t for k in ("head guard", "headguard", "exoshield")):
        return "head"
    return ""


def _matches_focus(product: str, focus_product: str) -> bool:
    """True when no focus is set, or the product is in the same family as the
    focus (canonical-name mismatches like 'BodyShield' vs 'BodyShield GK
    Leggings' still match) — used to PREFER, never to hard-drop."""
    if not focus_product:
        return True
    ff = _product_family(focus_product)
    return not ff or _product_family(product) == ff


def _connection_for(product: str, brain) -> Optional[dict]:
    """Best same-family semantic connection, or None. Deliberately does NOT fall
    back to a different product's connection — a Gloves test must not borrow a
    BodyShield reference/structure as if it were its own."""
    fam = _product_family(product)
    pool = [c for c in brain.get("connections", [])
            if _product_family(c.get("PRODUCT", "")) == fam] if fam \
        else list(brain.get("connections", []))
    if not pool:
        return None
    return sorted(pool, key=lambda c: _num(c.get("CONNECTION_SCORE")) or 0, reverse=True)[0]


# Test-angle matrix: each angle turns internal proof into a distinct LEARNING
# goal, so a plan is hypotheses (not a generic idea dump). Extension angles are
# explicitly exploratory (thin — no internal proof yet).
_ANGLES = (
    ("baseline", "does our proven {hook} + {fmt} still win when tightened to one pain beat",
     "confirms the winning format holds", "High"),
    ("hook-swap", "does a sharper pain-confession hook beat our current {hook} open",
     "a stronger hook variant to standardize", "Medium"),
    ("length-test", "does a sub-10s cut hold retention better than our {fmt} norm",
     "the ideal reel length for this format", "Medium"),
    ("comment-driver", "does ending on a question drive more replies than a plain CTA",
     "whether question-CTAs lift comments (inferred)", "Medium"),
    ("format-variant", "does a Do/Don't cut of the same proof outperform the straight {fmt}",
     "a second format that carries the same proof", "Medium"),
    ("icp-extension", "does the {product} proof translate to a Parents/youth angle",
     "whether the format extends to a new ICP (no internal proof yet)", "Thin"),
)


def generate_creative_test_plan(count: int = 20, brain: Optional[dict] = None,
                                focus_product: str = "", focus_icp: str = "") -> dict:
    """Build a ranked creative TEST plan (not a generic idea dump).

    Every test follows: internal analysis anchor -> external inspiration
    (reference only) -> creative bridge (semantic connection / storytelling
    structure) -> test hypothesis -> KPI/proxy -> risk -> sources. Anchored to
    real internal winning profiles / refined ideas; extension angles that lack
    internal proof are labelled exploratory, never proven.
    """
    brain = brain if brain is not None else _brain()
    profiles = _active_profiles(brain)
    ideas = sorted(brain.get("ideas", []), key=lambda i: _num(i.get("IDEA_SCORE")) or 0,
                   reverse=True)
    # Prefer (never hard-drop) items matching the requested product, so a focused
    # ask surfaces the right ones first but the plan can still reach `count`.
    if focus_product:
        profiles.sort(key=lambda p: _matches_focus(str(p.get("PRODUCT", "")), focus_product),
                      reverse=True)
        ideas.sort(key=lambda i: _matches_focus(str(i.get("PRODUCT", "")), focus_product),
                   reverse=True)
    tests: list[dict] = []
    seen = set()

    def _add(t: dict) -> None:
        key = (t["product"], t["icp"], t["angle"])
        if key in seen:
            return
        seen.add(key)
        tests.append(t)

    # 1) Anchor a test on each refined/rated idea (strongest, real internal proof).
    for idea in ideas:
        if len(tests) >= count:
            break
        product = str(idea.get("PRODUCT", "")).strip() or focus_product or "the product"
        icp = str(idea.get("ICP", "")).strip() or focus_icp or "General"
        conn = _connection_for(product, brain)
        _add(_build_test(
            angle="idea-anchored",
            product=product, icp=icp,
            hook=_first(idea.get("HOOK_TAGS")) or "Curiosity Gap",
            fmt=_first(idea.get("FORMAT_TAGS")) or "Demo",
            structure=(conn or {}).get("STORYTELLING_STRUCTURE")
            or _first(idea.get("STORYTELLING_STRUCTURE")) or "Pain → Demo → Protected replay → CTA",
            title=idea.get("REFINED_IDEA_TITLE") or idea.get("IDEA_TITLE") or f"{product} test",
            hypothesis=f"does '{(idea.get('REFINED_IDEA_TITLE') or idea.get('IDEA_TITLE') or product)}' "
                       f"convert its proof into a repeatable win",
            proves="this specific idea is worth scaling",
            confidence="High",
            internal_urls=_split_urls(idea.get("INTERNAL_EVIDENCE_URLS")),
            profile_name=str(idea.get("SOURCE_PROFILE_NAME", "")),
            conn=conn))

    # 2) Fan out proven winning profiles across the test-angle matrix.
    for prof in profiles:
        product = str(prof.get("PRODUCT", "")).strip() or "the product"
        icp = str(prof.get("ICP", "")).strip() or focus_icp or "General"
        hook = _first(prof.get("HOOK_TAGS")) or "Curiosity Gap"
        fmt = _first(prof.get("FORMAT_TAGS")) or "Demo"
        conn = _connection_for(product, brain)
        structure = (conn or {}).get("STORYTELLING_STRUCTURE") or \
            "Pain → Demo → Protected replay → CTA"
        for angle, hyp_tmpl, proves, conf in _ANGLES:
            if len(tests) >= count:
                break
            eff_icp = "Parents" if angle == "icp-extension" else icp
            _add(_build_test(
                angle=angle, product=product, icp=eff_icp, hook=hook, fmt=fmt,
                structure=structure, title=f"{product} — {angle} test",
                hypothesis=hyp_tmpl.format(hook=hook, fmt=fmt, product=product),
                proves=proves, confidence=conf,
                internal_urls=_split_urls(prof.get("SUPPORTING_VIDEO_URLS")),
                profile_name=str(prof.get("PROFILE_NAME", "")), conn=conn))
        if len(tests) >= count:
            break

    return {"requested": count, "returned": len(tests), "tests": tests[:count],
            "short": len(tests) < count,
            "thin": not profiles and not ideas}


def _first(cell) -> str:
    for part in re.split(r"[;,]", str(cell or "")):
        if part.strip():
            return part.strip()
    return ""


def _build_test(angle, product, icp, hook, fmt, structure, title, hypothesis,
                proves, confidence, internal_urls, profile_name, conn) -> dict:
    ext_urls = _split_urls((conn or {}).get("EXTERNAL_REFERENCE_URLS"))
    return {
        "angle": angle, "title": title, "product": product, "icp": icp,
        "hook": hook, "format": fmt, "structure": structure,
        "internal_basis": profile_name or "Storelli winning profile",
        "internal_urls": internal_urls,
        "inspiration_ref": _handle(ext_urls[0]) if ext_urls else "",
        "external_urls": ext_urls[:1],
        "bridge": (conn or {}).get("CONCEPT_NAME", "") or structure,
        "hypothesis": hypothesis,
        "kpi_proxy": dt.kpi_value(structure or hook),
        "proves": proves,
        "fails": "the mechanism doesn't carry this angle — stop investing in it",
        "shootability": "High" if angle in ("baseline", "idea-anchored", "length-test") else "Medium",
        "risk": "keep the pain beat tasteful; don't overclaim injury prevention"
        if confidence != "Thin" else "no internal proof yet — treat as a cheap exploratory test",
        "confidence": confidence,
    }


def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url or "") or \
        re.search(r"instagram\.com/([\w.\-]+)/", url or "")
    return "@" + m.group(1) if m else "external reference"


# ---------------------------------------------------------------------------
# Slack rendering
# ---------------------------------------------------------------------------
class _Sources:
    """Ordered [S]/[E]/[C]/[N] source collector -> clickable Slack block."""

    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, kind: str, url: str, label: str) -> str:
        n = sum(1 for (t, _, _) in self.rows if t[0] == kind) + 1
        tag = f"{kind}{n}"
        self.rows.append((tag, str(url or "").strip(), label))
        return tag

    @property
    def has_external(self) -> bool:
        return any(t[0] == "E" for (t, _, _) in self.rows)

    def block(self) -> str:
        if not self.rows:
            return ""
        lines = ["*Sources:*"]
        for tag, url, label in self.rows:
            lines.append(f"  [{tag}] <{url}|{label}>" if url else f"  [{tag}] {label}")
        if self.has_external:
            lines.append(_NOT_PROOF)
        return "\n".join(lines)


def _fmt_split(split: dict) -> str:
    parts = [f"{k} {split[k]}" for k in ("Great", "Good", "Ok", "Underdog") if split.get(k)]
    return (", ".join(parts) or "no labels") + f" (n={split.get('n', 0)})"


_DEMO_LABELS = {"demo_age": "Age", "demo_gender": "Gender", "demo_location": "Location",
                "demo_follower_split": "Followers", "demo_reach_segment": "Reach segment"}


def _fmt_demo(split: Optional[dict]) -> str:
    if not split:
        return "n/a"
    top = sorted(split.items(), key=lambda kv: kv[1], reverse=True)[:2]
    return ", ".join(f"{k} {v:g}%" for k, v in top)


def _render_trial_vs_standard(text: str, cmp: dict) -> str:
    mode = st.detect_response_mode(text)
    if not cmp.get("ok"):
        return dt.render(
            f"I can't reach the analysis sheet right now ({cmp.get('error', 'unknown')}).",
            [dt.step("Data check", "internal sheet unreachable", [], "risk", "Thin")],
            move="retry once the Sheet/service-account is configured.", mode=mode)

    if not cmp["classifiable"]:
        steps = [
            dt.step("Data check", "no trial/standard indicator in the data", [], "risk", "Thin"),
            dt.step("Cohort split", "can't separate trial vs standard reels", [], "inference", "Thin"),
        ]
        return dt.render(
            "I can't split trial vs standard yet — there's no trial/standard indicator in the data.",
            steps,
            move="add a 'Reel Type' column (Trial/Standard) — ideally the IG trial-reel flag — "
                 "then I can compare them.", mode=mode)

    dims = cmp["comparisons"]
    demo = cmp.get("demographics", {})
    src = _Sources()
    perf = dims.get("performance", {})
    steps = [
        dt.step("Cohort split",
                f"trial n={cmp['n_trial']} vs standard n={cmp['n_standard']}", [], "topic", "Medium"),
        dt.step("Performance",
                f"trial {_fmt_split(perf.get('trial', {}))}; standard {_fmt_split(perf.get('standard', {}))}",
                [], "internal", "Medium"),
    ]
    hf = dims.get("hook_format", {})
    steps.append(dt.step("Hook/format",
                         f"trial: {', '.join(hf.get('trial', {}) ) or 'n/a'}; "
                         f"standard: {', '.join(hf.get('standard', {})) or 'n/a'}",
                         [], "format", "Medium"))
    if "duration" in dims:
        d = dims["duration"]
        steps.append(dt.step("Duration",
                             f"trial {d.get('trial')}s vs standard {d.get('standard')}s avg",
                             [], "inference", "Medium"))

    if demo:
        # Real demographic comparison (columns exist and parsed).
        for field, comp in list(demo.items())[:2]:
            label = _DEMO_LABELS.get(field, field)
            steps.append(dt.step(label, f"trial {_fmt_demo(comp.get('trial'))}; "
                                         f"standard {_fmt_demo(comp.get('standard'))}",
                                  [], "internal", "Medium"))
        lead = "Here's trial vs standard, including the demographic split we have:"
        move = "prioritize the cohort/segment that over-indexes on your target ICP."
    else:
        # Honest: no demographic data — say which columns are missing.
        steps.insert(0, dt.step("Data check", "demographic fields not in the data",
                                [], "risk", "Thin"))
        lead = ("I can compare trial vs standard on the data we have, but *not demographics* — "
                "demographic fields aren't in the current data.")
        miss = ", ".join(cmp.get("missing_demographics", [])) or \
            "AGE_SPLIT, GENDER_SPLIT, LOCATION_SPLIT, FOLLOWER_NONFOLLOWER_SPLIT"
        move = (f"comparing {'; '.join(cmp['available_dims'])}. For a real demographic split, add "
                f"{miss} from an IG audience export.")
    steps.append(dt.kpi_step("engagement"))
    return dt.render(lead, steps, move=move, sources=src.block(), mode=mode)


def _render_demographics(text: str, cmp: dict) -> str:
    """A pure demographics ask (no trial/standard framing)."""
    mode = st.detect_response_mode(text)
    if cmp.get("ok") and cmp.get("demographics") :
        return _render_trial_vs_standard(text, cmp)
    miss = ", ".join(cmp.get("missing_demographics", [])) or \
        "AGE_SPLIT, GENDER_SPLIT, LOCATION_SPLIT, FOLLOWER_NONFOLLOWER_SPLIT"
    steps = [
        dt.step("Data check", f"missing: {miss}", [], "risk", "Thin"),
        dt.step("What we have", "performance, product/ICP, hook/format, structure", [], "topic", "Medium"),
    ]
    return dt.render(
        "I don't have demographic-level data for this yet — age, gender, location and "
        "follower-vs-non-follower aren't in the current data.",
        steps,
        move=f"add {miss} from an IG audience export (or connect the IG Insights API) and I can "
             "break audience down for real.", mode=mode)


def _render_duration(text: str, d: dict) -> str:
    mode = st.detect_response_mode(text)
    source = d.get("source")
    if not d.get("ok"):
        return dt.render(f"I can't reach the analysis sheet right now ({d.get('error', 'unknown')}).",
                         [dt.step("Data check", "internal sheet unreachable", [], "risk", "Thin")],
                         move="retry once Sheets is configured.", mode=mode)

    # ---- exact seconds ----------------------------------------------------
    if source == "exact":
        src = _Sources()
        for ex in d.get("examples", [])[:3]:
            src.add("S", ex["link"],
                    f"Storelli reel — {ex['seconds']:.0f}s ({ex.get('performance') or 'top'})")
        steps = [
            dt.step("Metric used", f"top performers by {d['metric']}", [], "internal", "Medium"),
            dt.step("Pattern found",
                    f"median {d['median']:.0f}s, avg {d['average']:.0f}s across {d['count']} winners",
                    [t for (t, _, _) in src.rows[:1]], "internal", "High"),
            dt.step("Most common", f"{d['common_bucket']}", [], "topic", "Medium"),
            dt.kpi_step("retention"),
        ]
        lead = (f"Our highest-performing reels cluster around *{d['common_bucket']}* — "
                f"median {d['median']:.0f}s, average {d['average']:.0f}s.")
        return dt.render(lead, steps,
                         move=f"keep top reels in the {d['common_bucket']} range; test a tighter "
                              "cut against it.", sources=src.block(), mode=mode)

    # ---- Content audit coarse-bucket proxy --------------------------------
    if source == "content_audit_bucket":
        steps = [
            dt.step("Metric used", f"ranked winners by {d.get('metric')}", [], "internal", "Medium"),
            dt.step("Duration proxy", "Content audit video-length bucket", [], "topic", "Medium"),
            dt.step("Pattern", f"strongest reels mostly sit in {d['dominant_bucket']}",
                    [], "internal", "Medium"),
            dt.step("Caveat", "bucketed, not exact seconds", [], "risk", "Thin"),
        ]
        lead = (f"I don't have exact duration seconds yet, but I do have coarse video-length "
                f"buckets for ~{d['coverage_pct']}% of the top reels ({d['matched']}/"
                f"{d['total_winners']}).")
        return dt.render(lead, steps,
                         move="add a `DURATION_SECONDS` column for exact length analysis.",
                         mode=mode)

    # ---- genuinely missing ------------------------------------------------
    steps = [
        dt.step("Data check", "no duration/seconds field on the reels", [], "risk", "Thin"),
        dt.step("Metric used", f"ranked winners by {d.get('metric')}", [], "internal", "Medium"),
    ]
    return dt.render(
        "I can't tell you the length of our best reels yet — there's no duration field, and the "
        "Content audit length buckets don't cover the top reels.",
        steps,
        move="store `DURATION_SECONDS` from yt-dlp metadata (`info['duration']`) going forward; "
             "a safe one-time backfill reads duration from each reel's metadata only "
             "(no video re-analysis, no re-tagging).", mode=mode)


def _render_test_plan(text: str, plan: dict) -> str:
    """Compact, CEO-readable numbered test plan with sources grouped at bottom."""
    tests = plan.get("tests", [])
    if not tests:
        return dt.render(
            "I don't have enough internal evidence to build a grounded test plan yet.",
            [dt.step("Data check", "no active winning profiles or rated ideas", [], "risk", "Thin")],
            move="run build-winning-profiles / generate-ideas first, then I can plan tests "
                 "anchored to real proof.", mode=st.MODE_DEEP)

    src = _Sources()
    # one grouped source per distinct internal profile + external reference used
    prof_tag: dict[str, str] = {}
    ext_tag: dict[str, str] = {}
    lines = []
    for i, t in enumerate(tests, 1):
        pkey = t["internal_basis"]
        if pkey not in prof_tag:
            url = t["internal_urls"][0] if t["internal_urls"] else ""
            prof_tag[pkey] = src.add("S", url, f"Storelli proof — {pkey[:38]}")
        s_ref = prof_tag[pkey]
        refs = f"[{s_ref}]"
        if t["external_urls"]:
            ekey = t["external_urls"][0]
            if ekey not in ext_tag:
                ext_tag[ekey] = src.add("E", ekey, f"External reference — {t['inspiration_ref']}")
            refs += f"[{ext_tag[ekey]}]"
        tag = "" if t["confidence"] != "Thin" else " _(exploratory — no internal proof yet)_"
        # one line per test: anchor · product/ICP · structure · learning goal · KPI proxy
        lines.append(
            f"{i}. *{t['product']} / {t['icp']}* — {t['structure']}. "
            f"Test: {t['hypothesis']}; KPI: {t['kpi_proxy']} (proxy). {refs}{tag}")

    header = (f"Test plan — {len(tests)} experiments, each anchored to internal proof, "
              f"using external work as execution reference only:")
    if plan.get("short"):
        header += (f" (only {len(tests)} are grounded enough to propose — "
                   f"add more winning profiles/ideas to reach {plan.get('requested', len(tests))}.)")
    body = header + "\n" + "\n".join(lines)
    tail = src.block()
    text_out = body + ("\n\n" + tail if tail else "")
    # An explicit "list of N" is a scannable numbered list, not a 3-bullet CEO
    # answer — so we strip canned endings + number/linkify but do NOT apply the
    # word cap (which would trim items and desync the header count). Each item is
    # already one compact line.
    return st.format_trace_answer(st.remove_canned_endings(text_out))


# ---------------------------------------------------------------------------
# public Slack entrypoints
# ---------------------------------------------------------------------------
_DURATION_KW = ("how long", "how many seconds", "seconds long", "reel length", "best reel length",
                "duration", "length of our", "how long are our best", "highest performing reels duration",
                "best reels duration")
_DEMO_KW = ("demographic", "demographics", "audience split", "audience breakdown",
            "audience demographic", "who is watching", "who watches")
_TRIAL_KW = ("trial reel", "trial reels", "trial vs standard", "trial versus standard",
             "standard reels", "trial and standard")
_AUDIT_KW = ("metrics audit", "audit our metrics", "what metrics do we", "what data do we have",
             "what analytics do we")
# view/follower ratio questions are an analytics read, not a generic pattern ask.
_RATIO_KW = ("relative to audience size", "relative to our audience", "views relative",
             "view to follower", "views per follower", "view/follower", "punched above")
# Schema-plan asks ("what fields do we need to add", "what do we need to track…").
_SCHEMA_KW = ("need to add", "need to track", "fields do we need", "columns do we need",
              "what fields", "what columns", "what should we add", "what to add",
              "which fields", "which columns", "fields to add", "columns to add",
              "what data do we need", "schema plan", "what do we need to add",
              "what metrics should we add", "what do we need to measure",
              "what do we need to collect")
# Import + schema-setup Slack asks (kept narrow so they don't shadow other routes).
_MISSING_METRICS_KW = ("what metrics are missing", "which metrics are missing", "missing metrics",
                       "what metrics do we not have", "what metrics don't we have",
                       "what social metrics are missing", "which social metrics are missing")
_IMPORT_KW = ("how do i import", "how to import", "import ig metrics", "import metrics",
              "import social metrics", "paste ig", "paste the ig", "upload ig metrics",
              "how do we import", "import the metrics", "import instagram")
_BUCKET_USE_KW = ("use the content audit", "content audit duration bucket", "content audit buckets",
                  "use content audit", "can we use the content audit", "content-audit bucket")
_BUCKET_PERF_KW = ("duration bucket performs", "bucket performs best", "best duration bucket",
                   "which duration bucket", "what duration bucket", "best performing bucket")
# Automatic Instagram ingestion Slack asks (status only — Slack never applies).
_IG_CONFIG_KW = ("instagram metrics configured", "ig metrics configured", "is instagram configured",
                 "are ig metrics configured", "instagram api configured", "ig api configured",
                 "are instagram metrics set up")
_IG_PULL_KW = ("pull instagram metrics", "pull ig metrics", "refresh ig metrics",
               "refresh instagram metrics", "update the metrics automatically",
               "update metrics automatically", "automatically pull", "auto-pull",
               "can you update the metrics", "fetch instagram metrics", "sync instagram metrics")
_DEMO_MISSING_KW = ("why are demographics missing", "why is demographic data missing",
                    "why no demographics", "demographics missing", "why are the demographics",
                    "why don't we have demographics")
_IG_CONNECTED_KW = ("ig metrics connected", "instagram metrics connected",
                    "are ig metrics connected", "are instagram metrics connected",
                    "is instagram connected", "is ig connected")
# Metric-status asks (read the live sheet + sync ledger; no secrets).
_LAST_REFRESH_KW = ("last refreshed", "when were metrics", "last metrics refresh",
                    "when did we refresh", "last sync", "when was the last refresh")
_REELS_WITH_METRICS_KW = ("how many reels have metrics", "reels have metrics",
                          "how many have metrics", "reels with metrics")
_TRACKING_KW = ("metrics are we tracking", "metrics do we track", "what are we tracking",
                "which metrics are we tracking", "what metrics are we actually tracking")
_MISSING_REELS_KW = ("reels missing metrics", "any reels missing", "reels without metrics",
                     "reels are missing metrics", "which reels are missing")
_WHAT_CHANGED_KW = ("what changed in our content", "what changed in performance",
                    "what has changed", "what's changed")
# Self-updating intelligence status (from the refresh run history).
_BRAIN_STATUS_KW = ("brain last update", "brain last refresh", "when did the brain",
                    "is the brain up to date", "did we find anything new",
                    "what changed since the last refresh", "new patterns emerge",
                    "new inspiration did we find", "winning profiles change",
                    "should we regenerate ideas", "did the brain refresh",
                    "last intelligence refresh", "brain refreshed", "brain up to date",
                    "anything new this week", "did our profiles change", "did anything fail",
                    "when will it refresh", "when does it refresh", "next refresh",
                    "is the brain healthy", "brain health", "what needs attention",
                    "new storelli videos", "new storelli reels")

_TESTPLAN_STRONG = ("test plan", "creative test plan", "testing plan", "ideas to test",
                    "ideas we should test", "ideas we can test", "test ideas", "ideas to run as tests")
_TESTPLAN_ASK = ("what should we test", "what tests should we run", "what should we test next",
                 "which formats should we test", "what to test next", "tests should we run next",
                 "what should we test based", "what do we test next", "what can we test",
                 "experiments should we run", "experiments to run", "experiments should we try")


def is_schema_plan_query(text: str, context: Optional[list] = None) -> bool:
    t = " " + _lower(text) + " "
    return any(k in t for k in _SCHEMA_KW)


def _schema_focus(text: str) -> str:
    t = _lower(text)
    if any(k in t for k in ("demographic", "audience", "age", "gender", "location", "follower")):
        return "demographics"
    if any(k in t for k in ("duration", "seconds", "length", "how long")):
        return "duration"
    return "all"


def is_social_analytics_query(text: str, context: Optional[list] = None) -> bool:
    t = " " + _lower(text) + " "
    if is_schema_plan_query(text, context):
        return True
    if any(k in t for k in _MISSING_METRICS_KW + _IMPORT_KW + _BUCKET_USE_KW + _BUCKET_PERF_KW):
        return True
    if any(k in t for k in _IG_CONFIG_KW + _IG_PULL_KW + _DEMO_MISSING_KW + _IG_CONNECTED_KW
           + _LAST_REFRESH_KW + _REELS_WITH_METRICS_KW + _TRACKING_KW + _MISSING_REELS_KW
           + _WHAT_CHANGED_KW + _BRAIN_STATUS_KW):
        return True
    if any(k in t for k in _TRIAL_KW):
        return True
    if any(k in t for k in _DEMO_KW):
        return True
    if any(k in t for k in _AUDIT_KW + _RATIO_KW):
        return True
    # duration must be about reels/posts, not "how long should X be"
    if any(k in t for k in _DURATION_KW) and any(w in t for w in ("reel", "reels", "video", "post", "content", "best")):
        return True
    return False


def is_creative_test_plan_query(text: str, context: Optional[list] = None) -> bool:
    t = " " + _lower(text) + " "
    # Defer genuinely urgency-framed asks to the existing orchestrator.
    if "urgent" in t or "most urgent" in t:
        return False
    if any(k in t for k in _TESTPLAN_STRONG):
        return True
    if any(k in t for k in _TESTPLAN_ASK):
        return True
    # "give me a list of 20 ideas we should test" / "20 ideas to test"
    if re.search(r"\b\d+\b", t) and "idea" in t and ("test" in t or "experiment" in t):
        return True
    return False


def _parse_count(text: str, default: int = 20) -> int:
    m = re.search(r"\b(\d{1,3})\b", text or "")
    if not m:
        return default
    n = int(m.group(1))
    return max(1, min(n, 40))


_INSERT_LOCATION = ("Insert them in the *Marketing brain POC* tab *between `Status` and the first "
                    "taxonomy category `HOOK`* — leave the top category row (row 1) blank above "
                    "each and put the column name in row 2. Don't append them to the far right "
                    "(the header forward-fill would misread them as taxonomy columns).")


def render_schema_plan(text: str, context: Optional[list] = None) -> str:
    """Answer 'what fields do we need to add?' / '…to track demographics?' /
    '…reel duration?' with required columns, the exact insertion location, and
    what can be backfilled vs what needs IG Insights. Read-only."""
    mode = st.detect_response_mode(text)
    focus = _schema_focus(text)
    if focus == "duration":
        steps = [
            dt.step("Add column", "`DURATION_SECONDS` (integer seconds)", [], "topic", "High"),
            dt.step("Backfill", "yt-dlp metadata info['duration'] — may need cookies", [], "inference", "Medium"),
            dt.step("Available now", "coarse Content audit length buckets (~half of reels)", [], "internal", "Medium"),
            dt.step("Placement", "between Status and HOOK; row 1 blank", [], "topic", "Medium"),
        ]
        return dt.render(
            "To answer reel-duration questions exactly, add one column: *DURATION_SECONDS*.",
            steps, move=_INSERT_LOCATION, mode=mode)
    if focus == "demographics":
        cols = "AGE_SPLIT, GENDER_SPLIT, LOCATION_SPLIT, FOLLOWER_NONFOLLOWER_SPLIT"
        steps = [
            dt.step("Add columns", cols, [], "topic", "High"),
            dt.step("Source", "IG Insights export/API — cannot come from video metadata", [], "risk", "Medium"),
            dt.step("Format", "'F 58% / M 42%', '18-24 34% / 25-34 41%'", [], "topic", "Medium"),
            dt.step("Placement", "between Status and HOOK; row 1 blank", [], "topic", "Medium"),
        ]
        return dt.render(
            "To compare audience demographics I need four columns the sheet doesn't have yet.",
            steps,
            move=f"add {cols} from an IG audience export; {_INSERT_LOCATION}", mode=mode)
    # focus == all
    req = "REEL_TYPE, DURATION_SECONDS, POST_DATE, VIEWS, LIKES, COMMENTS, SAVES, SHARES, " \
          "ENGAGEMENT_RATE, FOLLOWERS_AT_POST, AGE_SPLIT, GENDER_SPLIT, LOCATION_SPLIT, " \
          "FOLLOWER_NONFOLLOWER_SPLIT"
    steps = [
        dt.step("Required", "14 columns: reel-type, duration, date, engagement, demographics",
                [], "topic", "High"),
        dt.step("Backfillable", "DURATION_SECONDS via yt-dlp metadata (cookie-gated)", [], "inference", "Medium"),
        dt.step("Needs IG export", "views/likes/comments/saves/shares + all demographics", [], "risk", "Medium"),
        dt.step("Placement", "between Status and HOOK; row 1 blank, row 2 = name", [], "topic", "Medium"),
    ]
    return dt.render(
        f"To answer all the social-metrics questions, add these columns: {req}.",
        steps,
        move=(f"{_INSERT_LOCATION} Optional extras: {', '.join(_OPTIONAL_COLUMNS)}."), mode=mode)



def _render_top_by_ratio(text: str) -> str:
    """Top performers by views relative to audience size — uses the real metric
    when present, and says so honestly when the inputs aren't there."""
    mode = st.detect_response_mode(text)
    rows, columns, err = _internal_sheet()
    if err:
        return dt.render(f"I can't reach the analysis sheet right now ({err}).",
                         [dt.step("Data check", "sheet unreachable", [], "risk", "Thin")],
                         move="retry once Sheets is configured.", mode=mode)
    avail = detect_available_metrics(columns, rows)
    if not avail.get("views", {}).get("available"):
        return dt.render(
            "I can't rank by views-per-follower yet — we don't have a views column populated.",
            [dt.step("Missing", "VIEWS (and a follower count) per reel", [], "risk", "Thin")],
            move="add VIEWS + FOLLOWERS_AT_MEASUREMENT (the refresh fills these from public "
                 "Apify data).", mode=mode)
    fcol = _column_for_field(columns, "followers")
    top = find_top_performing_posts(rows, columns, limit=25)
    scored = []
    for t in top:
        row = next((r for r in rows if r.get("_row") == t["_row"]), {})
        followers = _num(row.get(fcol)) if fcol else None
        denom = followers or float(config.STORELLI_IG_FOLLOWER_COUNT or 0)
        if denom > 0 and t["metric"] == "views":
            scored.append((t["value"] / denom, t, bool(followers)))
    if not scored:
        return dt.render("I don't have enough views/follower data to rank that yet.",
                         [dt.step("Missing", "views or follower denominator", [], "risk", "Thin")],
                         move="run the refresh to populate public metrics.", mode=mode)
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[:3]
    exact = all(x[2] for x in best)
    steps = [dt.step("Metric used", "views ÷ followers at measurement", [], "internal", "Medium"),
             dt.step("Top ratio", f"{best[0][0]:.2f}× audience", [], "internal", "Medium")]
    if not exact:
        steps.append(dt.step("Caveat", "some rows use the configured follower fallback "
                                       "(approximate)", [], "risk", "Thin"))
    src = _Sources()
    for _r, t, _e in best:
        src.add("S", t["link"], f"Storelli reel — {t['value']:.0f} views")
    return dt.render(
        f"These punched furthest above our audience size — top is {best[0][0]:.2f}× followers.",
        steps, move="reuse the hook/format from the top one; it travelled beyond the base.",
        sources=src.block(), mode=mode)


def _render_missing_metrics(text: str) -> str:
    mode = st.detect_response_mode(text)
    a = audit_metrics_schema()
    if not a.get("ok"):
        return dt.render(f"I can't reach the sheet right now ({a.get('error')}).",
                         [dt.step("Data check", "sheet unreachable", [], "risk", "Thin")],
                         move="retry once Sheets is configured.", mode=mode)
    miss = a["recommended_missing"]
    steps = [
        dt.step("Have", ", ".join(a["available"][:6]) or "none", [], "topic", "Medium"),
        dt.step("Missing", ", ".join(miss[:8]) or "none", [], "risk", "Medium"),
        dt.step("Duration proxy",
                "Content audit buckets" if a["duration_present"] is False else "exact",
                [], "inference", "Thin"),
    ]
    return dt.render(
        f"We're missing {len(miss)} of the key social-metrics fields in the sheet.",
        steps,
        move="ask 'what fields do we need to add?' for the exact columns + where to put them, "
             "then paste an IG export into SOCIAL_METRICS_IMPORT_STAGING.", mode=mode)


def _render_import_howto(text: str) -> str:
    mode = st.detect_response_mode(text)
    steps = [
        dt.step("Step 1", "add the metric columns to the POC tab (schema plan)", [], "topic", "Medium"),
        dt.step("Step 2", f"paste the IG export into the {STAGING_TAB} tab", [], "topic", "Medium"),
        dt.step("Step 3", "run import-social-metrics --dry-run to preview matches", [], "internal", "Medium"),
        dt.step("Safety", "matched by LINK; never overwrites filled cells; nothing auto-writes",
                [], "risk", "Medium"),
    ]
    return dt.render(
        "Import IG metrics via the staging tab, not by editing analyzed rows directly.",
        steps,
        move=f"paste your IG Insights export columns into {STAGING_TAB} (one row per reel, LINK "
             "required), then I'll dry-run the match before anything is written.", mode=mode)


def _render_bucket_usability(text: str) -> str:
    mode = st.detect_response_mode(text)
    a = audit_duration_buckets()
    cov = (f"{a['rows_with_bucket']}/{a['total_rows']}" if a.get("ok") else "some")
    steps = [
        dt.step("Proxy", "Content audit video-length buckets", [], "internal", "Medium"),
        dt.step("Coverage", f"{cov} analyzed reels have a bucket", [], "topic", "Medium"),
        dt.step("Caveat", "coarse buckets, not exact seconds", [], "risk", "Thin"),
    ]
    return dt.render(
        "Yes — we can use the Content audit duration buckets as a coarse proxy while exact "
        "seconds are missing.",
        steps,
        move="I already fall back to them for duration questions; add DURATION_SECONDS when you "
             "want exact medians.", mode=mode)


def _render_best_bucket(text: str) -> str:
    mode = st.detect_response_mode(text)
    a = audit_duration_buckets()
    if not a.get("ok") or not a.get("distribution"):
        return dt.render(
            "I don't have Content audit duration buckets matched to performance yet.",
            [dt.step("Data check", "no bucket/performance overlap", [], "risk", "Thin")],
            move="add DURATION_SECONDS (or populate Content audit buckets) for a length read.",
            mode=mode)
    steps = [
        dt.step("Metric used", "PERFORMANCE label (no raw metrics)", [], "internal", "Medium"),
        dt.step("Duration proxy", "Content audit bucket", [], "topic", "Medium"),
    ]
    if a["best_bucket"]:
        steps.append(dt.step("Best bucket", f"{a['best_bucket']} ({a['best_great_rate']}% Great)",
                             [], "internal", "Medium"))
        lead = f"Best-performing length bucket so far: *{a['best_bucket']}*."
    else:
        steps.append(dt.step("Caveat", "too few reels per bucket (n<2)", [], "risk", "Thin"))
        lead = "I can't reliably pick a best length bucket yet — too few reels per bucket."
    steps.append(dt.step("Caveat", "bucketed, not exact seconds", [], "risk", "Thin"))
    return dt.render(lead, steps,
                     move="add DURATION_SECONDS for an exact median, then re-check.", mode=mode)


def _render_ig_ingest_status(text: str) -> str:
    """Config/status answer for automatic IG ingestion. Slack NEVER applies a
    write — it reports status and points at the exact CLI/dashboard action."""
    mode = st.detect_response_mode(text)
    if config.instagram_configured():
        steps = [
            dt.step("Status", "Instagram API configured", [], "topic", "Medium"),
            dt.step("Scope", "Storelli-owned media only (official API)", [], "internal", "Medium"),
            dt.step("Safety", "dry-run first; apply is gated + fills empty cells only",
                    [], "risk", "Medium"),
        ]
        return dt.render(
            "Instagram metrics ingestion is configured — I can pull owned-media metrics.",
            steps,
            move="run `pull-instagram-metrics --dry-run` (CLI or the dashboard button) to preview, "
                 "then `--apply` when it's SAFE. I never auto-apply from Slack.", mode=mode)
    steps = [
        dt.step("Status", "Instagram API not configured", [], "risk", "Medium"),
        dt.step("Missing", ", ".join(config.instagram_missing_vars()), [], "risk", "Medium"),
        dt.step("Permissions", "instagram_manage_insights + instagram_basic", [], "topic", "Thin"),
    ]
    return dt.render(
        config.IG_INGEST_NOT_CONFIGURED_MSG,
        steps,
        move="add the Storelli Instagram API credentials (or paste an export into "
             "SOCIAL_METRICS_IMPORT_STAGING as a fallback). I never apply writes from Slack.",
        mode=mode)


def _render_demographics_missing(text: str) -> str:
    """Honest 'why are demographics missing' explanation — account-level only."""
    mode = st.detect_response_mode(text)
    configured = config.instagram_configured()
    steps = [
        dt.step("Why", "Instagram exposes demographics at the ACCOUNT level, not per reel",
                [], "risk", "Medium"),
        dt.step("Per-reel", "no per-reel demographic source exists", [], "inference", "Thin"),
        dt.step("Config", "IG API connected" if configured else "IG API not connected yet",
                [], "topic", "Medium"),
    ]
    lead = ("Demographics are missing because Instagram only gives audience splits at the "
            "account level, never per individual reel.")
    move = ("account-level splits go to the INSTAGRAM_ACCOUNT_INSIGHTS tab; per-reel demographics "
            "aren't available, so I won't fake them."
            if configured else
            "connect the IG API for account-level demographics (stored separately, not per-reel) — "
            "I won't fabricate per-reel splits.")
    return dt.render(lead, steps, move=move, mode=mode)


def _render_brain_status(text: str) -> str:
    """Conversational answer about the self-updating brain, from run history —
    last refresh, what changed, whether ideas should regenerate. Not a dashboard
    dump."""
    mode = st.detect_response_mode(text)
    t = _lower(text)
    try:
        import intelligence_refresh as ir
        runs = ir.last_runs(n=1)
    except Exception as e:  # noqa: BLE001
        log.warning("brain status failed: %s", e)
        runs = []

    def _n(row, key):
        try:
            return int(str(row.get(key, "0") or 0))
        except (ValueError, TypeError):
            return 0

    # health / next-run / failure sub-intents use the readiness+health layer
    if any(k in t for k in ("when will it refresh", "when does it refresh", "next refresh")):
        try:
            note = ir.next_scheduled_note()
        except Exception:  # noqa: BLE001
            note = "not scheduled yet"
        return dt.render(f"Next refresh: {note}.",
                         [dt.step("Cadence", note, [], "topic", "Medium")],
                         move="enable the weekly Railway Cron to make it automatic.", mode=mode)
    if any(k in t for k in ("healthy", "brain health", "needs attention", "up to date",
                            "did anything fail")):
        try:
            h = ir.health_state()
        except Exception:  # noqa: BLE001
            h = {"state": "BLOCKED", "reasons": ["status unavailable"]}
        state = h["state"]
        lead = {"HEALTHY": "The brain is healthy and current.",
                "PARTIAL": "The brain is mostly working, but something needs attention.",
                "BLOCKED": "The brain can't refresh right now.",
                "STALE": "The brain is overdue for a refresh."}.get(state, state)
        steps = [dt.step("Health", state, [], "topic", "Medium")]
        for reason in (h.get("reasons") or [])[:3]:
            steps.append(dt.step("Needs", reason, [], "risk", "Thin"))
        return dt.render(lead, steps,
                         move=("all good — next weekly run will keep it current."
                               if state == "HEALTHY"
                               else "address the items above (see `refresh-readiness`)."), mode=mode)

    if not runs:
        return dt.render(
            "The brain hasn't run an automatic refresh yet — no refresh history.",
            [dt.step("Status", "no scheduled refresh recorded", [], "topic", "Thin")],
            move="run `refresh-intelligence --dry-run` to preview, then schedule it weekly "
                 "(Railway Cron). I'll report each run here.", mode=mode)
    r = runs[0]
    when = r.get("FINISHED_AT") or r.get("STARTED_AT") or "recently"
    new_media, analyzed = _n(r, "INTERNAL_NEW_MEDIA"), _n(r, "INTERNAL_ANALYZED")
    ext_added, q80 = _n(r, "EXTERNAL_ADDED"), _n(r, "EXTERNAL_QUALITY_80")
    profiles = _n(r, "PROFILES_UPDATED")
    regen = str(r.get("IDEA_REGEN_RECOMMENDED", "")).strip().lower() in ("true", "1", "yes")

    # focused sub-intents
    if "regenerate" in t:
        lead = ("Yes — new evidence justifies regenerating the idea pool."
                if regen else "Not yet — I wouldn't regenerate the idea pool.")
        steps = [dt.step("Signal", f"new profiles {profiles}, quality refs +{q80}, "
                                    f"new inspiration +{ext_added}", [], "internal", "Medium")]
        return dt.render(lead, steps,
                         move=("run generate-ideas / refine-ideas to refresh the pool."
                               if regen else "leave the rated ideas as-is until evidence shifts more."),
                         mode=mode)
    if "profile" in t:
        lead = (f"Winning profiles updated: {profiles}." if profiles
                else "No winning-profile change in the last refresh.")
        return dt.render(lead, [dt.step("Last refresh", when, [], "topic", "Medium")],
                         move="ask 'should we regenerate ideas?' to see if that shifts the pool.",
                         mode=mode)
    if "inspiration" in t:
        return dt.render(
            f"We added {ext_added} new external references last refresh ({q80} high-quality).",
            [dt.step("Reminder", "external is execution reference, not proof", [], "risk", "Thin")],
            move="external stays reference-only; it never becomes Storelli proof.", mode=mode)
    # default: overall status / what changed / up to date
    steps = [
        dt.step("Last refresh", f"{when} ({r.get('STATUS', '?')})", [], "topic", "Medium"),
        dt.step("Internal", f"{new_media} new reels detected, {analyzed} newly analyzed, "
                            f"profiles {profiles}", [], "internal", "Medium"),
        dt.step("External", f"{ext_added} new references ({q80} high-quality) — reference only",
                [], "topic", "Medium"),
        dt.step("Regenerate ideas?", "recommended" if regen else "not yet", [], "inference", "Thin"),
    ]
    lead = f"Brain last refreshed {when}."
    if new_media == 0 and analyzed == 0 and ext_added == 0:
        lead += " Nothing materially new since then."
    return dt.render(lead, steps,
                     move=("new evidence — worth regenerating ideas." if regen
                           else "no idea-pool change needed yet."), mode=mode)


def _render_ig_metrics_status(text: str) -> str:
    """Short status: reels with metrics, tracked columns, missing, last refresh.
    Reads the live sheet + sync ledger; never exposes tokens/secrets."""
    mode = st.detect_response_mode(text)
    t = _lower(text)
    try:
        import social_metrics_ingest as smi
        s = smi.metrics_status()
    except Exception as e:  # noqa: BLE001
        log.warning("metrics_status failed: %s", e)
        s = {"ok": False}
    if not s.get("ok"):
        return dt.render("I can't reach the metrics sheet right now.",
                         [dt.step("Data check", "sheet unreachable", [], "risk", "Thin")],
                         move="retry once Sheets is configured.", mode=mode)
    if any(k in t for k in _LAST_REFRESH_KW + _WHAT_CHANGED_KW):
        last = s["last_refresh"] or "never — no automatic refresh has run yet"
        steps = [dt.step("Last refresh", last, [], "topic", "Medium"),
                 dt.step("Coverage", f"{s['reels_with_metrics']}/{s['reels_total']} reels have metrics",
                         [], "internal", "Medium")]
        return dt.render(f"Metrics last refreshed: {last}.", steps,
                         move="run `refresh-instagram-metrics --apply` (once IG is connected) to update.",
                         mode=mode)
    if any(k in t for k in _MISSING_REELS_KW):
        steps = [dt.step("Missing", f"{s['reels_missing']} of {s['reels_total']} reels have no metrics",
                         [], "risk", "Medium"),
                 dt.step("Fix", "connect IG + refresh-instagram-metrics", [], "topic", "Thin")]
        return dt.render(f"{s['reels_missing']} reels are missing metrics.", steps,
                         move="connect the IG API and run refresh-instagram-metrics to fill them.",
                         mode=mode)
    if any(k in t for k in _TRACKING_KW):
        cov = s["coverage"]
        pop = ", ".join(f"{c} ({cov.get(c, 0)})" for c in s["tracked_columns"][:8]) or "none populated yet"
        steps = [dt.step("Columns", ", ".join(s["tracked_columns"][:10]) or "none", [], "topic", "Medium"),
                 dt.step("Populated", pop, [], "internal", "Medium")]
        return dt.render("Metric columns we track (with how many reels have each):", steps,
                         move="fill them automatically via refresh-instagram-metrics.", mode=mode)
    # default: how many reels have metrics
    steps = [dt.step("With metrics", f"{s['reels_with_metrics']}/{s['reels_total']} reels",
                     [], "internal", "Medium"),
             dt.step("Missing", str(s["reels_missing"]), [], "risk", "Thin")]
    return dt.render(f"{s['reels_with_metrics']} of {s['reels_total']} reels have metrics.", steps,
                     move="run refresh-instagram-metrics to fill the rest (once IG is connected).",
                     mode=mode)


def answer_social_analytics_question(text: str, context: Optional[list] = None) -> Optional[str]:
    """Slack entrypoint for analytics questions (IG ingest status, schema plan,
    import/staging, duration buckets, trial/standard, demographics, duration,
    metrics audit). Returns None if it doesn't own it."""
    t = _lower(text)
    try:
        # Self-updating intelligence status (from refresh run history).
        if any(k in t for k in _BRAIN_STATUS_KW):
            return _render_brain_status(text)
        # Automatic-ingestion status + demographics-why first (never applies).
        if any(k in t for k in _IG_CONFIG_KW + _IG_CONNECTED_KW + _IG_PULL_KW):
            return _render_ig_ingest_status(text)
        if any(k in t for k in _DEMO_MISSING_KW):
            return _render_demographics_missing(text)
        # Metric-status asks (reels-missing BEFORE the schema 'missing metrics').
        if any(k in t for k in _LAST_REFRESH_KW + _REELS_WITH_METRICS_KW + _TRACKING_KW
               + _MISSING_REELS_KW + _WHAT_CHANGED_KW):
            return _render_ig_metrics_status(text)
        # Schema-plan asks next — "what do we need to track to answer X" mentions
        # duration/demographics but wants the columns-to-add answer, not the data.
        if is_schema_plan_query(text, context):
            return render_schema_plan(text, context)
        if any(k in t for k in _MISSING_METRICS_KW):
            return _render_missing_metrics(text)
        if any(k in t for k in _IMPORT_KW):
            return _render_import_howto(text)
        if any(k in t for k in _BUCKET_PERF_KW):     # before generic duration
            return _render_best_bucket(text)
        if any(k in t for k in _BUCKET_USE_KW):
            return _render_bucket_usability(text)
        if any(k in t for k in _DURATION_KW):
            return _render_duration(text, analyze_winning_reel_duration())
        if any(k in t for k in _TRIAL_KW):
            return _render_trial_vs_standard(text, compare_trial_vs_standard())
        if any(k in t for k in _DEMO_KW):
            return _render_demographics(text, compare_trial_vs_standard())
        if any(k in t for k in _RATIO_KW):
            return _render_top_by_ratio(text)
        if any(k in t for k in _AUDIT_KW):
            a = audit_metrics_schema()
            mode = st.detect_response_mode(text)
            steps = [dt.step("Available", ", ".join(a["available"][:8]) or "none", [], "topic", "Medium"),
                     dt.step("Missing", ", ".join(a["missing"][:8]) or "none", [], "risk", "Medium"),
                     dt.step("Best metric", a["chosen_metric"], [], "internal", "Medium")]
            return dt.render(
                f"Here's what our data actually carries ({a['row_count']} internal rows):",
                steps,
                move=("add duration + IG audience-export fields to unlock length and demographic "
                      "analysis." if not (a["duration_present"] and a["demographics_present"])
                      else "data coverage looks good."), mode=mode)
    except Exception as e:  # noqa: BLE001 - never break the bot
        log.warning("social_analytics answer failed: %s", e)
        return None
    return None


def answer_creative_test_plan(text: str, context: Optional[list] = None) -> Optional[str]:
    """Slack entrypoint for 'give me N ideas to test' / 'what should we test next'."""
    try:
        import interpretation
        count = _parse_count(text, default=20 if any(k in _lower(text) for k in _TESTPLAN_STRONG) else 8)
        focus_product = interpretation.detect_product(text) or ""
        focus_icp = interpretation.detect_icp(text) or ""
        plan = generate_creative_test_plan(count=count, focus_product=focus_product,
                                           focus_icp=focus_icp)
        return _render_test_plan(text, plan)
    except Exception as e:  # noqa: BLE001
        log.warning("social_analytics test plan failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CLI: audit-social-metrics (read-only) + backfill-duration-metadata (dry-run)
# ---------------------------------------------------------------------------
def audit_social_metrics_report(check_content_audit: bool = True) -> str:
    """Read-only plain-text audit for the CLI. Reads the live POC tab (and, if
    reachable, the Content audit duration buckets). Writes nothing."""
    rows, columns, err = _internal_sheet()
    if err:
        return f"audit-social-metrics: can't reach the sheet ({err}). Nothing written."
    a = audit_metrics_schema(rows=rows, columns=columns)

    # Content audit coarse duration buckets (read-only; may be unreachable).
    ca_line = "Content audit duration buckets: not checked"
    if check_content_audit:
        links = {str(r.get("LINK", "")).strip() for r in rows if str(r.get("LINK", "")).strip()}
        buckets = content_audit_duration_buckets(links)
        if buckets:
            cov = round(100 * len(buckets) / len(links)) if links else 0
            ca_line = (f"Content audit duration buckets: PRESENT — {len(buckets)}/{len(links)} "
                       f"linked reels ({cov}%), coarse buckets not exact seconds")
        else:
            ca_line = "Content audit duration buckets: none found (or tab unreachable)"

    cov = a.get("coverage", {})
    out = ["Social metrics audit (read-only — nothing written)",
           f"Worksheet rows: {a['row_count']}",
           "",
           "AVAILABLE (with fill coverage):"]
    for f in a["available"]:
        out.append(f"  - {f:26} {cov.get(f, 0)}%")
    out += ["", "MISSING:"]
    for f in a["missing"]:
        rec = _RECOMMENDED_COLUMNS.get(f)
        out.append(f"  - {f}" + (f"  -> add {rec}" if rec else "  (optional)"))
    out += ["",
            f"Trial vs standard classifiable: {'YES' if a['reel_type_classifiable'] else 'NO'}",
            f"Demographic comparison possible: {'YES' if a['demographic_comparison_possible'] else 'NO'}"
            + ("" if a['demographic_comparison_possible']
               else f" (missing {', '.join(a['missing_demographics'])})"),
            f"Exact duration (DURATION_SECONDS): {'YES' if a['duration_present'] else 'NO'}",
            ca_line,
            f"Best available performance metric: {a['chosen_metric']}",
            "",
            "Recommended columns to add: " + (", ".join(a["recommended_missing"]) or "(none)"),
            "Insert between Status and HOOK (row 1 blank, row 2 = column name)."]
    return "\n".join(out)


def duration_backfill_candidates() -> dict:
    """Read-only: identify POC rows that COULD receive a DURATION_SECONDS value
    (have a LINK, no existing exact duration). Writes nothing."""
    rows, columns, err = _internal_sheet()
    if err:
        return {"ok": False, "error": err, "candidates": []}
    dcol = _column_for_field(columns, "duration")
    candidates = []
    for r in rows:
        link = str(r.get("LINK", "")).strip()
        if not link or performance.is_reference_row(r):
            continue
        existing = _parse_seconds(r.get(dcol)) if dcol else None
        if existing is None:
            candidates.append({"row": r.get("_row"), "link": link})
    return {"ok": True, "error": "", "has_duration_column": bool(dcol),
            "candidates": candidates, "total": len(candidates)}


def backfill_duration_dry_run(sample: int = 3, probe: bool = False) -> str:
    """DRY-RUN ONLY. Lists rows that could receive DURATION_SECONDS. Metadata-only,
    no Gemini, no taxonomy re-tagging, NO WRITES. Optionally probes a tiny sample
    for yt-dlp `info['duration']` (metadata only, no cookies added); reports
    cleanly if yt-dlp/IG metadata isn't reachable. There is no non-dry-run mode."""
    info = duration_backfill_candidates()
    if not info["ok"]:
        return f"backfill-duration-metadata --dry-run: can't reach the sheet ({info['error']})."
    lines = ["backfill-duration-metadata --dry-run (NO WRITES)",
             f"DURATION_SECONDS column present: {'yes' if info['has_duration_column'] else 'no'}",
             f"Rows that could receive DURATION_SECONDS: {info['total']}"]
    for c in info["candidates"][:10]:
        lines.append(f"  - row {c['row']}: {c['link']}")
    if info["total"] > 10:
        lines.append(f"  … and {info['total'] - 10} more")

    if probe and info["candidates"]:
        lines.append("")
        lines.append(f"Metadata probe (sample of {min(sample, len(info['candidates']))}, "
                     "metadata-only, no cookies added):")
        try:
            import yt_dlp  # noqa: F401
        except Exception:  # noqa: BLE001
            lines.append("  yt-dlp not available in this environment — cannot probe. "
                         "(Metadata access may also require the IG cookie session.)")
            return "\n".join(lines)
        for c in info["candidates"][:sample]:
            secs = _probe_duration_metadata(c["link"])
            lines.append(f"  - row {c['row']}: "
                         + (f"would set DURATION_SECONDS={secs:.0f}" if secs is not None
                            else "metadata unavailable (likely needs cookies) — skipped"))
    else:
        lines.append("")
        lines.append("Pass --probe to attempt a small metadata-only sample (no writes either way).")
    lines.append("")
    lines.append("Dry-run only: nothing was written to the Sheet, Notion, or any row.")
    return "\n".join(lines)


def _probe_duration_metadata(url: str) -> Optional[float]:
    """Best-effort metadata-only duration via yt-dlp (no download, no cookies).
    Returns seconds or None. Never raises."""
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=False)
        d = meta.get("duration") if isinstance(meta, dict) else None
        return float(d) if d is not None else None
    except Exception:  # noqa: BLE001
        return None


# ===========================================================================
# Sheet-schema setup + metrics import workflow (Import + Schema Setup)
# ===========================================================================
def _norm_link(u) -> str:
    """Normalize an IG/TikTok URL for matching (drop query/trailing slash/case)."""
    return str(u or "").strip().lower().split("?")[0].rstrip("/")


# ---------------------------------------------------------------------------
# Task 1/2 — preflight the POC header + build the exact insertion plan
# ---------------------------------------------------------------------------
def _poc_values() -> Optional[list]:
    """Raw get_all_values() of the POC worksheet (read-only). None on failure."""
    try:
        from sheets_client import SheetsClient
        return SheetsClient().values
    except Exception as e:  # noqa: BLE001
        log.warning("social_analytics: POC preflight read failed: %s", e)
        return None


def preflight_poc_structure(values: Optional[list] = None) -> dict:
    """Read the two-row POC header and locate the safe insertion point.

    Returns Status column, the first taxonomy category column (HOOK), the
    metadata columns, and whether Status sits immediately before HOOK (the safe
    insertion boundary). Read-only.
    """
    if values is None:
        values = _poc_values()
    if not values or len(values) < 2:
        return {"ok": False, "error": "POC header unreadable"}
    row1, row2 = values[0], values[1]
    status_col = next((i + 1 for i, c in enumerate(row2) if str(c).strip() == "Status"), None)
    hook_col = next((i + 1 for i, c in enumerate(row1) if str(c).strip()), None)
    metadata = [str(c).strip() for i, c in enumerate(row2)
                if str(c).strip() and (hook_col is None or i < hook_col - 1)]
    # The load-bearing invariant is NOT "Status is adjacent to HOOK" (metadata
    # columns accumulate over time), it is: every column before the first
    # taxonomy category has a BLANK row-1 category, i.e. the metadata block is
    # contiguous and the insertion point sits at its end, immediately before
    # HOOK. Inserting there can never land inside a taxonomy group.
    pure_metadata_region = bool(hook_col) and all(
        not str(row1[i]).strip() for i in range(0, hook_col - 1) if i < len(row1))
    safe = bool(status_col and hook_col and status_col < hook_col and pure_metadata_region)
    return {
        "ok": True,
        "status_col": status_col,
        "hook_col": hook_col,
        "first_category": row1[hook_col - 1].strip() if hook_col else None,
        "metadata_columns": metadata,
        "total_columns": len(row2),
        "insert_after_col": status_col,
        "insert_before_col": hook_col,
        "status_immediately_before_hook": bool(status_col and hook_col
                                               and status_col == hook_col - 1),
        "metadata_block_contiguous": pure_metadata_region,
        "safe": safe,
    }


def _col_letter(n: int) -> str:
    """1-based column index -> A1 letter(s)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def insertion_plan(include_optional: bool = True, values: Optional[list] = None,
                   columns: Optional[list] = None) -> dict:
    """The exact plan for adding the metric columns between Status and HOOK.

    Read-only (computes the plan; performs no write)."""
    pf = preflight_poc_structure(values)
    cols = list(columns) if columns else (
        list(_REQUIRED_METRIC_COLUMNS) + (list(_OPTIONAL_COLUMNS) if include_optional else []))
    plan = {"ok": pf["ok"], "preflight": pf, "columns": cols, "count": len(cols),
            "row1_value": "(blank)", "include_optional": include_optional}
    if pf["ok"]:
        start = pf["insert_before_col"]                 # insert before HOOK
        plan["insert_at_col"] = start
        plan["insert_at_a1"] = f"{_col_letter(start)} (before {_col_letter(pf['hook_col'])}=HOOK)"
        plan["new_hook_col_after"] = pf["hook_col"] + len(cols)
        plan["positions"] = [{"col": start + i, "a1_row2": f"{_col_letter(start + i)}2",
                              "name": name} for i, name in enumerate(cols)]
    return plan


def render_insertion_plan(plan: dict) -> str:
    pf = plan.get("preflight", {})
    if not plan.get("ok"):
        return "Could not read the POC header to build an insertion plan."
    lines = [
        "SOCIAL METRICS — POC insertion plan (NOTHING WRITTEN)",
        f"  Worksheet metadata columns (row 2): {', '.join(pf['metadata_columns'])}",
        f"  Status column: {_col_letter(pf['status_col'])} (col {pf['status_col']})",
        f"  First taxonomy category HOOK: {_col_letter(pf['hook_col'])} (col {pf['hook_col']})",
        f"  Metadata block contiguous (safe to insert before HOOK): "
        f"{'YES' if pf.get('metadata_block_contiguous') else 'NO — review'}",
        "",
        f"  INSERT {plan['count']} new columns starting at {plan['insert_at_a1']},",
        "  pushing the taxonomy block right. For every new column: row 1 (category) = BLANK,",
        "  row 2 = the column name. Do NOT append to the far right.",
        "",
        f"  Required ({len(_REQUIRED_METRIC_COLUMNS)}): {', '.join(_REQUIRED_METRIC_COLUMNS)}",
    ]
    if plan["include_optional"]:
        lines.append(f"  Optional ({len(_OPTIONAL_COLUMNS)}): {', '.join(_OPTIONAL_COLUMNS)}")
    lines += ["",
              "  Column placement:"]
    for p in plan["positions"]:
        lines.append(f"    {p['a1_row2']:>5}  {p['name']}   (row 1 blank)")
    lines += ["",
              f"  After insertion, HOOK moves to column {plan['new_hook_col_after']} "
              f"({_col_letter(plan['new_hook_col_after'])}).",
              "  Taxonomy columns are matched by (category, option) each run, so shifting is safe.",
              "  No write performed — approve to execute."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# POC column auto-inserter — dry-run by default; the real write is gated behind
# apply=True AND passes safety + idempotency guards. Inserts BEFORE the first
# taxonomy category so the two-row header stays intact and taxonomy shifts right
# (values preserved; taxonomy is matched by (category, option) each run).
# ---------------------------------------------------------------------------
def insert_poc_metric_columns(include_optional: bool = True, apply: bool = False,
                              columns: Optional[list] = None) -> dict:
    """Plan (and, only when apply=True + guards pass, perform) the insertion of
    the metric columns between Status and HOOK.

    Guards that block a write: header unreadable, Status NOT immediately before
    HOOK (unsafe boundary), or ANY target column already present (idempotent —
    never double-inserts). New columns get a blank row-1 category and the name in
    row 2; data cells are left empty (no fabricated values). Existing analyzed
    data is shifted right, never overwritten."""
    values = _poc_values()
    plan = insertion_plan(include_optional, values, columns)
    if not plan["ok"]:
        return {"ok": False, "error": "POC header unreadable", "wrote": False}
    # Idempotency FIRST: if the columns are already there, a re-run is a no-op —
    # and after a real insert Status is no longer adjacent to HOOK, so this check
    # must precede the boundary guard to give a clear message instead of "unsafe".
    row2 = [str(c).strip() for c in values[1]]
    already = [c for c in plan["columns"] if c in row2]
    if already:
        return {"ok": True, "wrote": False, "plan": plan, "already_present": already,
                "note": "some/all target columns already exist — nothing to insert (idempotent)"}
    pf = plan["preflight"]
    if not pf["safe"]:
        return {"ok": False, "wrote": False, "plan": plan,
                "error": ("unsafe insertion boundary: the columns before the first taxonomy "
                          "category are not a contiguous blank-category metadata block")}
    if not apply:
        return {"ok": True, "wrote": False, "dry_run": True, "plan": plan}
    # ---- APPLY (gated) ----------------------------------------------------
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        ws = sh.worksheet(config.GOOGLE_WORKSHEET_NAME)
        # each inserted column: [row1 blank category, row2 name]; data cells empty
        new_cols = [["", name] for name in plan["columns"]]
        ws.insert_cols(new_cols, col=plan["insert_at_col"], value_input_option="RAW")
        return {"ok": True, "wrote": True, "inserted": len(plan["columns"]), "plan": plan}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "wrote": False, "plan": plan,
                "error": f"insert failed: {type(e).__name__}: {e}"}


def render_insert_result(r: dict) -> str:
    if not r.get("ok"):
        base = f"insert-social-schema: {r.get('error')}"
        return base + ("\n\n" + render_insertion_plan(r["plan"]) if r.get("plan") else "")
    if r.get("already_present"):
        return ("insert-social-schema: columns already present, nothing inserted "
                f"({', '.join(r['already_present'])}). Idempotent — safe to re-run.")
    if r.get("wrote"):
        return (f"insert-social-schema: INSERTED {r['inserted']} columns between Status and HOOK "
                "(row 1 blank, row 2 = names). Taxonomy shifted right; data preserved.")
    return (render_insertion_plan(r["plan"])
            + "\n\n  DRY-RUN — no write performed. Re-run with --apply to execute.")


# ---------------------------------------------------------------------------
# Task 3 — staging tab (create is a WRITE; status check is read-only)
# ---------------------------------------------------------------------------
def staging_tab_status() -> dict:
    """Read-only: does SOCIAL_METRICS_IMPORT_STAGING exist and have the header?"""
    vals = _read_named_worksheet(STAGING_TAB)
    if vals is None:
        return {"exists": False, "reason": "not found / unreachable"}
    header = [str(c).strip() for c in (vals[0] if vals else []) if str(c).strip()]
    return {"exists": True, "header": header,
            "header_ok": header == list(STAGING_COLUMNS),
            "row_count": max(0, len(vals) - 1)}


def ensure_staging_tab() -> dict:
    """WRITE: create SOCIAL_METRICS_IMPORT_STAGING with the header row if absent.
    Non-destructive (new tab only; never touches POC/taxonomy). Only ever called
    from the explicit `setup-metrics-staging` CLI after operator approval."""
    status = staging_tab_status()
    if status.get("exists"):
        return {"created": False, "reason": "already exists", "status": status}
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        ws = sh.add_worksheet(title=STAGING_TAB, rows=500, cols=len(STAGING_COLUMNS))
        ws.update(range_name="A1", values=[list(STAGING_COLUMNS)])
        return {"created": True, "columns": list(STAGING_COLUMNS)}
    except Exception as e:  # noqa: BLE001
        return {"created": False, "reason": f"error: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Task 4 — validation helpers
# ---------------------------------------------------------------------------
def validate_import_value(column: str, value) -> tuple:
    """(ok, parsed_or_error) for a staging cell. Empty is OK (skipped). Numeric
    columns must parse as numbers; split columns must parse as 'L pct%'; text is
    accepted as-is. Never coerces a bad value into a fake one."""
    v = str(value or "").strip()
    if not v:
        return True, ""                                  # empty -> nothing to import
    if column in _NUMERIC_METRIC_COLUMNS:
        n = _num(v)
        return (n is not None, n if n is not None else f"not numeric: {v!r}")
    if column in _SPLIT_METRIC_COLUMNS:
        d = _parse_split(v)
        return (bool(d), d if d else f"unparseable split: {v!r}")
    return True, v


# ---------------------------------------------------------------------------
# Task 5 — dry-run import (reads staging, matches to POC, writes nothing)
# ---------------------------------------------------------------------------
def import_social_metrics_dry_run() -> dict:
    """Match SOCIAL_METRICS_IMPORT_STAGING rows to POC by LINK and report what a
    real import WOULD do. Writes nothing; there is no non-dry-run mode."""
    staging = _read_named_worksheet(STAGING_TAB)
    if staging is None:
        return {"ok": False, "error": f"{STAGING_TAB} tab not found / unreachable"}
    if len(staging) < 2:
        return {"ok": True, "empty": True, "matched": 0, "unmatched": [], "no_link": 0,
                "would_fill": {}, "already_populated": {}, "parse_errors": []}
    header = [str(c).strip() for c in staging[0]]
    idx = {name: i for i, name in enumerate(header)}
    link_i = idx.get("LINK")
    poc_rows, poc_cols, err = _internal_sheet()
    if err:
        return {"ok": False, "error": f"POC unreachable: {err}"}
    poc_by_link = {_norm_link(r.get("LINK", "")): r for r in poc_rows
                   if str(r.get("LINK", "")).strip()}
    metric_cols = [c for c in header
                   if c in _REQUIRED_METRIC_COLUMNS or c in _OPTIONAL_COLUMNS]

    matched, no_link = 0, 0
    unmatched, parse_errors = [], []
    would_fill: dict[str, int] = {}
    already: dict[str, int] = {}
    for raw in staging[1:]:
        if not any(str(c).strip() for c in raw):
            continue
        link = raw[link_i].strip() if link_i is not None and link_i < len(raw) else ""
        if not link:
            no_link += 1
            continue
        poc = poc_by_link.get(_norm_link(link))
        if not poc:
            unmatched.append(link)
            continue
        matched += 1
        for col in metric_cols:
            i = idx[col]
            val = raw[i].strip() if i < len(raw) else ""
            if not val:
                continue
            ok, parsed = validate_import_value(col, val)
            if not ok:
                parse_errors.append({"link": link, "column": col, "value": val})
                continue
            # would we fill or is the POC cell already populated (never overwrite)?
            if col in poc_cols and str(poc.get(col, "")).strip():
                already[col] = already.get(col, 0) + 1
            else:
                would_fill[col] = would_fill.get(col, 0) + 1
    return {"ok": True, "empty": False, "matched": matched, "unmatched": unmatched,
            "no_link": no_link, "would_fill": would_fill, "already_populated": already,
            "parse_errors": parse_errors,
            "poc_missing_columns": [c for c in metric_cols if c not in poc_cols]}


def render_import_dry_run(rep: dict) -> str:
    if not rep.get("ok"):
        return f"import-social-metrics --dry-run: {rep.get('error')}. Nothing written."
    if rep.get("empty"):
        return (f"import-social-metrics --dry-run: {STAGING_TAB} has only a header (no rows to "
                "import). Nothing written.")
    lines = ["import-social-metrics --dry-run (NO WRITES)",
             f"  Matched to POC by LINK: {rep['matched']}",
             f"  Unmatched links (skipped): {len(rep['unmatched'])}"]
    for u in rep["unmatched"][:10]:
        lines.append(f"    - {u}")
    if len(rep["unmatched"]) > 10:
        lines.append(f"    … and {len(rep['unmatched']) - 10} more")
    if rep["no_link"]:
        lines.append(f"  Rows with no LINK (skipped): {rep['no_link']}")
    lines.append("  Fields that WOULD be filled (matched row, POC cell empty):")
    for col, n in sorted(rep["would_fill"].items()):
        note = "  [POC column not added yet]" if col in rep.get("poc_missing_columns", []) else ""
        lines.append(f"    - {col}: {n}{note}")
    if not rep["would_fill"]:
        lines.append("    (none)")
    if rep["already_populated"]:
        lines.append("  Already-populated POC cells (would NOT overwrite):")
        for col, n in sorted(rep["already_populated"].items()):
            lines.append(f"    - {col}: {n}")
    if rep["parse_errors"]:
        lines.append(f"  Parse errors ({len(rep['parse_errors'])}):")
        for e in rep["parse_errors"][:10]:
            lines.append(f"    - {e['link']} [{e['column']}]: {e['value']}")
    lines.append("")
    lines.append("Dry-run only: nothing was written to the Sheet, Notion, or any row. "
                 "(No non-dry-run mode exists.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Task 6 — duration-bucket audit (read-only, from Content audit + PERFORMANCE)
# ---------------------------------------------------------------------------
def audit_duration_buckets(rows: Optional[list[dict]] = None,
                           columns: Optional[list[str]] = None,
                           audit_buckets: Optional[dict] = None) -> dict:
    """Read-only: join the Content audit coarse video-length buckets to POC rows
    and report the bucket distribution across Great/Good/Ok/Underdog and the
    best-performing bucket by Great-rate. Caveat: bucketed, not exact seconds."""
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
    else:
        err = ""
    if err:
        return {"ok": False, "error": err}
    by_link = {str(r.get("LINK", "")).strip(): r for r in rows if str(r.get("LINK", "")).strip()}
    if audit_buckets is None:
        audit_buckets = content_audit_duration_buckets(set(by_link))
    dist: dict[str, dict] = {}
    matched = 0
    for link, bucket in (audit_buckets or {}).items():
        r = by_link.get(link)
        if not r:
            continue
        matched += 1
        label = str(r.get("PERFORMANCE", "")).strip().title()
        d = dist.setdefault(bucket, {"Great": 0, "Good": 0, "Ok": 0, "Underdog": 0, "total": 0})
        d["total"] += 1
        if label in d:
            d[label] += 1
    reliable = [(b, d) for b, d in dist.items() if d["total"] >= 2]
    best = max(reliable, key=lambda kv: kv[1]["Great"] / kv[1]["total"]) if reliable else None
    return {"ok": True, "rows_with_bucket": matched, "total_rows": len(by_link),
            "distribution": dist,
            "best_bucket": best[0] if best else None,
            "best_great_rate": round(100 * best[1]["Great"] / best[1]["total"]) if best else None,
            "thin": not reliable}


def render_duration_bucket_audit(a: dict) -> str:
    if not a.get("ok"):
        return f"audit-duration-buckets: {a.get('error')}. Nothing written."
    lines = ["Duration bucket audit (read-only — from Content audit + PERFORMANCE label)",
             f"  POC rows with a Content audit duration bucket: {a['rows_with_bucket']}/{a['total_rows']}"]
    if not a["distribution"]:
        lines.append("  No Content audit duration buckets matched POC reels (tab empty/unreachable).")
        return "\n".join(lines)
    lines.append("  Distribution (Great / Good / Ok / Underdog / total):")
    for b, d in sorted(a["distribution"].items(), key=lambda kv: -kv[1]["total"]):
        lines.append(f"    {b:12} {d['Great']} / {d['Good']} / {d['Ok']} / {d['Underdog']} / {d['total']}")
    if a["best_bucket"]:
        lines.append(f"  Highest-performing bucket (by Great-rate, n>=2): {a['best_bucket']} "
                     f"({a['best_great_rate']}% Great)")
    else:
        lines.append("  Best bucket: too little data (no bucket has >=2 reels).")
    lines.append("  Caveat: these are coarse buckets, NOT exact seconds. Add DURATION_SECONDS "
                 "for exact analysis.")
    return "\n".join(lines)


# ===========================================================================
# EXPLICIT ANALYTICS QUERIES (analytics_query contract)
# ---------------------------------------------------------------------------
# The contract-driven half of this module: `analytics_query.parse()` decides WHAT
# was asked, the functions below decide whether we can answer it and compute the
# real numbers, and the renderers turn that into ordinary strategist prose.
#
# Availability ladder (§10) — these four are NOT the same thing, and collapsing
# them is how a brain ends up inventing a metric:
#   COLUMN_EXISTS    the field is modelled in the sheet
#   DATA_EXISTS      at least one row actually carries a value
#   ENOUGH_DATA      enough values to read a pattern from
#   COMPARABLE_DATA  enough values in EACH cohort being compared
# ===========================================================================
# Last source-relevance decision on the analytics path (route_debug only).
LAST_SOURCE_AUDIT: dict = {}

COLUMN_MISSING = "COLUMN_MISSING"
COLUMN_EXISTS = "COLUMN_EXISTS"
DATA_EXISTS = "DATA_EXISTS"
ENOUGH_DATA = "ENOUGH_DATA"
COMPARABLE_DATA = "COMPARABLE_DATA"

# Minimum values before we read anything into a distribution, and minimum per
# side before we compare two cohorts. Deliberately modest: this brain's whole
# point is being honest about small samples, not refusing to look at them.
_MIN_FOR_PATTERN = 5
_MIN_PER_COHORT = 3
# Posting-time windows need more than a couple of posts each or "best time" is
# noise dressed up as a finding.
_MIN_PER_TIME_WINDOW = 3

# analytics_query metric -> the logical field name used by _FIELD_ALIASES.
_METRIC_TO_FIELD = {
    "DURATION_SECONDS": "duration", "VIEWS": "views", "LIKES": "likes",
    "COMMENTS": "comments", "SHARES": "shares", "ENGAGEMENT_RATE": "engagement_rate",
    "REEL_TYPE": "reel_type", "AGE_SPLIT": "demo_age", "GENDER_SPLIT": "demo_gender",
    "PERFORMANCE": "performance_label",
}

# Full-resolution publication timestamp. POST_DATE is date-only by construction,
# so hour-of-day analysis needs its own column; when it is absent we can still do
# day-of-week from POST_DATE alone and say so.
_TIMESTAMP_ALIASES = ("post_timestamp", "post timestamp", "published_at",
                      "published at", "posted_at", "posted at", "timestamp")
_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
              "Saturday", "Sunday")


def _internal_rows_only(rows: list[dict]) -> list[dict]:
    """Internal Storelli rows with a LINK. External inspiration is never proof."""
    return [r for r in (rows or [])
            if not performance.is_reference_row(r) and str(r.get("LINK", "")).strip()]


def availability(metric: str, rows: list[dict], columns: list[str],
                 min_n: int = _MIN_FOR_PATTERN) -> dict:
    """Walk the availability ladder for one metric. Never computes the metric —
    only reports how far up the ladder we actually are."""
    field = _METRIC_TO_FIELD.get(metric)
    col = _column_for_field(columns, field) if field else None
    internal = _internal_rows_only(rows)
    if metric == "PERFORMANCE":
        # The performance label is modelled by definition; what varies is how
        # many rows carry a recognized, mature value.
        buckets = performance.buckets_for_rows(internal)
        n = len(buckets)
        state = (ENOUGH_DATA if n >= min_n else DATA_EXISTS if n else COLUMN_EXISTS)
        return {"metric": metric, "column": "PERFORMANCE", "state": state,
                "n_with_value": n, "n_rows": len(internal), "min_n": min_n}
    if not col:
        return {"metric": metric, "column": None, "state": COLUMN_MISSING,
                "n_with_value": 0, "n_rows": len(internal), "min_n": min_n}
    n = sum(1 for r in internal if str(r.get(col, "")).strip() != "")
    state = (ENOUGH_DATA if n >= min_n else DATA_EXISTS if n else COLUMN_EXISTS)
    return {"metric": metric, "column": col, "state": state, "n_with_value": n,
            "n_rows": len(internal), "min_n": min_n}


def _apply_filters(rows: list[dict], filters: dict) -> list[dict]:
    """Scope rows to the contract's product/ICP filters (substring, case-folded,
    so hand-typed 'BodyShield GK Leggings' matches a 'bodyshield' filter)."""
    out = list(rows)
    for key, col in (("product", "Product"), ("icp", "ICP")):
        wanted = [str(v).strip().lower() for v in (filters.get(key) or []) if str(v).strip()]
        if not wanted:
            continue
        out = [r for r in out
               if any(w in _lower(r.get(col, "")) or _lower(r.get(col, "")) in w
                      for w in wanted)]
    return out


def cohort_rows(rows: list[dict], aq: dict) -> dict:
    """Resolve the contract's cohort into concrete rows.

    §4 — one definition of "highest performing", stated in the answer: reels
    currently classified Great by the established methodology
    (`performance.buckets_for_rows`, which already drops external rows and
    immature AUTO labels). A user-named yardstick ("highest views") wins instead.
    """
    internal = _internal_rows_only(rows)
    scoped = _apply_filters(internal, aq.get("filters") or {})
    cohort = aq.get("cohort") or {}
    basis = cohort.get("basis", "all")
    buckets = performance.buckets_for_rows(scoped)

    if basis == "performance_label":
        want = cohort.get("performance", "Great")
        target = performance.bucket_from_performance(want) or want
        selected = [r for r in scoped if buckets.get(r["_row"]) == target]
        rest = [r for r in scoped if r["_row"] in buckets and buckets.get(r["_row"]) != target]
        return {"rows": selected, "rest": rest, "scoped": scoped,
                "definition": cohort.get("label", f"reels classified {want}"),
                "basis": basis, "labelled": len(buckets)}
    if basis in ("metric", "normalized"):
        metric_field = _METRIC_TO_FIELD.get(cohort.get("metric", "VIEWS"), "views")
        ranked = []
        for r in scoped:
            val = _num(_row_get_ci(r, *_FIELD_ALIASES.get(metric_field, (metric_field,))))
            if val is None:
                continue
            if basis == "normalized":
                followers = _num(_row_get_ci(r, *_FIELD_ALIASES["followers"])) \
                    or float(config.STORELLI_IG_FOLLOWER_COUNT or 0) or None
                val = (val / followers) if followers else None
                if val is None:
                    continue
            ranked.append((val, r))
        ranked.sort(key=lambda p: p[0], reverse=True)
        top = [r for _v, r in ranked[: max(1, int(aq.get("top_n") or 5))]]
        return {"rows": top, "rest": [r for _v, r in ranked[len(top):]], "scoped": scoped,
                "definition": cohort.get("label", "ranked by the metric you named"),
                "basis": basis, "labelled": len(ranked), "ranked": ranked}
    return {"rows": scoped, "rest": [], "scoped": scoped,
            "definition": cohort.get("label", "all analyzed internal reels"),
            "basis": basis, "labelled": len(buckets)}


# ---------------------------------------------------------------------------
# descriptive statistics (pure)
# ---------------------------------------------------------------------------
def _stats(values: list) -> dict:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {}
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"n": n, "median": round(median, 1), "mean": round(sum(vals) / n, 1),
            "min": round(vals[0], 1), "max": round(vals[-1], 1), "values": vals}


def _bucket_counts(values: list) -> dict:
    counts: dict[str, int] = {}
    for v in values:
        b = _duration_bucket(v)
        counts[b] = counts.get(b, 0) + 1
    return counts


def _pct_under(values: list, threshold: float) -> Optional[int]:
    if not values:
        return None
    return round(100 * sum(1 for v in values if v < threshold) / len(values))


def _threshold_in_question(text: str) -> Optional[float]:
    """"under 10 seconds" / "below 15s" -> 10.0 / 15.0."""
    m = re.search(r"\b(?:under|below|less than|shorter than|<)\s*(\d{1,3})\b", _lower(text))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# DURATION — first-class metric (§5/§6)
# ---------------------------------------------------------------------------
def duration_profile(aq: dict, rows: Optional[list[dict]] = None,
                     columns: Optional[list[str]] = None,
                     audit_buckets: Optional[dict] = None) -> dict:
    """Real duration analytics for the contract's cohort.

    Source hierarchy (§6), never blended and never mislabelled:
      1. exact DURATION_SECONDS               -> source='exact'
      2. Content audit coarse duration bucket -> source='bucket' (approximate)
      3. nothing                              -> source='none'
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    avail = availability("DURATION_SECONDS", rows, columns)
    coh = cohort_rows(rows, aq)
    duration_col = avail.get("column")

    exact, rest_exact = [], []
    if duration_col:
        for r in coh["rows"]:
            s = _parse_seconds(r.get(duration_col))
            if s is not None:
                exact.append(s)
        for r in coh["rest"]:
            s = _parse_seconds(r.get(duration_col))
            if s is not None:
                rest_exact.append(s)

    out = {"ok": True, "error": "", "availability": avail, "cohort": coh["definition"],
           "cohort_size": len(coh["rows"]), "scoped": len(coh["scoped"]),
           "threshold": _threshold_in_question(aq.get("raw", "")),
           "basis": coh["basis"]}

    if exact:
        st_ = _stats(exact)
        out.update({
            "source": "exact", "stats": st_, "coverage": len(exact),
            "coverage_pct": (round(100 * len(exact) / len(coh["rows"]))
                             if coh["rows"] else 0),
            "buckets": _bucket_counts(exact),
            "comparison": _stats(rest_exact) if len(rest_exact) >= _MIN_PER_COHORT else {},
            "comparable": (COMPARABLE_DATA if len(exact) >= _MIN_PER_COHORT
                           and len(rest_exact) >= _MIN_PER_COHORT else avail["state"]),
        })
        if out["threshold"] is not None:
            out["pct_under"] = _pct_under(exact, out["threshold"])
            if rest_exact:
                out["rest_pct_under"] = _pct_under(rest_exact, out["threshold"])
        return out

    # ---- coarse bucket fallback: approximate, and always SAID to be ----------
    links = {str(r.get("LINK", "")).strip() for r in coh["rows"] if str(r.get("LINK", "")).strip()}
    if audit_buckets is None:
        audit_buckets = content_audit_duration_buckets(links)
    matched = {lk: b for lk, b in (audit_buckets or {}).items() if lk in links}
    if matched:
        dist: dict[str, int] = {}
        for b in matched.values():
            dist[b] = dist.get(b, 0) + 1
        out.update({"source": "bucket", "buckets": dist,
                    "dominant_bucket": max(dist.items(), key=lambda kv: kv[1])[0],
                    "coverage": len(matched),
                    "coverage_pct": round(100 * len(matched) / len(links)) if links else 0,
                    "stats": {}, "comparison": {}})
        return out

    out.update({"source": "none", "stats": {}, "buckets": {}, "coverage": 0,
                "comparison": {}, "backfill_field": "DURATION_SECONDS",
                "recommended_source": "Apify videoDuration / yt-dlp info['duration']"})
    return out


# ---------------------------------------------------------------------------
# TIME — publication timestamp, day of week, hour of day (§7/§8)
# ---------------------------------------------------------------------------
def _parse_timestamp(raw: str):
    """Parse a publication timestamp/date cell -> (datetime|None, has_time)."""
    from datetime import datetime, timezone as _tz
    s = str(raw or "").strip()
    if not s:
        return None, False
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        has_time = bool(re.search(r"\d{1,2}:\d{2}", s))
        return (dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)), has_time
    except ValueError:
        pass
    for fmt, has_time in (("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%dT%H:%M:%S", True),
                          ("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False),
                          ("%d/%m/%Y", False), ("%m/%d/%Y", False)):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=_tz.utc), has_time
        except ValueError:
            continue
    return None, False


def temporal_fields(rows: Optional[list[dict]] = None,
                    columns: Optional[list[str]] = None) -> dict:
    """Audit which temporal dimensions the dataset actually supports (§7).

    Nothing here is derived from a field the source didn't provide: hour-of-day
    exists only when a real timestamp with a time component exists.
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    internal = _internal_rows_only(rows)
    ts_col = None
    have = {str(c).strip().lower(): str(c).strip() for c in (columns or [])}
    for alias in _TIMESTAMP_ALIASES:
        if alias in have:
            ts_col = have[alias]
            break
    date_col = _column_for_field(columns, "date")

    with_date, with_time = 0, 0
    for r in internal:
        dt, has_time = (None, False)
        if ts_col:
            dt, has_time = _parse_timestamp(r.get(ts_col))
        if dt is None and date_col:
            dt, has_time = _parse_timestamp(r.get(date_col))
        if dt is not None:
            with_date += 1
            if has_time:
                with_time += 1
    _tz, tz_label, tz_is_utc = config.posting_timezone()
    return {"ok": True, "error": "", "rows": len(internal),
            "timestamp_column": ts_col, "date_column": date_col,
            "with_date": with_date, "with_time": with_time,
            "day_of_week_derivable": with_date > 0,
            "hour_derivable": with_time > 0,
            "timezone": tz_label, "timezone_is_utc_default": tz_is_utc,
            "post_age_derivable": with_date > 0,
            "maturity_days": config.PERFORMANCE_MATURITY_DAYS}


def posting_time_profile(aq: dict, rows: Optional[list[dict]] = None,
                         columns: Optional[list[str]] = None) -> dict:
    """Day-of-week / hour-of-day performance, or an honest "not enough per
    window" (§8). A best posting time is only ever claimed when each window it
    compares actually carries enough mature posts."""
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    tf = temporal_fields(rows, columns)
    internal = _internal_rows_only(rows)
    scoped = _apply_filters(internal, aq.get("filters") or {})
    buckets = performance.buckets_for_rows(scoped)
    wants_hour = aq.get("metric") == "POST_HOUR"

    ts_col, date_col = tf.get("timestamp_column"), tf.get("date_column")
    tzinfo, tz_label, _utc_default = config.posting_timezone()
    by_day: dict[str, dict] = {}
    by_hour: dict[int, dict] = {}
    n_placed = 0
    for r in scoped:
        dt, has_time = (None, False)
        if ts_col:
            dt, has_time = _parse_timestamp(r.get(ts_col))
        if dt is None and date_col:
            dt, has_time = _parse_timestamp(r.get(date_col))
        if dt is None:
            continue
        local = dt.astimezone(tzinfo)
        bucket = buckets.get(r["_row"])
        n_placed += 1
        day = _DAY_NAMES[local.weekday()]
        slot = by_day.setdefault(day, {"total": 0, "great": 0, "labelled": 0})
        slot["total"] += 1
        if bucket:
            slot["labelled"] += 1
            if performance.is_positive(bucket):
                slot["great"] += 1
        if has_time:
            hslot = by_hour.setdefault(local.hour, {"total": 0, "great": 0, "labelled": 0})
            hslot["total"] += 1
            if bucket:
                hslot["labelled"] += 1
                if performance.is_positive(bucket):
                    hslot["great"] += 1

    dim = by_hour if wants_hour else by_day
    windows_with_enough = [k for k, v in dim.items() if v["labelled"] >= _MIN_PER_TIME_WINDOW]
    sufficient = len(windows_with_enough) >= 2
    best = ""
    if sufficient:
        ranked = sorted(((k, v) for k, v in dim.items()
                         if v["labelled"] >= _MIN_PER_TIME_WINDOW),
                        key=lambda kv: (kv[1]["great"] / kv[1]["labelled"], kv[1]["labelled"]),
                        reverse=True)
        best = str(ranked[0][0]) if ranked else ""
    return {"ok": True, "error": "", "temporal": tf, "dimension": "hour" if wants_hour else "day",
            "by_day": by_day, "by_hour": by_hour, "placed": n_placed,
            "scoped": len(scoped), "labelled": len(buckets),
            "windows_with_enough": sorted(str(w) for w in windows_with_enough),
            "min_per_window": _MIN_PER_TIME_WINDOW, "sufficient": sufficient,
            "best_window": best, "timezone": tz_label,
            "hour_derivable": tf.get("hour_derivable", False),
            "day_derivable": tf.get("day_of_week_derivable", False)}


def post_age_profile(aq: dict, rows: Optional[list[dict]] = None,
                     columns: Optional[list[str]] = None) -> dict:
    """"How old is the latest reel?" — lifecycle temporal dimension."""
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    internal = _apply_filters(_internal_rows_only(rows), aq.get("filters") or {})
    ages = []
    for r in internal:
        age = performance.post_age_days(r)
        if age is not None:
            ages.append((age, r))
    ages.sort(key=lambda p: p[0])
    if not ages:
        return {"ok": True, "error": "", "n": 0, "rows": len(internal),
                "newest_days": None, "oldest_days": None}
    newest_age, newest = ages[0]
    return {"ok": True, "error": "", "n": len(ages), "rows": len(internal),
            "newest_days": round(newest_age, 1),
            "newest_link": str(newest.get("LINK", "")).strip(),
            "newest_performance": str(newest.get("PERFORMANCE", "")).strip(),
            "newest_mature": performance.is_mature(newest),
            "oldest_days": round(ages[-1][0], 1),
            "maturity_days": config.PERFORMANCE_MATURITY_DAYS,
            "without_date": len(internal) - len(ages)}


def metric_profile(aq: dict, rows: Optional[list[dict]] = None,
                   columns: Optional[list[str]] = None) -> dict:
    """Descriptive / ranking analytics for a raw numeric metric (comments, views,
    likes, shares, engagement rate)."""
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    metric = aq["metric"]
    avail = availability(metric, rows, columns)
    coh = cohort_rows(rows, aq)
    col = avail.get("column")

    def _collect(rs):
        out = []
        for r in rs:
            v = _num(r.get(col)) if col else None
            if v is not None:
                out.append({"value": v, "link": str(r.get("LINK", "")).strip(),
                            "performance": str(r.get("PERFORMANCE", "")).strip(),
                            "product": str(r.get("Product", "")).strip(),
                            "_row": r.get("_row")})
        out.sort(key=lambda d: d["value"], reverse=True)
        return out

    # Descriptive statistics describe the WHOLE scoped population, never the
    # top-N slice: "the median is X across 5 reels" after ranking the top 5 would
    # be the median of the winners, quietly answering a different question.
    population = _collect(coh["scoped"])
    ranked = _collect(coh["rows"]) if coh["basis"] != "all" else population
    return {"ok": True, "error": "", "metric": metric, "availability": avail,
            "cohort": coh["definition"], "cohort_size": len(coh["rows"]),
            "stats": _stats([d["value"] for d in population]),
            "top": ranked[: max(1, int(aq.get("top_n") or 5))],
            "n_with_value": len(population)}


def row_count_profile(aq: dict, rows: Optional[list[dict]] = None,
                      columns: Optional[list[str]] = None) -> dict:
    """"How many BodyShield reels do we have?" — a question about our sample."""
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    internal = _internal_rows_only(rows)
    scoped = _apply_filters(internal, aq.get("filters") or {})
    buckets = performance.buckets_for_rows(scoped)
    great = sum(1 for b in buckets.values() if performance.is_positive(b))
    analyzed = sum(1 for r in scoped
                   if any(str(r.get(c, "")).strip() == "1" for c in taxonomy.all_signal_columns()))
    import analytics_query as AQ
    scope_bits = []
    for key in ("product", "icp"):
        for v in (aq.get("filters") or {}).get(key, []) or []:
            scope_bits.append(AQ.display(v))
    return {"ok": True, "error": "", "total": len(scoped), "library": len(internal),
            "analyzed": analyzed, "labelled": len(buckets), "great": great,
            "scope": ", ".join(scope_bits), "pending_maturity":
                len(performance.pending_maturity_rows(scoped))}


def performance_slice_profile(aq: dict, rows: Optional[list[dict]] = None,
                              columns: Optional[list[str]] = None) -> dict:
    """"What performs better: POV or tutorial?" — Great-rate per named taxonomy
    option, with the sample size behind each side."""
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    internal = _apply_filters(_internal_rows_only(rows), aq.get("filters") or {})
    buckets = performance.buckets_for_rows(internal)
    terms = (aq.get("filters") or {}).get("taxonomy_terms") or []
    sides = []
    for term in terms:
        col = None
        for layer, options in taxonomy.LAYERS.items():
            for opt in options:
                if taxonomy.slug(opt) == taxonomy.slug(term) or _lower(opt) == _lower(term):
                    col = taxonomy.column_for(layer, opt)
                    break
            if col:
                break
        if not col:
            continue
        tagged = [r for r in internal if str(r.get(col, "")).strip() == "1"
                  and r["_row"] in buckets]
        great = sum(1 for r in tagged if performance.is_positive(buckets[r["_row"]]))
        sides.append({"label": term, "column": col, "n": len(tagged), "great": great,
                      "great_rate": round(100 * great / len(tagged)) if tagged else None})
    sides.sort(key=lambda s: (s["great_rate"] if s["great_rate"] is not None else -1,
                              s["n"]), reverse=True)
    comparable = all(s["n"] >= _MIN_PER_COHORT for s in sides) and len(sides) >= 2
    return {"ok": True, "error": "", "sides": sides, "comparable": comparable,
            "labelled": len(buckets), "min_per_cohort": _MIN_PER_COHORT}


# ---------------------------------------------------------------------------
# renderers — backend structured, Slack conversational (§18)
# ---------------------------------------------------------------------------
# Every renderer builds a ClaimLedger as it writes, so each cited source is
# bound to the exact claim it supports and a missing-data answer renders no
# Sources block at all (§11/§13/§14).
_DIRECTIONAL = ("I'd treat that as directional rather than an optimal number — "
                "duration and performance move together here, not necessarily "
                "one because of the other.")


def _fmt_secs(v) -> str:
    """12.0 -> '12s'; 12.4 -> '12.4s'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{int(f)}s" if abs(f - int(f)) < 0.05 else f"{f:.1f}s"


def _source_line_for_row(row_link: str, label: str) -> str:
    return f"<{row_link}|{label}>" if row_link else label


def _cohort_sentence(aq: dict, definition: str) -> str:
    """State the definition once, plainly, when the user's phrasing was the
    ambiguous kind (§4) — never a silent switch between yardsticks."""
    if not (aq.get("cohort") or {}).get("stated"):
        return ""
    return f"I'm reading that as {definition}."


def _scope_suffix(aq: dict) -> str:
    import analytics_query as AQ
    bits = []
    for key in ("product", "icp"):
        for v in (aq.get("filters") or {}).get(key, []) or []:
            bits.append(AQ.display(v))
    if not bits:
        return ""
    where = " / ".join(bits)
    if aq.get("scope_source") == "inherited":
        return f" (staying inside {where}, as we were discussing)"
    return f" (within {where})"


def _render_duration_answer(aq: dict, prof: dict) -> tuple:
    """(text, ledger, source_pairs)."""
    import source_binding as SB
    ledger = SB.ClaimLedger()
    pairs: list = []
    if not prof.get("ok"):
        return (f"I can't reach the analyzed sheet right now, so I don't want to "
                f"guess at reel length. ({prof.get('error')})", ledger, pairs)

    scope = _scope_suffix(aq)
    definition = prof.get("cohort", "")
    lead = _cohort_sentence(aq, definition)

    # An empty cohort is a SCOPE answer, not a schema answer: saying "there's no
    # duration column" when the column is fine but the filter matched nothing
    # would send someone off to fix the wrong problem.
    if not prof.get("cohort_size"):
        ledger.add("no reels match that scope", [], SB.SCHEMA_EVIDENCE)
        return (f"I don't have any reels matching that{scope} — {prof.get('scoped', 0)} "
                f"in scope, none of them in the cohort you asked about — so there's "
                f"nothing to measure the length of yet.", ledger, pairs)

    # ---- exact seconds ----------------------------------------------------
    if prof.get("source") == "exact":
        s = prof["stats"]
        n, cov_pct = s["n"], prof.get("coverage_pct", 0)
        parts = []
        if lead:
            parts.append(lead)
        skew = ("skew short" if s["median"] <= 15 else
                "sit in the mid-length range" if s["median"] <= 30 else "run long")
        parts.append(f"They {skew}: the median is {_fmt_secs(s['median'])} across "
                     f"{n} reel{'s' if n != 1 else ''} with an exact duration"
                     f"{scope}, average {_fmt_secs(s['mean'])}, "
                     f"range {_fmt_secs(s['min'])}–{_fmt_secs(s['max'])}.")
        ledger.add(f"median duration is {s['median']}s across {n} reels",
                   [f"S{len(pairs) + 1}"], SB.AGGREGATE_EVIDENCE)
        pairs.append((f"S{len(pairs) + 1}",
                      f"{n} internal reels with exact DURATION_SECONDS "
                      f"({cov_pct}% of that cohort)"))

        buckets = prof.get("buckets") or {}
        if buckets:
            top_bucket, top_count = max(buckets.items(), key=lambda kv: kv[1])
            parts.append(f"The heaviest cluster is {top_bucket} "
                         f"({top_count} of {n}).")
            ledger.add(f"heaviest duration cluster is {top_bucket}",
                       [pairs[0][0]], SB.AGGREGATE_EVIDENCE)

        thr = prof.get("threshold")
        if thr is not None and prof.get("pct_under") is not None:
            under = prof["pct_under"]
            cnt = round(under * n / 100)
            line = (f"{cnt} of the {n} ({under}%) come in under "
                    f"{_fmt_secs(thr)}")
            if prof.get("rest_pct_under") is not None:
                line += f", against {prof['rest_pct_under']}% of the rest"
            parts.append(line + ".")
            ledger.add(f"{under}% are under {thr}s", [pairs[0][0]], SB.AGGREGATE_EVIDENCE)

        comp = prof.get("comparison") or {}
        if comp and thr is None:
            delta = s["median"] - comp["median"]
            direction = ("shorter" if delta < 0 else "longer" if delta > 0
                         else "the same length")
            if direction == "the same length":
                parts.append(f"That's about the same as the rest of the labelled "
                             f"library (median {_fmt_secs(comp['median'])}, "
                             f"n={comp['n']}).")
            else:
                parts.append(f"That's {_fmt_secs(abs(delta))} {direction} than the "
                             f"rest of the labelled library "
                             f"(median {_fmt_secs(comp['median'])}, n={comp['n']}).")
            ledger.add(f"cohort median vs rest ({comp['median']}s, n={comp['n']})",
                       [pairs[0][0]], SB.AGGREGATE_EVIDENCE)

        if cov_pct < 100:
            parts.append(f"Worth knowing: only {s['n']} of "
                         f"{prof.get('cohort_size', s['n'])} in that cohort carry an "
                         f"exact duration, so this reads the ones we can measure.")
            ledger.add("duration coverage is partial", [pairs[0][0]], SB.SCHEMA_EVIDENCE)
        parts.append(_DIRECTIONAL)
        return (" ".join(p for p in parts if p), ledger, pairs)

    # ---- coarse bucket proxy — approximate, and said to be ----------------
    if prof.get("source") == "bucket":
        dist = prof.get("buckets") or {}
        cov, cov_pct = prof.get("coverage", 0), prof.get("coverage_pct", 0)
        spread = ", ".join(f"{b}: {c}" for b, c in
                           sorted(dist.items(), key=lambda kv: -kv[1])[:4])
        pairs.append(("S1", f"Content audit coarse duration buckets for {cov} reels "
                            f"({cov_pct}% of that cohort)"))
        ledger.add(f"dominant duration bucket is {prof.get('dominant_bucket')}",
                   ["S1"], SB.AGGREGATE_EVIDENCE)
        ledger.add("only bucketed duration is available, not exact seconds",
                   ["S1"], SB.SCHEMA_EVIDENCE)
        text = (f"{lead + ' ' if lead else ''}I don't have exact seconds for those — "
                f"only the coarse Content-audit length buckets, for {cov} of them "
                f"({cov_pct}%), so this is approximate rather than a measured median. "
                f"On that basis they land mostly in {prof.get('dominant_bucket')} "
                f"({spread}). To get a real median we'd need DURATION_SECONDS "
                f"backfilled from the Apify/yt-dlp video metadata.")
        return (text, ledger, pairs)

    # ---- genuinely absent -------------------------------------------------
    avail = prof.get("availability") or {}
    reason = ("there's no duration column in the sheet at all"
              if avail.get("state") == COLUMN_MISSING
              else "the duration column exists but no reel carries a value yet")
    ledger.add("duration is not available", [], SB.SCHEMA_EVIDENCE)
    text = (f"{lead + ' ' if lead else ''}I can't tell you how long they are — "
            f"{reason}. Nothing in the current data lets me infer it, and I'm not "
            f"going to estimate seconds. Backfilling DURATION_SECONDS from the "
            f"Apify/yt-dlp video metadata would make this a real answer.")
    return (text, ledger, pairs)


def _render_posting_time_answer(aq: dict, prof: dict) -> tuple:
    import source_binding as SB
    ledger = SB.ClaimLedger()
    pairs: list = []
    if not prof.get("ok"):
        return (f"I can't reach the analyzed sheet right now, so I can't read "
                f"posting times. ({prof.get('error')})", ledger, pairs)

    tf = prof.get("temporal") or {}
    dim = prof.get("dimension")
    scope = _scope_suffix(aq)
    tz_note = ("" if not tf.get("timezone_is_utc_default")
               else " (timestamps are UTC — we've never recorded which local "
                    "timezone we publish on, so I won't pretend these are local hours)")

    if dim == "hour" and not prof.get("hour_derivable"):
        ledger.add("no publication time-of-day is stored", [], SB.SCHEMA_EVIDENCE)
        detail = (f"We have a post date for {tf.get('with_date', 0)} of "
                  f"{tf.get('rows', 0)} internal reels, but no time-of-day on any of "
                  f"them — POST_DATE is date-only, so the hour is simply not in the "
                  f"data.")
        return (f"I can't call a best time of day to post. {detail} Day of week I "
                f"could look at; the hour would need POST_TIMESTAMP kept at full "
                f"resolution from the Apify timestamp instead of being truncated "
                f"to a date.", ledger, pairs)

    if not prof.get("day_derivable") and dim == "day":
        ledger.add("no publication date is stored", [], SB.SCHEMA_EVIDENCE)
        return ("I can't tell you which day our strongest reels go out — none of "
                "the internal reels carry a post date, so there's nothing to group "
                "by. Backfilling POST_DATE would fix that.", ledger, pairs)

    label = "hour" if dim == "hour" else "day"
    if not prof.get("placed"):
        # Timestamps exist somewhere, but none survived this scope.
        ledger.add("no dated reels match that scope", [], SB.SCHEMA_EVIDENCE)
        return (f"I don't have any dated reels matching that{scope}, so there's no "
                f"publication {label} to group by here.", ledger, pairs)
    if not prof.get("sufficient"):
        counts = prof.get("by_hour" if dim == "hour" else "by_day") or {}
        windows = len(counts)
        ledger.add(f"posting-{label} windows are too thin to compare", [],
                   SB.SCHEMA_EVIDENCE)
        lead = ("No — not yet. " if aq.get("question_type") == "availability" else "")
        return (f"{lead}We do have the {label}s{scope} — {prof.get('placed', 0)} reels "
                f"spread across {windows} different {label}s{tz_note} — but not "
                f"enough performance-labelled posts in any single {label} to call a "
                f"best one. I'd need at least {prof.get('min_per_window')} labelled "
                f"reels in each {label} I'm comparing before that number means "
                f"anything, and right now "
                f"{len(prof.get('windows_with_enough') or [])} clear that bar. "
                f"That's a real answer, not a dodge: the timestamps exist, the "
                f"per-window sample doesn't.", ledger, pairs)

    counts = prof.get("by_hour" if dim == "hour" else "by_day") or {}
    best = prof.get("best_window")
    slot = counts.get(int(best) if dim == "hour" and str(best).isdigit() else best, {})
    rate = (round(100 * slot.get("great", 0) / slot["labelled"])
            if slot.get("labelled") else 0)
    best_label = f"{best}:00" if dim == "hour" else best
    pairs.append(("S1", f"{prof.get('labelled', 0)} performance-labelled internal "
                        f"reels grouped by publication {label}"))
    ledger.add(f"{best_label} has the highest Great rate ({rate}%)", ["S1"],
               SB.AGGREGATE_EVIDENCE)
    ranked_rest = [
        (k, v) for k, v in sorted(
            counts.items(),
            key=lambda kv: -((kv[1]["great"] / kv[1]["labelled"]) if kv[1]["labelled"] else 0))
        if v["labelled"] >= prof.get("min_per_window", 3)
        and str(k) != str(best)]          # never re-list the winner as "next best"
    others = ", ".join(
        f"{k}{'' if dim == 'day' else ':00'} "
        f"({round(100 * v['great'] / v['labelled'])}%, n={v['labelled']})"
        for k, v in ranked_rest[:2])
    next_bit = f" Next: {others}." if others else ""
    lead = ("Yes — just about. " if aq.get("question_type") == "availability" else "")
    return (f"{lead}On what we have{scope}, {best_label} comes out strongest — "
            f"{rate}% of the labelled reels posted then are Great "
            f"(n={slot.get('labelled', 0)}){tz_note}.{next_bit} "
            f"I'd hold this loosely: it's an association across a handful of "
            f"windows, not a scheduling law, and what we post almost certainly "
            f"matters more than when.", ledger, pairs)


def _render_metric_answer(aq: dict, prof: dict) -> tuple:
    import source_binding as SB
    import metric_registry as MR
    ledger = SB.ClaimLedger()
    pairs: list = []
    if not prof.get("ok"):
        return (f"I can't reach the analyzed sheet right now. ({prof.get('error')})",
                ledger, pairs)
    metric = prof["metric"]
    pretty = metric.lower().replace("_", " ")
    avail = prof["availability"]
    scope = _scope_suffix(aq)

    if avail["state"] in (COLUMN_MISSING, COLUMN_EXISTS) or not prof.get("n_with_value"):
        ledger.add(f"{metric} is not populated", [], SB.SCHEMA_EVIDENCE)
        gap = MR.metric_gap_note(metric) or ""
        why = ("that column isn't in the sheet" if avail["state"] == COLUMN_MISSING
               else f"the column exists but none of the {avail.get('n_rows', 0)} "
                    f"internal reels carry a value")
        return (f"I can't rank our reels by {pretty} — {why}, so any number I gave "
                f"you would be invented. {gap}".strip(), ledger, pairs)

    s = prof.get("stats") or {}
    top = prof.get("top") or []
    parts = []
    lead = _cohort_sentence(aq, prof.get("cohort", ""))
    if lead:
        parts.append(lead)
    pairs.append(("S1", f"{prof['n_with_value']} internal reels with a {metric} value"))
    if aq.get("question_type") == "ranking" and top:
        best = top[0]
        parts.append(f"Top by {pretty}{scope}: "
                     f"{int(best['value']) if float(best['value']).is_integer() else best['value']}"
                     f" on the leader, then "
                     + ", ".join(str(int(t['value'])) for t in top[1:4]) + ".")
        ledger.add(f"top {pretty} values across {prof['n_with_value']} reels",
                   ["S1"], SB.AGGREGATE_EVIDENCE)
        for i, t in enumerate(top[:3], start=2):
            sid = f"S{i}"
            pairs.append((sid, _source_line_for_row(
                t["link"], f"reel with {int(t['value'])} {pretty}"
                           f"{' — ' + t['performance'] if t['performance'] else ''}")))
            ledger.add(f"example reel at {int(t['value'])} {pretty}", [sid],
                       SB.EXAMPLE_CONTENT)
    if s:
        parts.append(f"Across {s['n']} reels with a value{scope}, the median is "
                     f"{s['median']:g} and the average {s['mean']:g} "
                     f"(range {s['min']:g}–{s['max']:g}).")
        ledger.add(f"median {pretty} is {s['median']:g} across {s['n']} reels",
                   ["S1"], SB.AGGREGATE_EVIDENCE)
    if avail["state"] == DATA_EXISTS:
        parts.append(f"Only {avail['n_with_value']} of {avail['n_rows']} reels carry "
                     f"{metric}, so treat this as a partial read.")
        ledger.add(f"{metric} coverage is partial", ["S1"], SB.SCHEMA_EVIDENCE)
    return (" ".join(parts), ledger, pairs)


def _render_count_answer(aq: dict, prof: dict) -> tuple:
    import source_binding as SB
    ledger = SB.ClaimLedger()
    pairs: list = []
    if not prof.get("ok"):
        return (f"I can't reach the analyzed sheet right now. ({prof.get('error')})",
                ledger, pairs)
    scope = prof.get("scope") or ""
    where = f" for {scope}" if scope else ""
    pairs.append(("S1", f"internal POC rows{where}: {prof['total']} of "
                        f"{prof['library']} in the library"))
    ledger.add(f"{prof['total']} internal reels{where}", ["S1"], SB.AGGREGATE_EVIDENCE)
    text = (f"We have {prof['total']} internal reel{'s' if prof['total'] != 1 else ''}"
            f"{where} in the library — {prof['analyzed']} tagged with the taxonomy, "
            f"{prof['labelled']} carrying a usable performance label, and "
            f"{prof['great']} of those classified Great.")
    if prof.get("pending_maturity"):
        text += (f" {prof['pending_maturity']} are still too young to label, so "
                 f"they're held out of the performance read.")
        ledger.add("some reels are pending maturity", ["S1"], SB.SCHEMA_EVIDENCE)
    if prof["total"] and prof["labelled"] < 3:
        text += " That's too thin to read a pattern from."
    return (text, ledger, pairs)


def _render_post_age_answer(aq: dict, prof: dict) -> tuple:
    import source_binding as SB
    ledger = SB.ClaimLedger()
    pairs: list = []
    if not prof.get("ok"):
        return (f"I can't reach the analyzed sheet right now. ({prof.get('error')})",
                ledger, pairs)
    if not prof.get("n"):
        ledger.add("no post dates are stored", [], SB.SCHEMA_EVIDENCE)
        return ("I can't tell you how recent our latest reel is — none of the "
                "internal rows carry a post date, so there's nothing to measure "
                "age from.", ledger, pairs)
    days = prof["newest_days"]
    pairs.append(("S1", _source_line_for_row(prof.get("newest_link", ""),
                                             "most recent internal reel")))
    ledger.add(f"newest reel is {days} days old", ["S1"], SB.AGGREGATE_EVIDENCE)
    mature = ("old enough to read a performance label from"
              if prof.get("newest_mature")
              else f"still inside the {prof['maturity_days']}-day maturity window, so "
                   f"its performance label is deliberately held back")
    extra = ""
    if prof.get("without_date"):
        extra = (f" {prof['without_date']} rows have no date at all, so this is the "
                 f"newest of the {prof['n']} we can date.")
        ledger.add("some rows carry no post date", ["S1"], SB.SCHEMA_EVIDENCE)
    return (f"The most recent reel I can date is {days:g} days old — {mature}."
            f"{extra}", ledger, pairs)


def _render_perf_slice_answer(aq: dict, prof: dict) -> tuple:
    import source_binding as SB
    ledger = SB.ClaimLedger()
    pairs: list = []
    if not prof.get("ok"):
        return (f"I can't reach the analyzed sheet right now. ({prof.get('error')})",
                ledger, pairs)
    sides = prof.get("sides") or []
    if len(sides) < 2:
        return ("", ledger, pairs)          # let the normal signal routes answer
    pairs.append(("S1", f"{prof['labelled']} performance-labelled internal reels, "
                        f"split by tag"))
    named = ", ".join(f"{s['label']} ({s['great_rate']}% Great of n={s['n']})"
                      for s in sides if s["great_rate"] is not None)
    ledger.add(f"Great-rate comparison across {len(sides)} tags", ["S1"],
               SB.AGGREGATE_EVIDENCE)
    if not prof.get("comparable"):
        thin = ", ".join(f"{s['label']} n={s['n']}" for s in sides)
        ledger.add("per-side sample is too thin to compare", ["S1"], SB.SCHEMA_EVIDENCE)
        return (f"Not enough on each side to call it: {thin}. I'd want at least "
                f"{prof['min_per_cohort']} labelled reels per side before comparing "
                f"Great rates, or the winner is just noise. On what's there: "
                f"{named}.", ledger, pairs)
    best, second = sides[0], sides[1]
    # Only recap the full list when there are more than the two named above,
    # otherwise the sentence just repeats itself.
    recap = f" Full split: {named}." if len(sides) > 2 else ""
    return (f"*{best['label'].title()}* is ahead: {best['great_rate']}% of its "
            f"labelled reels are Great (n={best['n']}), against "
            f"{second['great_rate']}% for {second['label'].title()} "
            f"(n={second['n']}).{recap} That's an association across a small "
            f"sample, so I'd treat it as the better bet rather than a settled "
            f"fact.", ledger, pairs)


def answer_analytics_query(aq: dict, text: str = "",
                           context: Optional[list] = None) -> Optional[str]:
    """Answer an EXPLICIT analytics question from the parsed contract.

    Returns None when the contract names something this layer doesn't compute, so
    the caller's existing routing handles the turn unchanged. Read-only.
    """
    import analytics_query as AQ
    import source_binding as SB
    if not aq:
        return None
    metric = aq.get("metric")
    try:
        # A metric only private Instagram Insights could provide is answered by
        # the registry/demographics path that already does it well.
        if aq.get("requires_private_data"):
            return answer_social_analytics_question(text or aq.get("raw", ""), context)
        if metric == AQ.M_REEL_TYPE:
            # Trial/Standard has a dedicated honest handler; keep it (§9).
            return _render_trial_vs_standard(text or aq.get("raw", ""),
                                             compare_trial_vs_standard())

        if metric == AQ.M_DURATION:
            body, ledger, pairs = _render_duration_answer(aq, duration_profile(aq))
        elif metric in (AQ.M_POST_DAY, AQ.M_POST_HOUR):
            body, ledger, pairs = _render_posting_time_answer(aq, posting_time_profile(aq))
        elif metric == AQ.M_POST_AGE:
            body, ledger, pairs = _render_post_age_answer(aq, post_age_profile(aq))
        elif metric == AQ.M_ROW_COUNT:
            body, ledger, pairs = _render_count_answer(aq, row_count_profile(aq))
        elif metric == AQ.M_PERFORMANCE:
            body, ledger, pairs = _render_perf_slice_answer(aq, performance_slice_profile(aq))
        elif metric in (AQ.M_VIEWS, AQ.M_LIKES, AQ.M_COMMENTS, AQ.M_SHARES,
                        AQ.M_ENGAGEMENT):
            body, ledger, pairs = _render_metric_answer(aq, metric_profile(aq))
        else:
            return None
        if not body:
            return None

        # Source integrity: only claim-bound sources may be rendered, and an
        # answer that carries no supported claim renders no Sources block (§14).
        verdict = SB.relevant_source_ids(body, [sid for sid, _l in pairs], ledger)
        LAST_SOURCE_AUDIT.update(before=len(pairs), after=len(verdict["keep"]),
                                 dropped=sorted(verdict["dropped"]),
                                 reason=verdict["reason"])
        kept = [(sid, line) for sid, line in pairs if sid in verdict["keep"]]
        block = SB.render_sources(kept, ledger)
        out = body if not block else f"{body}\n\n{block}"
        return st.compact_slack_response(out, st.detect_response_mode(text or ""))
    except Exception as e:  # noqa: BLE001 - never break the bot on an analytics ask
        log.warning("answer_analytics_query failed: %s", e)
        return None


def _render_duration_recommendation(aq: dict, prof: dict) -> tuple:
    """A length recommendation grounded in the cohort's real duration spread.

    §22: the frame resolves WHICH concept, the analytics informs HOW LONG. This
    commits to a target range (a recommendation, not a data dump) while keeping
    the evidence behind it visible and the association/causation line intact.
    """
    import source_binding as SB
    ledger = SB.ClaimLedger()
    pairs: list = []
    if not prof.get("ok"):
        return (f"I can't reach the analyzed sheet right now, so I'd rather not "
                f"put a number on it. ({prof.get('error')})", ledger, pairs)

    import analytics_query as AQ
    referent = aq.get("referent") or ""
    subject = f"*{referent}*" if referent else "this one"
    scope_bits = []
    for key in ("product", "icp"):
        for v in (aq.get("filters") or {}).get(key, []) or []:
            scope_bits.append(AQ.display(v))
    where = " / ".join(scope_bits)

    if prof.get("source") != "exact" or not prof.get("stats"):
        note = ("only coarse length buckets, so I can't give you a target in "
                "seconds" if prof.get("source") == "bucket"
                else "no measured durations at all")
        return (f"I'd keep {subject} short, but I want to be straight with you: I "
                f"have {note}, so that's a general short-form instinct rather than "
                f"something our own numbers back. Backfilling DURATION_SECONDS "
                f"would let me give you an actual target range.", ledger, pairs)

    s = prof["stats"]
    n = s["n"]
    lo, hi = int(round(s["median"] * 0.8)), int(round(s["median"] * 1.25))
    pairs.append(("S1", f"{n} {where or 'internal'} reels classified Great, with an "
                        f"exact duration"))
    ledger.add(f"Great {where or 'internal'} reels sit at a median of {s['median']}s "
               f"across {n}", ["S1"], SB.AGGREGATE_EVIDENCE)

    thin = ""
    if n < _MIN_PER_COHORT:
        thin = (f" Caveat: that's only {n} reel{'s' if n != 1 else ''}, so treat the "
                f"range as a starting point rather than a proven window.")
        ledger.add("the cohort behind this is thin", ["S1"], SB.SCHEMA_EVIDENCE)
    comp = prof.get("comparison") or {}
    contrast = ""
    if comp:
        contrast = (f" For contrast, the rest of the labelled library sits at a "
                    f"median of {_fmt_secs(comp['median'])} (n={comp['n']}).")
        ledger.add(f"rest of library median is {comp['median']}s", ["S1"],
                   SB.AGGREGATE_EVIDENCE)

    scope_phrase = f"our Great {where} reels" if where else "our Great reels"
    return (f"Target roughly {lo}–{hi} seconds for {subject}. That's built on "
            f"{scope_phrase}: median {_fmt_secs(s['median'])} across {n} with an "
            f"exact duration, ranging {_fmt_secs(s['min'])}–{_fmt_secs(s['max'])}."
            f"{contrast} I'd treat the range as the useful part — the shorter cuts "
            f"are associated with the stronger results here, but I wouldn't claim a "
            f"specific second count is what makes them work.{thin}", ledger, pairs)


def answer_duration_recommendation(aq: dict, text: str = "",
                                   context: Optional[list] = None) -> Optional[str]:
    """Slack entrypoint for an analytics-informed length recommendation."""
    import source_binding as SB
    if not aq:
        return None
    try:
        body, ledger, pairs = _render_duration_recommendation(aq, duration_profile(aq))
        if not body:
            return None
        verdict = SB.relevant_source_ids(body, [sid for sid, _l in pairs], ledger)
        LAST_SOURCE_AUDIT.update(before=len(pairs), after=len(verdict["keep"]),
                                 dropped=sorted(verdict["dropped"]),
                                 reason=verdict["reason"])
        kept = [(sid, line) for sid, line in pairs if sid in verdict["keep"]]
        block = SB.render_sources(kept, ledger)
        out = body if not block else f"{body}\n\n{block}"
        return st.compact_slack_response(out, st.detect_response_mode(text or ""))
    except Exception as e:  # noqa: BLE001
        log.warning("answer_duration_recommendation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CLI: audit-analytics-coverage (read-only)
# ---------------------------------------------------------------------------
def analytics_coverage(rows: Optional[list[dict]] = None,
                       columns: Optional[list[str]] = None) -> dict:
    """What the live dataset can and cannot answer, per analytic dimension.

    Read-only and fail-soft. This is the honest inventory behind every answer in
    this module: exact-duration coverage overall and for the Great cohort,
    publication-date vs publication-TIME coverage, whether any posting-time
    window carries enough labelled posts to compare, and whether Trial/Standard
    is present at all. Nothing is estimated — a dimension we cannot support is
    reported as unsupported.
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
        if err:
            return {"ok": False, "error": err}
    internal = _internal_rows_only(rows)
    buckets = performance.buckets_for_rows(internal)
    great = [r for r in internal if performance.is_positive(buckets.get(r["_row"], ""))]

    dur_col = _column_for_field(columns, "duration")
    dur_all = [r for r in internal if dur_col and _parse_seconds(r.get(dur_col)) is not None]
    dur_great = [r for r in great if dur_col and _parse_seconds(r.get(dur_col)) is not None]

    audit_links = {}
    if len(dur_all) < len(internal):
        try:
            audit_links = content_audit_duration_buckets(
                {str(r.get("LINK", "")).strip() for r in internal}) or {}
        except Exception:  # noqa: BLE001
            audit_links = {}

    tf = temporal_fields(internal, columns)
    hour_aq = {"metric": "POST_HOUR", "filters": {}, "cohort": {"basis": "all"},
               "question_type": "availability", "raw": "", "top_n": 5}
    day_aq = dict(hour_aq, metric="POST_DAY_OF_WEEK")
    hour_prof = posting_time_profile(hour_aq, internal, columns)
    day_prof = posting_time_profile(day_aq, internal, columns)

    reel_type_col = _column_for_field(columns, "reel_type")
    typed = {"trial": 0, "standard": 0, "unknown": 0}
    for r in internal:
        typed[classify_reel_type(r)] += 1

    def _pct(n, d):
        return round(100 * n / d) if d else 0

    return {
        "ok": True, "error": "",
        "internal_rows": len(internal),
        "performance_labelled": len(buckets),
        "great_rows": len(great),
        "duration": {
            "column": dur_col,
            "exact_coverage": len(dur_all),
            "exact_coverage_pct": _pct(len(dur_all), len(internal)),
            "great_exact_coverage": len(dur_great),
            "great_exact_coverage_pct": _pct(len(dur_great), len(great)),
            "content_audit_bucket_coverage": len(audit_links),
            "supports_exact_analysis": len(dur_great) >= _MIN_FOR_PATTERN,
        },
        "temporal": {
            "date_column": tf.get("date_column"),
            "timestamp_column": tf.get("timestamp_column"),
            "with_date": tf.get("with_date"), "with_date_pct": _pct(tf.get("with_date", 0), len(internal)),
            "with_time": tf.get("with_time"), "with_time_pct": _pct(tf.get("with_time", 0), len(internal)),
            "day_derivable": tf.get("day_of_week_derivable"),
            "hour_derivable": tf.get("hour_derivable"),
            "timezone": tf.get("timezone"),
            "timezone_is_utc_default": tf.get("timezone_is_utc_default"),
            "day_windows_with_enough": day_prof.get("windows_with_enough", []),
            "hour_windows_with_enough": hour_prof.get("windows_with_enough", []),
            "min_per_window": _MIN_PER_TIME_WINDOW,
            "supports_day_analysis": bool(day_prof.get("sufficient")),
            "supports_hour_analysis": bool(hour_prof.get("sufficient")),
        },
        "reel_type": {
            "column": reel_type_col, "trial": typed["trial"],
            "standard": typed["standard"], "unknown": typed["unknown"],
            "supports_trial_split": typed["trial"] >= _MIN_PER_COHORT
            and typed["standard"] >= _MIN_PER_COHORT,
        },
        "metrics": {m: availability(m, internal, columns)
                    for m in ("VIEWS", "LIKES", "COMMENTS", "SHARES",
                              "ENGAGEMENT_RATE", "DURATION_SECONDS")},
    }


def render_analytics_coverage(c: dict) -> str:
    """Plain-text coverage report for the CLI. Nothing written."""
    if not c.get("ok"):
        return f"audit-analytics-coverage: {c.get('error')}. Nothing written."
    d, t, rt = c["duration"], c["temporal"], c["reel_type"]
    L = ["Analytics coverage audit (read-only — live POC tab)",
         f"  internal rows: {c['internal_rows']}  ·  performance-labelled: "
         f"{c['performance_labelled']}  ·  Great: {c['great_rows']}",
         "",
         "  DURATION",
         f"    exact column:            {d['column'] or 'ABSENT'}",
         f"    exact coverage:          {d['exact_coverage']}/{c['internal_rows']} "
         f"({d['exact_coverage_pct']}%)",
         f"    Great-cohort coverage:   {d['great_exact_coverage']}/{c['great_rows']} "
         f"({d['great_exact_coverage_pct']}%)",
         f"    Content-audit buckets:   {d['content_audit_bucket_coverage']} "
         f"(coarse proxy only, never exact seconds)",
         f"    supports exact analysis: {'YES' if d['supports_exact_analysis'] else 'NO'}",
         "",
         "  TIME",
         f"    date column:             {t['date_column'] or 'ABSENT'}",
         f"    timestamp column:        {t['timestamp_column'] or 'ABSENT'}",
         f"    rows with a date:        {t['with_date']} ({t['with_date_pct']}%)",
         f"    rows with a TIME of day: {t['with_time']} ({t['with_time_pct']}%)",
         f"    timezone:                {t['timezone']}"
         f"{' (default — posting timezone not configured)' if t['timezone_is_utc_default'] else ''}",
         f"    day windows >= {t['min_per_window']}:       "
         f"{', '.join(t['day_windows_with_enough']) or 'none'}",
         f"    hour windows >= {t['min_per_window']}:      "
         f"{', '.join(t['hour_windows_with_enough']) or 'none'}",
         f"    supports day analysis:   {'YES' if t['supports_day_analysis'] else 'NO'}",
         f"    supports hour analysis:  {'YES' if t['supports_hour_analysis'] else 'NO'}",
         "",
         "  TRIAL / STANDARD",
         f"    column:                  {rt['column'] or 'ABSENT'}",
         f"    trial / standard / unknown: {rt['trial']} / {rt['standard']} / {rt['unknown']}",
         f"    supports the split:      {'YES' if rt['supports_trial_split'] else 'NO'}",
         "",
         "  METRIC AVAILABILITY LADDER"]
    for name, a in c["metrics"].items():
        L.append(f"    {name:20} {a['state']:14} {a['n_with_value']}/{a['n_rows']} rows"
                 f"{'  col=' + a['column'] if a.get('column') else ''}")
    return "\n".join(L)
