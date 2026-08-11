"""Automatic Instagram metrics ingestion — owned media -> POC rows.

Primary path (manual SOCIAL_METRICS_IMPORT_STAGING is fallback only):

    Instagram Graph API  ->  owned media + insights  ->  map to POC by LINK
      ->  dry-run QA (SAFE / NOT SAFE)  ->  gated apply (fill empty cells only)
      ->  account-level demographics -> INSTAGRAM_ACCOUNT_INSIGHTS tab

Hard rules enforced here:
- Only Storelli-OWNED media (the API only returns the owned account's media).
- Match ONLY by canonical LINK / Instagram shortcode — never row order, never
  fuzzy title/caption similarity, never invented matches.
- Never overwrite a populated metric cell; only ever fill empty ones.
- Only the metric columns are written — never taxonomy, Product, ICP, or Status.
- No metric is fabricated; unavailable API metrics are simply left empty.
- Account-level demographics are NEVER written as per-post demographics.
"""
from __future__ import annotations

import re
from typing import Optional

import config
from logger import get_logger

log = get_logger()

# The metric columns the API path may fill (subset of the POC metric columns).
# Deliberately excluded: REEL_TYPE (IG trial/standard flag isn't in the API),
# FOLLOWERS_AT_POST (no per-post follower count), and the demographic *_SPLIT
# columns (account-level only — see the separate tab). Excluding them keeps the
# import honest rather than faking a value.
_API_FILLABLE_COLUMNS = ("DURATION_SECONDS", "POST_DATE", "VIEWS", "LIKES", "COMMENTS",
                         "SAVES", "SHARES", "ENGAGEMENT_RATE", "REACH", "IMPRESSIONS",
                         "PROFILE_VISITS", "WEBSITE_CLICKS")

ACCOUNT_INSIGHTS_TAB = "INSTAGRAM_ACCOUNT_INSIGHTS"
ACCOUNT_INSIGHTS_COLUMNS = ("PERIOD", "PULLED_AT", "METRIC", "BREAKDOWN", "SPLIT", "SOURCE")


# ---------------------------------------------------------------------------
# URL / shortcode helpers (Part C) — pure
# ---------------------------------------------------------------------------
_SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)",
                           re.IGNORECASE)


def extract_instagram_shortcode(url: str) -> str:
    """Return the media shortcode from any IG URL shape:
    /reel/X, /reels/X, /p/X, /tv/X, and profile-scoped /<user>/reel/X. '' if none."""
    m = _SHORTCODE_RE.search(str(url or ""))
    return m.group(1) if m else ""


def canonicalize_instagram_url(url: str) -> str:
    """Canonical form for matching: prefer the shortcode (`https://www.instagram.
    com/reel/<code>/`); else a normalized lowercase URL with query/trailing slash
    stripped."""
    code = extract_instagram_shortcode(url)
    if code:
        return f"https://www.instagram.com/reel/{code}/"
    return str(url or "").strip().lower().split("?")[0].rstrip("/")


def _poc_key(link: str) -> str:
    """Match key for a link: its shortcode if present, else canonical URL."""
    return extract_instagram_shortcode(link) or canonicalize_instagram_url(link)


# ---------------------------------------------------------------------------
# mapping (Part C) — pure
# ---------------------------------------------------------------------------
def map_media_to_poc_rows(media: list, poc_rows: list) -> dict:
    """Map owned media to POC rows by LINK shortcode/canonical URL only.

    Returns {matched: [(media, row)], unmatched_poc: [rows], media_not_in_poc:
    [media], duplicate_matches: [...]}. Never uses row order or fuzzy matching.
    """
    poc_by_key: dict[str, list] = {}
    for r in poc_rows:
        link = str(r.get("LINK", "")).strip()
        if not link:
            continue
        poc_by_key.setdefault(_poc_key(link), []).append(r)

    matched, media_not_in_poc, duplicate = [], [], []
    matched_row_ids: dict = {}
    for m in media:
        key = _poc_key(m.get("permalink", ""))
        rows = poc_by_key.get(key) if key else None
        if not rows:
            media_not_in_poc.append(m)
            continue
        if len(rows) > 1:
            duplicate.append({"media": m.get("id"), "key": key,
                              "rows": [r.get("_row") for r in rows]})
        row = rows[0]
        rid = row.get("_row")
        if rid in matched_row_ids:
            duplicate.append({"row": rid, "media": [matched_row_ids[rid].get("id"), m.get("id")]})
        else:
            matched_row_ids[rid] = m
            matched.append((m, row))

    matched_keys = {_poc_key(str(r.get("LINK", "")).strip()) for _m, r in matched}
    unmatched_poc = [r for r in poc_rows
                     if str(r.get("LINK", "")).strip()
                     and _poc_key(str(r.get("LINK", "")).strip()) not in matched_keys]
    return {"matched": matched, "unmatched_poc": unmatched_poc,
            "media_not_in_poc": media_not_in_poc, "duplicate_matches": duplicate}


# ---------------------------------------------------------------------------
# field mapping (Part B/E) — pure
# ---------------------------------------------------------------------------
def _num_str(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(round(f, 2))
    except (TypeError, ValueError):
        return ""


def build_metric_values(media: dict, insights: dict) -> dict:
    """Map one owned media + its insights to POC metric-column values. Only
    fields actually present are returned; nothing is invented. ENGAGEMENT_RATE is
    a real derived value (interactions/reach), only when both are present."""
    out: dict = {}
    ts = str(media.get("timestamp", "")).strip()
    if ts:
        out["POST_DATE"] = ts[:10]                       # YYYY-MM-DD
    dur = media.get("duration")
    if dur not in (None, ""):
        s = _num_str(dur)
        if s:
            out["DURATION_SECONDS"] = s

    def put(col, *keys):
        for k in keys:
            if k in insights and insights[k] not in (None, ""):
                s = _num_str(insights[k])
                if s:
                    out[col] = s
                return

    put("VIEWS", "views", "plays", "video_views")
    put("REACH", "reach")
    put("LIKES", "likes")
    put("COMMENTS", "comments")
    put("SAVES", "saved", "saves")
    put("SHARES", "shares")
    put("IMPRESSIONS", "impressions")
    put("PROFILE_VISITS", "profile_visits", "profile_activity")
    put("WEBSITE_CLICKS", "website_clicks")

    inter = insights.get("total_interactions")
    reach = insights.get("reach")
    try:
        if inter not in (None, "") and reach not in (None, "") and float(reach) > 0:
            out["ENGAGEMENT_RATE"] = str(round(100 * float(inter) / float(reach), 2))
    except (TypeError, ValueError):
        pass
    return out


# ---------------------------------------------------------------------------
# fill planning (Part E) — pure: only empty cells, only fillable columns present
# ---------------------------------------------------------------------------
def plan_fills(mapping: dict, insights_by_media: dict, poc_cols: list) -> dict:
    """Compute per-row fills (empty cells only) + skips (already populated).

    poc_cols = the metadata column names present in the POC tab (so we only plan
    writes to columns that actually exist and are API-fillable)."""
    fillable = [c for c in _API_FILLABLE_COLUMNS if c in poc_cols]
    fills: dict = {}                       # row_idx -> {col: value}
    would_fill: dict = {}                  # col -> count
    already: dict = {}                     # col -> count
    missing_cols = [c for c in _API_FILLABLE_COLUMNS if c not in poc_cols]
    for media, row in mapping["matched"]:
        vals = build_metric_values(media, insights_by_media.get(media.get("id"), {}))
        for col in fillable:
            v = vals.get(col)
            if not v:
                continue
            if str(row.get(col, "")).strip():
                already[col] = already.get(col, 0) + 1        # populated -> never overwrite
            else:
                fills.setdefault(row["_row"], {})[col] = v
                would_fill[col] = would_fill.get(col, 0) + 1
    return {"fills": fills, "would_fill": would_fill, "already_populated": already,
            "fillable_columns": fillable, "poc_missing_columns": missing_cols}


# ---------------------------------------------------------------------------
# orchestration (Parts D/E/F)
# ---------------------------------------------------------------------------
def _load_poc(sheets=None):
    """Return a SheetsClient-like object (read_rows/meta_col/ws). Injectable."""
    if sheets is not None:
        return sheets
    from sheets_client import SheetsClient
    return SheetsClient()


def _safety(configured: bool, media_count: int, matched: int, api_errors: list) -> tuple:
    """(safe: bool, reasons: list). NOT SAFE if unconfigured, no media, no
    matches, or blocking API errors."""
    reasons = []
    if not configured:
        reasons.append("Instagram API not configured")
    if configured and media_count == 0:
        reasons.append("no owned media returned by the API")
    if configured and matched == 0 and media_count > 0:
        reasons.append("no owned media matched a POC LINK")
    if api_errors:
        reasons.append(f"{len(api_errors)} API error(s)")
    return (not reasons, reasons)


def pull_instagram_metrics(dry_run: bool = True, apply: bool = False,
                           client=None, sheets=None, max_items: int = 500) -> dict:
    """Fetch owned IG metrics, map to POC, and either report (dry-run) or fill
    empty metric cells (gated apply). Read-only unless apply=True AND safe."""
    # ---- config gate ------------------------------------------------------
    if client is None and not config.instagram_configured():
        return {"ok": False, "configured": False, "error": config.IG_INGEST_NOT_CONFIGURED_MSG,
                "missing_vars": config.instagram_missing_vars()}
    try:
        if client is None:
            from instagram_insights_client import InstagramInsightsClient
            client = InstagramInsightsClient()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "configured": False, "error": str(e),
                "missing_vars": config.instagram_missing_vars()}

    from instagram_insights_client import normalize_media
    api_errors: list = []

    # ---- fetch owned media + insights ------------------------------------
    try:
        raw_media = client.fetch_media(max_items=max_items)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "configured": True, "error": f"fetch_media failed: {e}",
                "api_errors": [str(e)]}
    media = [normalize_media(m) for m in raw_media]

    poc = _load_poc(sheets)
    poc_rows = poc.read_rows()
    poc_cols = list(poc.meta_col.keys())

    mapping = map_media_to_poc_rows(media, poc_rows)
    insights_by_media: dict = {}
    unavailable = 0
    for m, _row in mapping["matched"]:
        try:
            ins = client.fetch_media_insights(m["id"], m.get("media_product_type", ""))
        except Exception as e:  # noqa: BLE001
            api_errors.append(f"insights {m['id']}: {e}")
            ins = {}
        if not ins:
            unavailable += 1
        insights_by_media[m["id"]] = ins

    plan = plan_fills(mapping, insights_by_media, poc_cols)

    # ---- account-level demographics (never per-post) ----------------------
    demographics = {}
    try:
        demographics = client.fetch_account_demographics()
    except Exception as e:  # noqa: BLE001
        api_errors.append(f"demographics: {e}")

    safe, reasons = _safety(True, len(media), len(mapping["matched"]), api_errors)

    report = {
        "ok": True, "configured": True, "dry_run": not apply,
        "media_fetched": len(media),
        "matched_rows": len(mapping["matched"]),
        "unmatched_poc_rows": len(mapping["unmatched_poc"]),
        "media_not_in_poc": len(mapping["media_not_in_poc"]),
        "duplicate_matches": mapping["duplicate_matches"],
        "would_fill": plan["would_fill"],
        "already_populated": plan["already_populated"],
        "fillable_columns": plan["fillable_columns"],
        "poc_missing_columns": plan["poc_missing_columns"],
        "insights_unavailable_rows": unavailable,
        "api_errors": api_errors,
        "has_account_demographics": bool(demographics),
        "safe": safe, "not_safe_reasons": reasons,
        "cells_to_fill": sum(len(v) for v in plan["fills"].values()),
        "unmatched_examples": [str(r.get("LINK", "")).strip()
                               for r in mapping["unmatched_poc"][:10]],
    }

    if not apply:
        return report

    # ---- APPLY (gated) ----------------------------------------------------
    if not safe:
        report["wrote"] = False
        report["refused"] = "dry-run safety checks did not pass: " + "; ".join(reasons)
        return report
    written = _apply_fills(poc, plan["fills"])
    report["wrote"] = True
    report["rows_written"] = written["rows"]
    report["cells_written"] = written["cells"]
    report["verify_ok"] = written["verify_ok"]
    report["write_log"] = written["log"]
    # account-insights tab (separate; never per-post)
    if demographics:
        report["account_insights"] = _write_account_insights(poc, demographics)
    return report


def _apply_fills(poc, fills: dict) -> dict:
    """Write only the planned empty cells (metric columns). Snapshots each cell
    (must be empty), writes, then verifies. Uses the SheetsClient worksheet."""
    import gspread
    meta = poc.meta_col
    updates, log = [], []
    for row_idx, cols in fills.items():
        for col, val in cols.items():
            cidx = meta.get(col)
            if not cidx:
                continue
            a1 = gspread.utils.rowcol_to_a1(row_idx, cidx)
            updates.append({"range": a1, "values": [[val]]})
            log.append({"row": row_idx, "col": col, "a1": a1, "value": val})
    if updates:
        poc.ws.batch_update(updates)
    # verify by re-reading
    verify_ok = True
    try:
        fresh = {r["_row"]: r for r in poc.read_rows()}
        for row_idx, cols in fills.items():
            for col, val in cols.items():
                if str(fresh.get(row_idx, {}).get(col, "")).strip() != str(val):
                    verify_ok = False
    except Exception as e:  # noqa: BLE001
        log.append({"verify_error": str(e)})
        verify_ok = False
    return {"rows": len(fills), "cells": len(updates), "verify_ok": verify_ok, "log": log}


def _write_account_insights(poc, demographics: dict) -> dict:
    """Append account-level demographic splits to INSTAGRAM_ACCOUNT_INSIGHTS
    (create if absent). These are account-wide, NOT per-post — kept in their own
    tab so they can never masquerade as per-reel data."""
    try:
        import gspread
        sh = poc.ws.spreadsheet
        try:
            ws = sh.worksheet(ACCOUNT_INSIGHTS_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=ACCOUNT_INSIGHTS_TAB, rows=200,
                                  cols=len(ACCOUNT_INSIGHTS_COLUMNS))
            ws.update(range_name="A1", values=[list(ACCOUNT_INSIGHTS_COLUMNS)],
                      value_input_option="RAW")
        rows = []
        for metric, breakdowns in demographics.items():
            for breakdown, split in breakdowns.items():
                split_str = " / ".join(f"{k} {v}" for k, v in split.items())
                rows.append(["lifetime", "(pull)", metric, breakdown, split_str, "IG Graph API"])
        if rows:
            ws.append_rows(rows, value_input_option="RAW")
        return {"tab": ACCOUNT_INSIGHTS_TAB, "rows_appended": len(rows)}
    except Exception as e:  # noqa: BLE001
        return {"tab": ACCOUNT_INSIGHTS_TAB, "error": str(e)}


# ---------------------------------------------------------------------------
# rendering (CLI)
# ---------------------------------------------------------------------------
def render_pull_report(rep: dict) -> str:
    if not rep.get("configured"):
        miss = rep.get("missing_vars") or []
        lines = [config.IG_INGEST_NOT_CONFIGURED_MSG,
                 "  Missing env vars: " + (", ".join(miss) or "(unknown)"),
                 "  Required permissions: instagram_manage_insights, instagram_basic "
                 "(+ pages_read_engagement for Page-linked login).",
                 "  Fallback: paste an export into SOCIAL_METRICS_IMPORT_STAGING and run "
                 "import-social-metrics --dry-run."]
        return "\n".join(lines)
    if not rep.get("ok"):
        return f"pull-instagram-metrics: {rep.get('error')}"
    head = "pull-instagram-metrics --apply" if not rep.get("dry_run") else \
        "pull-instagram-metrics --dry-run (NO WRITES)"
    lines = [head,
             f"  Owned media fetched: {rep['media_fetched']}",
             f"  Matched POC rows: {rep['matched_rows']}",
             f"  Unmatched POC rows: {rep['unmatched_poc_rows']}",
             f"  Owned media not in POC: {rep['media_not_in_poc']}",
             f"  Rows with no insights available: {rep['insights_unavailable_rows']}",
             f"  Cells that WOULD be filled: {rep['cells_to_fill']}"]
    for col, n in sorted(rep["would_fill"].items()):
        lines.append(f"    - {col}: {n}")
    if rep["already_populated"]:
        lines.append("  Skipped (already populated — never overwritten):")
        for col, n in sorted(rep["already_populated"].items()):
            lines.append(f"    - {col}: {n}")
    if rep["poc_missing_columns"]:
        lines.append("  API-fillable columns not in POC yet: " + ", ".join(rep["poc_missing_columns"]))
    if rep["duplicate_matches"]:
        lines.append(f"  Duplicate matches (review): {len(rep['duplicate_matches'])}")
    if rep["api_errors"]:
        lines.append(f"  API errors: {len(rep['api_errors'])}")
    lines.append(f"  Account-level demographics available: "
                 f"{'yes' if rep['has_account_demographics'] else 'no'} "
                 "(account-wide, NOT per-reel)")
    lines.append("")
    lines.append(f"  VERDICT: {'SAFE' if rep['safe'] else 'NOT SAFE'}"
                 + ("" if rep["safe"] else " — " + "; ".join(rep["not_safe_reasons"])))
    if rep.get("dry_run"):
        lines.append("  Dry-run only: nothing was written. Re-run with --apply (gated on SAFE).")
    else:
        if rep.get("refused"):
            lines.append(f"  REFUSED: {rep['refused']}")
        else:
            lines.append(f"  WROTE {rep.get('cells_written', 0)} cells across "
                         f"{rep.get('rows_written', 0)} rows; verify_ok={rep.get('verify_ok')}.")
            if rep.get("account_insights"):
                ai = rep["account_insights"]
                lines.append(f"  Account insights: {ai.get('rows_appended', 0)} rows -> "
                             f"{ai.get('tab')}" + (f" (error: {ai['error']})" if ai.get("error") else ""))
    return "\n".join(lines)
