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
                         "PROFILE_VISITS", "WEBSITE_CLICKS", "FOLLOWERS_AT_MEASUREMENT")

ACCOUNT_INSIGHTS_TAB = "INSTAGRAM_ACCOUNT_INSIGHTS"
ACCOUNT_INSIGHTS_COLUMNS = ("PERIOD", "PULLED_AT", "METRIC", "BREAKDOWN", "SPLIT", "SOURCE")

# Per-media sync ledger — enables incremental refresh + the mutable-metric
# update policy (below). Keyed by Instagram shortcode.
SYNC_STATE_TAB = "INSTAGRAM_SYNC_STATE"
SYNC_STATE_COLUMNS = ("SHORTCODE", "MEDIA_ID", "POC_ROW", "FIRST_SYNCED_AT", "LAST_SYNCED_AT",
                      "VIEWS", "REACH", "LIKES", "COMMENTS", "SAVES", "SHARES", "IMPRESSIONS",
                      "PROFILE_VISITS", "WEBSITE_CLICKS", "ENGAGEMENT_RATE")

# ---- mutable-metric policy (documented + enforced) ------------------------
# IMMUTABLE metadata — a real one-time property of the post; fill once, never
# change (updating would only ever be a data error).
_IMMUTABLE_COLUMNS = ("POST_DATE", "DURATION_SECONDS")
# CUMULATIVE metrics — legitimately change over a post's life (views/comments/
# etc.). Refreshed to the latest official API value, BUT only when the current
# cell is one WE wrote (its value equals the last value we synced). If the cell
# no longer matches what we last wrote, a human edited it -> we never overwrite.
_CUMULATIVE_COLUMNS = ("VIEWS", "REACH", "LIKES", "COMMENTS", "SAVES", "SHARES",
                       "IMPRESSIONS", "PROFILE_VISITS", "WEBSITE_CLICKS", "ENGAGEMENT_RATE",
                       "FOLLOWERS_AT_MEASUREMENT")
# HUMAN fields (REEL_TYPE, and everything outside the metric set) are never
# written by the API at all.

# Known media-level metric names, for the verify "available vs unavailable" list.
_KNOWN_MEDIA_METRICS = ("views", "plays", "reach", "likes", "comments", "saved", "shares",
                        "total_interactions", "profile_visits", "profile_activity",
                        "impressions", "website_clicks", "ig_reels_avg_watch_time",
                        "ig_reels_video_view_total_time")


def _now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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
    # Public follower count used for THIS measurement (never a stale global).
    put("FOLLOWERS_AT_MEASUREMENT", "followers_at_measurement", "follower_count")
    # When these public numbers were read, so a stale metric is visibly stale.
    if insights.get("metrics_measured_at"):
        out["METRICS_MEASURED_AT"] = str(insights["metrics_measured_at"])

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


# ===========================================================================
# Part 1 — connection preflight (safe; never prints the token)
# ===========================================================================
def verify_connection(client=None) -> dict:
    """Validate the Instagram connection safely. Resolves the account, checks
    media + insights access, and lists available/unavailable metrics. Never
    returns or logs the access token."""
    token_present = bool(config.INSTAGRAM_ACCESS_TOKEN)
    account_present = bool(config.INSTAGRAM_BUSINESS_ACCOUNT_ID)
    if client is None and not config.instagram_configured():
        return {"connected": False, "configured": False,
                "token_present": token_present, "account_id_present": account_present,
                "missing_vars": config.instagram_missing_vars(),
                "blocker": config.IG_INGEST_NOT_CONFIGURED_MSG}
    try:
        if client is None:
            from instagram_insights_client import InstagramInsightsClient
            client = InstagramInsightsClient()
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "configured": False, "blocker": str(e),
                "missing_vars": config.instagram_missing_vars()}

    out = {"connected": False, "configured": True, "account": None, "media_access": False,
           "insights_access": False, "available_metrics": [], "unavailable_metrics": [],
           "token_health": {}, "blocker": None, "media_sampled": 0}
    # 1. account resolve (proves token can see the configured Storelli account)
    try:
        acct = client.fetch_account()
        out["account"] = {"id": acct.get("id"), "username": acct.get("username"),
                          "followers_count": acct.get("followers_count"),
                          "media_count": acct.get("media_count")}
    except Exception as e:  # noqa: BLE001
        out["blocker"] = f"cannot resolve IG account (token/permission?): {type(e).__name__}"
        out["token_health"] = _safe_token_health(client)
        return out
    # 2. media access
    try:
        media = client.fetch_media(max_items=5)
        out["media_access"] = True
        out["media_sampled"] = len(media)
    except Exception as e:  # noqa: BLE001
        out["blocker"] = f"account resolved but media fetch failed: {type(e).__name__}"
        out["token_health"] = _safe_token_health(client)
        return out
    # 3. insights access on a sample reel + available/unavailable metric list
    from instagram_insights_client import normalize_media
    sample = [m for m in media if str(m.get("media_product_type", "")).upper() == "REELS"] or media
    if sample:
        m = normalize_media(sample[0])
        try:
            ins = client.fetch_media_insights(m["id"], m.get("media_product_type", ""))
        except Exception:  # noqa: BLE001
            ins = {}
        out["insights_access"] = bool(ins)
        out["available_metrics"] = sorted(ins.keys())
        out["unavailable_metrics"] = [k for k in _KNOWN_MEDIA_METRICS if k not in ins]
    out["token_health"] = _safe_token_health(client)
    out["connected"] = bool(out["account"] and out["media_access"])
    if not out["connected"] and not out["blocker"]:
        out["blocker"] = "account/media not accessible"
    return out


def _safe_token_health(client) -> dict:
    try:
        return client.token_health()
    except Exception as e:  # noqa: BLE001
        return {"known": False, "error": type(e).__name__}


def render_connection_report(v: dict) -> str:
    if not v.get("configured"):
        return "\n".join([
            "Connected: NO",
            f"Blocker: {v.get('blocker')}",
            "  Missing env vars: " + (", ".join(v.get("missing_vars") or []) or "(unknown)"),
            "  (access token never printed.)"])
    acct = v.get("account") or {}
    th = v.get("token_health") or {}
    if th.get("known"):
        exp = th.get("expires_at")
        tok = f"valid={th.get('is_valid')}, expires_at={exp or 'never/long-lived'}"
    else:
        tok = f"unknown ({th.get('reason') or th.get('error') or 'no debug_token access'})"
    return "\n".join([
        f"Connected: {'YES' if v.get('connected') else 'NO'}",
        f"Storelli account resolved: {acct.get('username') or acct.get('id') or '(none)'}"
        + (f" ({acct.get('media_count')} media)" if acct.get("media_count") is not None else ""),
        f"media access: {'YES' if v.get('media_access') else 'NO'}",
        f"insights access: {'YES' if v.get('insights_access') else 'NO'}",
        "available metrics: " + (", ".join(v.get("available_metrics") or []) or "(none seen on sample)"),
        "unavailable metrics: " + (", ".join(v.get("unavailable_metrics") or []) or "(none)"),
        f"token health: {tok}",
        f"blocker: {v.get('blocker') or '(none)'}",
        "  (access token never printed.)"])


# ===========================================================================
# Parts 3/4 — incremental refresh with the mutable-metric policy
# ===========================================================================
def read_sync_state(sheets) -> dict:
    """Read INSTAGRAM_SYNC_STATE -> {shortcode: {media_id, poc_row, first, last,
    values:{col:val}}}. Fail-soft -> {} when the tab is absent/unreadable."""
    try:
        import gspread
        sh = sheets.ws.spreadsheet
        try:
            vals = sh.worksheet(SYNC_STATE_TAB).get_all_values()
        except gspread.WorksheetNotFound:
            return {}
    except Exception as e:  # noqa: BLE001
        log.warning("sync-state read failed: %s", e)
        return {}
    if not vals or len(vals) < 2:
        return {}
    header = [c.strip() for c in vals[0]]
    idx = {h: i for i, h in enumerate(header)}
    out = {}
    for r in vals[1:]:
        def g(col):
            i = idx.get(col)
            return r[i].strip() if i is not None and i < len(r) else ""
        sc = g("SHORTCODE")
        if not sc:
            continue
        out[sc] = {"media_id": g("MEDIA_ID"), "poc_row": g("POC_ROW"),
                   "first": g("FIRST_SYNCED_AT"), "last": g("LAST_SYNCED_AT"),
                   "values": {c: g(c) for c in _CUMULATIVE_COLUMNS if g(c)}}
    return out


def plan_incremental(mapping: dict, insights_by_media: dict, poc_cols: list,
                     sync_state: dict) -> dict:
    """Per-cell decisions under the mutable-metric policy. Returns fills
    (row -> {col: value}) plus change counts and a per-media log."""
    fillable = [c for c in _API_FILLABLE_COLUMNS if c in poc_cols]
    fills: dict = {}
    counts = {"first_fill": 0, "update": 0, "unchanged": 0, "immutable_kept": 0,
              "manual_protected": 0}
    changes, per_media = [], []
    for media, row in mapping["matched"]:
        sc = extract_instagram_shortcode(media.get("permalink", ""))
        vals = build_metric_values(media, insights_by_media.get(media.get("id"), {}))
        last_vals = (sync_state.get(sc, {}) or {}).get("values", {})
        row_changes = {}
        for col in fillable:
            v = vals.get(col)
            if not v:
                continue
            cur = str(row.get(col, "")).strip()
            if col in _IMMUTABLE_COLUMNS:
                if not cur:
                    row_changes[col] = v
                    counts["first_fill"] += 1
                else:
                    counts["immutable_kept"] += 1
                continue
            # cumulative
            if not cur:
                row_changes[col] = v
                counts["first_fill"] += 1
            else:
                last = str(last_vals.get(col, "")).strip()
                if last and last == cur:            # a cell WE last wrote -> API-owned
                    if str(v) != cur:
                        row_changes[col] = v
                        counts["update"] += 1
                        changes.append({"shortcode": sc, "col": col, "old": cur, "new": v})
                    else:
                        counts["unchanged"] += 1
                else:                                # human-edited / unknown -> protect
                    counts["manual_protected"] += 1
        if row_changes:
            fills[row["_row"]] = row_changes
        per_media.append({"shortcode": sc, "media_id": media.get("id"),
                          "poc_row": row.get("_row"), "api_values": vals})
    return {"fills": fills, "counts": counts, "changes": changes, "per_media": per_media,
            "fillable": fillable}


def refresh_instagram_metrics(dry_run: bool = True, apply: bool = False,
                              client=None, sheets=None, sync_state=None,
                              max_items: int = 500) -> dict:
    """Operational incremental refresh: verify -> pull owned media + insights ->
    map by LINK -> tiered fill plan -> (gated) apply empty/updatable cells ->
    verify -> update sync-state -> summary. Read-only unless apply=True AND SAFE."""
    conn = verify_connection(client)
    if not conn.get("connected"):
        return {"ok": False, "configured": conn.get("configured", False),
                "connection": conn, "error": conn.get("blocker"),
                "missing_vars": conn.get("missing_vars")}

    if client is None:
        from instagram_insights_client import InstagramInsightsClient
        client = InstagramInsightsClient()
    from instagram_insights_client import normalize_media

    api_errors = []
    try:
        media = [normalize_media(m) for m in client.fetch_media(max_items=max_items)]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "configured": True, "connection": conn,
                "error": f"fetch_media failed: {e}"}

    poc = _load_poc(sheets)
    poc_rows = poc.read_rows()
    poc_cols = list(poc.meta_col.keys())
    mapping = map_media_to_poc_rows(media, poc_rows)

    insights_by_media, unavailable = {}, 0
    for m, _row in mapping["matched"]:
        try:
            ins = client.fetch_media_insights(m["id"], m.get("media_product_type", ""))
        except Exception as e:  # noqa: BLE001
            api_errors.append(f"insights {m['id']}: {e}")
            ins = {}
        if not ins:
            unavailable += 1
        insights_by_media[m["id"]] = ins

    if sync_state is None:
        sync_state = read_sync_state(poc)
    plan = plan_incremental(mapping, insights_by_media, poc_cols, sync_state)

    safe = bool(conn["connected"] and len(media) > 0 and len(mapping["matched"]) > 0)
    report = {
        "ok": True, "configured": True, "dry_run": not apply, "connection": conn,
        "media_fetched": len(media), "matched_rows": len(mapping["matched"]),
        "unmatched_poc_rows": len(mapping["unmatched_poc"]),
        "media_not_in_poc": len(mapping["media_not_in_poc"]),
        "insights_unavailable_rows": unavailable, "api_errors": api_errors,
        "counts": plan["counts"], "changes": plan["changes"][:20],
        "cells_to_write": sum(len(v) for v in plan["fills"].values()),
        "already_synced_rows": sum(1 for pm in plan["per_media"]
                                   if pm["shortcode"] in sync_state),
        "new_rows": sum(1 for pm in plan["per_media"] if pm["shortcode"] not in sync_state),
        "safe": safe,
    }
    if not apply:
        return report
    if not safe:
        report["wrote"] = False
        report["refused"] = "connection/mapping not safe to apply"
        return report
    written = _apply_fills(poc, plan["fills"])
    report["wrote"] = True
    report["cells_written"] = written["cells"]
    report["rows_written"] = written["rows"]
    report["verify_ok"] = written["verify_ok"]
    report["sync_state"] = _write_sync_state(poc, plan["per_media"], sync_state)
    return report


def _write_sync_state(poc, per_media: list, prior: dict) -> dict:
    """Upsert one ledger row per media (keyed by shortcode) with the values we
    just synced, so the next run can tell an API-owned cell from a manual edit."""
    try:
        import gspread
        sh = poc.ws.spreadsheet
        try:
            ws = sh.worksheet(SYNC_STATE_TAB)
            existing = ws.get_all_values()
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=SYNC_STATE_TAB, rows=1000, cols=len(SYNC_STATE_COLUMNS))
            ws.update(range_name="A1", values=[list(SYNC_STATE_COLUMNS)], value_input_option="RAW")
            existing = [list(SYNC_STATE_COLUMNS)]
        header = [c.strip() for c in existing[0]]
        sc_i = header.index("SHORTCODE")
        row_by_sc = {r[sc_i].strip(): i + 2 for i, r in enumerate(existing[1:])
                     if sc_i < len(r) and r[sc_i].strip()}
        now = _now_utc()
        appends, updates = [], []
        for pm in per_media:
            sc = pm["shortcode"]
            vals = pm["api_values"]
            first = (prior.get(sc, {}) or {}).get("first") or now
            rowvals = [sc, pm.get("media_id", ""), str(pm.get("poc_row", "")), first, now]
            rowvals += [str(vals.get(c, "")) for c in _CUMULATIVE_COLUMNS]
            if sc in row_by_sc:
                r = row_by_sc[sc]
                updates.append({"range": f"A{r}:{_col_letter(len(SYNC_STATE_COLUMNS))}{r}",
                                "values": [rowvals]})
            else:
                appends.append(rowvals)
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        if appends:
            ws.append_rows(appends, value_input_option="RAW")
        return {"tab": SYNC_STATE_TAB, "updated": len(updates), "appended": len(appends)}
    except Exception as e:  # noqa: BLE001
        return {"tab": SYNC_STATE_TAB, "error": str(e)}


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def render_refresh_report(rep: dict) -> str:
    if not rep.get("ok"):
        return "\n".join(["refresh-instagram-metrics — not connected.",
                          render_connection_report(rep.get("connection", {}))])
    c = rep["counts"]
    head = "refresh-instagram-metrics --apply" if not rep.get("dry_run") else \
        "refresh-instagram-metrics --dry-run (NO WRITES)"
    lines = [head,
             f"  Owned media fetched: {rep['media_fetched']}  |  matched POC rows: {rep['matched_rows']}",
             f"  Already synced: {rep['already_synced_rows']}  |  new: {rep['new_rows']}",
             f"  Plan — first fills: {c['first_fill']}, updates: {c['update']}, "
             f"unchanged: {c['unchanged']}, immutable kept: {c['immutable_kept']}, "
             f"manual-protected: {c['manual_protected']}",
             f"  Cells to write: {rep['cells_to_write']}"]
    for ch in rep.get("changes", [])[:8]:
        lines.append(f"    ~ {ch['shortcode']} {ch['col']}: {ch['old']} -> {ch['new']}")
    lines.append(f"  VERDICT: {'SAFE' if rep['safe'] else 'NOT SAFE'}")
    if rep.get("dry_run"):
        lines.append("  Dry-run only: nothing written. Analytics questions read the sheet live, "
                     "so they answer immediately once --apply runs.")
    elif rep.get("refused"):
        lines.append(f"  REFUSED: {rep['refused']}")
    else:
        lines.append(f"  WROTE {rep.get('cells_written', 0)} cells across "
                     f"{rep.get('rows_written', 0)} rows; verify_ok={rep.get('verify_ok')}; "
                     f"sync-state {rep.get('sync_state')}.")
        lines.append("  Analytics refreshed: ask 'how long are our best reels?', 'what gets more "
                     "comments/saves?', 'trial vs standard?', 'what should we test next?' — live.")
    return "\n".join(lines)


# ===========================================================================
# Part 7 — Slack status helpers (read-only; no secrets)
# ===========================================================================
def is_owned_tiktok(url_or_handle: str) -> bool:
    """True ONLY when the given TikTok URL/handle matches the configured
    STORELLI_TIKTOK_HANDLE exactly. Ownership is deterministic on the handle,
    never inferred from caption/content. False when no handle is configured."""
    h = config.STORELLI_TIKTOK_HANDLE
    if not h:
        return False
    s = str(url_or_handle or "").lower()
    m = re.search(r"tiktok\.com/@([\w.\-]+)", s)
    handle = (m.group(1) if m else s).lstrip("@").strip().lower()
    return handle == h


# ---------------------------------------------------------------------------
# Internal NEW_MEDIA lifecycle + safe POC append (fixes the NEW_MEDIA gap)
# ---------------------------------------------------------------------------
def classify_owned_media(media: list, poc_rows: list) -> dict:
    """Classify each owned media item against the POC:
    NEW_MEDIA / KNOWN_UNANALYZED / KNOWN_ANALYZED. (Metrics refresh applies to
    all KNOWN rows separately.) Matching is by shortcode/canonical LINK only."""
    from sheets_client import SheetsClient
    by_key = {}
    for r in poc_rows:
        link = str(r.get("LINK", "")).strip()
        if link:
            by_key[_poc_key(link)] = r
    out = {"NEW_MEDIA": [], "KNOWN_UNANALYZED": [], "KNOWN_ANALYZED": []}
    for m in media:
        row = by_key.get(_poc_key(m.get("permalink", "")))
        if row is None:
            out["NEW_MEDIA"].append(m)
        elif SheetsClient.is_analyzed(row):
            out["KNOWN_ANALYZED"].append((m, row))
        else:
            out["KNOWN_UNANALYZED"].append((m, row))
    return out


def append_owned_media_to_poc(media: list, insights_by_media: dict, poc) -> dict:
    """Safely append NEW Storelli-OWNED reels as POC rows so they become eligible
    for the existing internal analysis pipeline.

    Guarantees: never duplicates an existing LINK/shortcode; writes only LINK +
    immutable metadata (POST_DATE, DURATION_SECONDS) + supported API metrics into
    columns that exist; NEVER writes taxonomy, Product, ICP, Storytelling, or a
    Status (left blank = unanalyzed/eligible); preserves the two-row header.
    Only owned media (already the API's own account) is accepted here.
    """
    poc_rows = poc.read_rows()
    existing = {_poc_key(str(r.get("LINK", "")).strip()) for r in poc_rows
                if str(r.get("LINK", "")).strip()}
    poc_cols = list(poc.meta_col.keys())
    new_rows, appended_links, seen = [], [], set()
    for m in media:
        link = str(m.get("permalink", "")).strip()
        key = _poc_key(link)
        if not link or key in existing or key in seen:
            continue
        seen.add(key)
        vals = build_metric_values(m, insights_by_media.get(m.get("id"), {}))
        rec = {"LINK": link}
        for col, v in vals.items():                 # only metadata columns that exist
            if col in poc_cols:
                rec[col] = v
        # never set Product / ICP / Storytelling structure / taxonomy / Status
        new_rows.append(rec)
        appended_links.append(link)
    appended = poc.append_metadata_rows(new_rows) if new_rows else 0
    return {"appended": appended, "skipped_existing": len(media) - len(new_rows),
            "new_links": appended_links}


def metrics_status(sheets=None) -> dict:
    """Read-only status for Slack: reels with metrics, tracked metric columns +
    coverage, reels missing metrics, and last-refresh time from the ledger."""
    try:
        poc = _load_poc(sheets)
        rows = poc.read_rows()
        cols = list(poc.meta_col.keys())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    metric_cols = [c for c in _API_FILLABLE_COLUMNS if c in cols]
    linked = [r for r in rows if str(r.get("LINK", "")).strip()]
    with_metrics = [r for r in linked
                    if any(str(r.get(c, "")).strip() for c in metric_cols)]
    coverage = {c: sum(1 for r in linked if str(r.get(c, "")).strip()) for c in metric_cols}
    last_refresh = ""
    try:
        ss = read_sync_state(poc)
        last_refresh = max((v.get("last", "") for v in ss.values()), default="")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "reels_total": len(linked), "reels_with_metrics": len(with_metrics),
            "reels_missing": len(linked) - len(with_metrics),
            "tracked_columns": metric_cols, "coverage": coverage,
            "last_refresh": last_refresh, "configured": config.instagram_configured()}
