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
                  "followers_at_post", "followers at post"),
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
    "views": "VIEWS", "likes": "LIKES", "comments": "COMMENTS", "saves": "SAVES",
    "shares": "SHARES", "engagement_rate": "ENGAGEMENT_RATE", "followers": "FOLLOWERS_AT_POST",
    "demo_age": "AGE_SPLIT", "demo_gender": "GENDER_SPLIT", "demo_location": "LOCATION_SPLIT",
    "demo_follower_split": "FOLLOWER_NONFOLLOWER_SPLIT", "demo_reach_segment": "REACH_BY_SEGMENT",
}
_OPTIONAL_COLUMNS = ("REACH", "IMPRESSIONS", "PROFILE_VISITS", "WEBSITE_CLICKS",
                     "PRODUCT_CLICKS", "TRIAL_CLICKS", "QUALIFIED_DMS")

# Required metric columns to add to the POC tab, in insertion order.
_REQUIRED_METRIC_COLUMNS = ("REEL_TYPE", "DURATION_SECONDS", "POST_DATE", "VIEWS", "LIKES",
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
_TEXT_METRIC_COLUMNS = {"REEL_TYPE", "POST_DATE", "SOURCE", "IMPORTED_AT", "NOTES", "LINK"}

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

_TESTPLAN_STRONG = ("test plan", "creative test plan", "testing plan", "ideas to test",
                    "ideas we should test", "ideas we can test", "test ideas", "ideas to run as tests")
_TESTPLAN_ASK = ("what should we test", "what tests should we run", "what should we test next",
                 "which formats should we test", "what to test next", "tests should we run next",
                 "what should we test based", "what do we test next", "what can we test")


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
    if any(k in t for k in _TRIAL_KW):
        return True
    if any(k in t for k in _DEMO_KW):
        return True
    if any(k in t for k in _AUDIT_KW):
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


def answer_social_analytics_question(text: str, context: Optional[list] = None) -> Optional[str]:
    """Slack entrypoint for analytics questions (schema plan, import/staging,
    duration buckets, trial/standard, demographics, duration, metrics audit).
    Returns None if it doesn't own it."""
    t = _lower(text)
    try:
        # Schema-plan asks first — "what do we need to track to answer X" mentions
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
    safe = bool(status_col and hook_col and status_col < hook_col)
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
        "safe": safe,
    }


def _col_letter(n: int) -> str:
    """1-based column index -> A1 letter(s)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def insertion_plan(include_optional: bool = True, values: Optional[list] = None) -> dict:
    """The exact plan for adding the metric columns between Status and HOOK.

    Read-only (computes the plan; performs no write)."""
    pf = preflight_poc_structure(values)
    cols = list(_REQUIRED_METRIC_COLUMNS) + (list(_OPTIONAL_COLUMNS) if include_optional else [])
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
        f"  Status immediately before HOOK: {'YES (safe)' if pf['status_immediately_before_hook'] else 'NO — review'}",
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
def insert_poc_metric_columns(include_optional: bool = True, apply: bool = False) -> dict:
    """Plan (and, only when apply=True + guards pass, perform) the insertion of
    the metric columns between Status and HOOK.

    Guards that block a write: header unreadable, Status NOT immediately before
    HOOK (unsafe boundary), or ANY target column already present (idempotent —
    never double-inserts). New columns get a blank row-1 category and the name in
    row 2; data cells are left empty (no fabricated values). Existing analyzed
    data is shifted right, never overwritten."""
    values = _poc_values()
    plan = insertion_plan(include_optional, values)
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
    if not (pf["safe"] and pf["status_immediately_before_hook"]):
        return {"ok": False, "wrote": False, "plan": plan,
                "error": "unsafe insertion boundary (Status is not immediately before HOOK)"}
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
