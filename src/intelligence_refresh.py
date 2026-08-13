"""Self-Updating Social Intelligence — master refresh orchestrator.

Wraps the EXISTING jobs (never duplicates them) into two isolated, bounded loops
that keep the Marketing Brain's evidence current with almost no human trigger:

  INTERNAL (Storelli = PROOF):
    Fetch owned IG metrics -> detect new -> analyze -> performance -> correlate
    -> latest_learnings -> winning profiles -> Notion sync
  EXTERNAL (inspiration = reference only):
    Select queries -> discover (Apify) -> dedupe -> analyze -> match -> quality
    -> semantic connections

Invariant preserved everywhere: internal Storelli content is proof; external
content is execution reference only. The two loops never mix evidence semantics.

Design: every stage returns a structured status and fails INDEPENDENTLY (one bad
external video, a missing Apify token, an exhausted Gemini quota, or a Notion
outage must not kill the rest). A run lock (in the run-log tab) prevents a
scheduled run and a dashboard run from corrupting each other. Dry-run writes
nothing and estimates load. Correlations/profiles/Notion rebuild ONLY when
relevant internal evidence actually changed. Ideas are NEVER auto-regenerated —
the orchestrator only computes a recommendation.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import config
from logger import get_logger

log = get_logger()

RUNS_TAB = "INTELLIGENCE_REFRESH_RUNS"
RUNS_COLUMNS = ("RUN_ID", "STARTED_AT", "FINISHED_AT", "TRIGGER", "MODE",
                "INTERNAL_NEW_MEDIA", "INTERNAL_ANALYZED", "METRICS_UPDATED",
                "CORRELATIONS_REBUILT", "PROFILES_UPDATED", "EXTERNAL_DISCOVERED",
                "EXTERNAL_ADDED", "EXTERNAL_ANALYZED", "EXTERNAL_QUALITY_80",
                "CONNECTIONS_CREATED", "CONNECTIONS_UPDATED", "IDEA_REGEN_RECOMMENDED",
                "FAILED_COUNT", "STATUS", "ERROR_SUMMARY")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _stage(name: str, status: str = "skipped", processed=0, created=0, updated=0,
           failed=0, reason: str = "", duration=0.0) -> dict:
    return {"stage": name, "status": status, "processed": processed, "created": created,
            "updated": updated, "failed": failed, "reason": reason,
            "duration_seconds": round(duration, 2)}


def _run_stage(name: str, fn) -> dict:
    """Run one stage fail-soft; normalize to the FULL structured status dict
    (every key present) so downstream rendering/history never KeyErrors,
    regardless of what a wrapper returned."""
    t0 = time.time()
    try:
        result = fn() or {}
    except Exception as e:  # noqa: BLE001 - a stage failure never kills the refresh
        log.warning("intelligence stage %s failed: %s", name, e)
        return _stage(name, "failed", reason=f"{type(e).__name__}: {e}",
                      duration=time.time() - t0)
    base = _stage(name)                       # all defaults present
    base.update(result)                       # wrapper values + any private _keys
    base["stage"] = result.get("stage", name)
    base.setdefault("status", "success")
    base["duration_seconds"] = round(time.time() - t0, 2)
    return base


# ---------------------------------------------------------------------------
# configuration gates (injectable for tests)
# ---------------------------------------------------------------------------
def _ig_configured() -> bool:
    return config.instagram_configured()


def _apify_configured() -> bool:
    return bool(config.APIFY_TOKEN)


def _load_sheets():
    from inspiration_sheets import InspirationSheets
    return InspirationSheets()


# ---------------------------------------------------------------------------
# lock + run history (in the run-log tab; survives restarts / cross-process)
# ---------------------------------------------------------------------------
def _runs_ws(sheets):
    import gspread
    sh = sheets.ws.spreadsheet if hasattr(sheets, "ws") else sheets._ws(RUNS_TAB).spreadsheet
    try:
        return sh.worksheet(RUNS_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=RUNS_TAB, rows=1000, cols=len(RUNS_COLUMNS))
        ws.update(range_name="A1", values=[list(RUNS_COLUMNS)], value_input_option="RAW")
        return ws


def _read_runs(sheets) -> list:
    try:
        vals = _runs_ws(sheets).get_all_values()
    except Exception as e:  # noqa: BLE001
        log.warning("intelligence run-log read failed: %s", e)
        return []
    if not vals or len(vals) < 2:
        return []
    hdr = [h.strip() for h in vals[0]]
    return [dict(zip(hdr, r)) for r in vals[1:] if any(str(c).strip() for c in r)]


def _lock_active(sheets) -> Optional[dict]:
    """Return the active (running, non-stale) run row, or None."""
    stale = config.INTELLIGENCE_REFRESH_STALE_LOCK_MIN * 60
    for r in reversed(_read_runs(sheets)):
        if str(r.get("STATUS", "")).strip().lower() != "running":
            continue
        started = r.get("STARTED_AT", "")
        try:
            ts = datetime.strptime(started, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except (ValueError, TypeError):
            age = 0
        if age < stale:
            return r
        log.info("intelligence: clearing stale lock %s (age %ds)", r.get("RUN_ID"), int(age))
    return None


def _write_run_row(sheets, row: dict, update_run_id: Optional[str] = None) -> None:
    ws = _runs_ws(sheets)
    values = [str(row.get(h, "")) for h in RUNS_COLUMNS]
    if update_run_id:
        cells = ws.get_all_values()
        for i, r in enumerate(cells[1:], start=2):
            if r and r[0].strip() == update_run_id:
                ws.update(range_name=f"A{i}", values=[values], value_input_option="RAW")
                return
    ws.append_row(values, value_input_option="RAW")


# ---------------------------------------------------------------------------
# job wrappers (call the EXISTING implementations; injectable for tests)
# ---------------------------------------------------------------------------
def _internal_append(dry_run: bool) -> dict:
    """Detect NEW Storelli-owned reels and safely append them to the POC so they
    become eligible for the existing analysis pipeline. Owned media only."""
    if not _ig_configured():
        return _stage("internal_append", "skipped", reason="Instagram API not configured")
    from instagram_insights_client import InstagramInsightsClient, normalize_media
    from sheets_client import SheetsClient
    import social_metrics_ingest as smi
    client = InstagramInsightsClient()
    media = [normalize_media(m) for m in client.fetch_media(max_items=500)]
    poc = SheetsClient()
    poc.validate_columns()
    new = smi.classify_owned_media(media, poc.read_rows())["NEW_MEDIA"]
    if dry_run:
        return {"stage": "internal_append", "status": "success", "processed": len(media),
                "created": len(new), "_new_media": len(new),
                "reason": f"{len(new)} new owned reel(s) would be appended to POC (no writes)"}
    if not new:
        return {"stage": "internal_append", "status": "skipped", "processed": len(media),
                "reason": "no new owned reels", "_appended": 0, "_new_media": 0}
    insights = {}
    for m in new:
        try:
            insights[m["id"]] = client.fetch_media_insights(m["id"], m.get("media_product_type", ""))
        except Exception:  # noqa: BLE001
            insights[m["id"]] = {}
    res = smi.append_owned_media_to_poc(new, insights, poc)
    return {"stage": "internal_append", "status": "success", "processed": len(media),
            "created": res["appended"], "_appended": res["appended"],
            "_new_media": res["appended"],
            "reason": f"{res['appended']} new owned reel(s) appended to POC"}


def _internal_metrics(dry_run: bool) -> dict:
    if not _ig_configured():
        return _stage("internal_metrics", "skipped", reason="Instagram API not configured")
    import social_metrics_ingest as smi
    rep = smi.refresh_instagram_metrics(dry_run=dry_run, apply=not dry_run)
    if not rep.get("ok"):
        return _stage("internal_metrics", "skipped", reason=rep.get("error", "not connected"))
    return {"stage": "internal_metrics", "status": "success",
            "processed": rep.get("matched_rows", 0),
            "updated": rep.get("cells_written", rep.get("cells_to_write", 0)),
            "created": rep.get("new_rows", 0),
            "reason": f"{rep.get('media_fetched', 0)} owned media; "
                      f"{rep.get('media_not_in_poc', 0)} not in POC (new candidates)",
            "_new_media": rep.get("media_not_in_poc", 0)}


def _internal_analyze(dry_run: bool, limit: Optional[int]) -> dict:
    from sheets_client import SheetsClient
    sheets = SheetsClient()
    sheets.validate_columns()
    rows = sheets.read_rows()
    eligible = [r for r in rows if SheetsClient.should_process(r, False)]
    if dry_run:
        return _stage("internal_analyze", "success", processed=len(eligible),
                      reason=f"{len(eligible)} eligible row(s) would be analyzed (no writes)")
    if not eligible:
        return _stage("internal_analyze", "skipped", reason="no eligible internal rows")
    from main import cmd_analyze
    stats = cmd_analyze(reprocess=False, limit=limit, qa_enabled=config.QA_COMPILER_ENABLED)
    status = "partial" if stats.get("quota_stopped") or stats.get("failed") else "success"
    return {"stage": "internal_analyze", "status": status,
            "processed": stats.get("eligible", 0), "created": stats.get("analyzed", 0),
            "updated": stats.get("needs_review", 0), "failed": stats.get("failed", 0),
            "reason": ("Gemini quota stop — remaining rows kept for next run"
                       if stats.get("quota_stopped") else ""),
            "_analyzed": stats.get("analyzed", 0)}


def _internal_recompute(dry_run: bool) -> dict:
    """Correlations -> latest_learnings -> winning profiles -> Notion sync."""
    if dry_run:
        return _stage("internal_recompute", "success",
                      reason="would recompute correlations + learnings + profiles + Notion")
    from datetime import datetime as _dt, timezone as _tz
    import synthesizer
    import winning_profiles
    from main import compute_findings
    from sheets_client import SheetsClient
    sheets = SheetsClient()
    sheets.validate_columns()
    analyzed, buckets, results = compute_findings(sheets)
    profiles_created = profiles_updated = 0
    corr_rebuilt = False
    if analyzed:
        ts = _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
        synthesizer.write_learnings(analyzed, buckets, results, ts)
        corr_rebuilt = True
        prof_run = winning_profiles.build_winning_profiles()
        profiles_created = prof_run.get("POSTS_ADDED", 0)
        profiles_updated = prof_run.get("POSTS_ANALYZED", 0)
    notion_reason = ""
    try:
        from main import notion_sync
        notion_sync()
        notion_reason = "Notion synced"
    except Exception as e:  # noqa: BLE001 - Notion failure never rolls back evidence
        notion_reason = f"Notion sync skipped: {type(e).__name__}"
    return {"stage": "internal_recompute", "status": "success",
            "processed": len(analyzed), "created": profiles_created, "updated": profiles_updated,
            "reason": notion_reason, "_correlations_rebuilt": corr_rebuilt,
            "_profiles_created": profiles_created, "_profiles_updated": profiles_updated}


def _external_discovery(dry_run: bool, sheets) -> dict:
    if not _apify_configured():
        return _stage("external_discovery", "skipped", reason="APIFY_TOKEN not configured")
    import query_economics as qe
    active = sheets.read_active_queries()
    sel = qe.select_active(active, config.INTELLIGENCE_MAX_ACTIVE_QUERIES)
    if dry_run:
        n = len(sel["selected"])
        est = n * config.APIFY_DEFAULT_MAX_RESULTS
        return _stage("external_discovery", "success", processed=n,
                      reason=f"{n} on-domain query(ies) selected; ~{est} max results "
                             f"({len(sel['paused'])} paused, {len(sel['review'])} to review)")
    import inspiration_discovery
    run = inspiration_discovery.discover_inspiration(sheets=sheets)
    return {"stage": "external_discovery", "status": run.get("STATUS", "success").lower(),
            "processed": run.get("POSTS_DISCOVERED", 0), "created": run.get("POSTS_ADDED", 0),
            "updated": run.get("POSTS_SKIPPED_EXISTING", 0),
            "failed": run.get("CHANNELS_FAILED", 0),
            "_added": run.get("POSTS_ADDED", 0)}


def _external_analyze(dry_run: bool, sheets) -> dict:
    if dry_run:
        return _stage("external_analyze", "success",
                      reason="would tag any new/pending external rows")
    import inspiration_analyzer
    run = inspiration_analyzer.analyze_inspiration(sheets=sheets)
    return {"stage": "external_analyze", "status": run.get("STATUS", "success").lower(),
            "processed": run.get("POSTS_DISCOVERED", 0), "created": run.get("POSTS_ANALYZED", 0),
            "failed": run.get("POSTS_FAILED", 0)}


def _external_match(dry_run: bool, sheets) -> dict:
    if dry_run:
        return _stage("external_match", "success", reason="would match safe/analyzed rows")
    import inspiration_matcher
    run = inspiration_matcher.match_inspiration(sheets=sheets)
    return {"stage": "external_match", "status": run.get("STATUS", "success").lower(),
            "processed": run.get("POSTS_DISCOVERED", 0), "updated": run.get("POSTS_ANALYZED", 0),
            "created": run.get("POSTS_SHORTLISTED", 0)}


def _external_quality(dry_run: bool, sheets) -> dict:
    if dry_run:
        return _stage("external_quality", "success", reason="would QC review candidates")
    import inspiration_quality
    run = inspiration_quality.quality_review_inspiration(sheets=sheets)
    return {"stage": "external_quality", "status": run.get("STATUS", "success").lower(),
            "processed": run.get("POSTS_DISCOVERED", 0),
            "created": run.get("POSTS_SHORTLISTED", 0),
            "_quality_80": run.get("POSTS_SHORTLISTED", 0)}


def _external_connections(dry_run: bool, sheets) -> dict:
    if dry_run:
        return _stage("external_connections", "success",
                      reason="would rebuild connections IF new quality inspiration was added")
    import semantic_connections
    run = semantic_connections.build_semantic_connections(sheets=sheets)
    return {"stage": "external_connections", "status": run.get("STATUS", "success").lower(),
            "processed": run.get("POSTS_DISCOVERED", 0), "created": run.get("POSTS_ADDED", 0),
            "updated": run.get("POSTS_ANALYZED", 0),
            "_created": run.get("POSTS_ADDED", 0)}


# ---------------------------------------------------------------------------
# loops
# ---------------------------------------------------------------------------
def _internal_loop(dry_run: bool, limit: Optional[int]) -> list:
    stages = []
    # 1) detect + append NEW owned reels first, so metrics + analysis include them.
    append = _run_stage("internal_append", lambda: _internal_append(dry_run))
    stages.append(append)
    metrics = _run_stage("internal_metrics", lambda: _internal_metrics(dry_run))
    stages.append(metrics)
    analyze = _run_stage("internal_analyze", lambda: _internal_analyze(dry_run, limit))
    stages.append(analyze)
    # Recompute ONLY when internal evidence actually changed.
    changed = (analyze.get("_analyzed", analyze.get("created", 0)) > 0
               or metrics.get("updated", 0) > 0
               or append.get("_appended", 0) > 0)
    if dry_run:
        stages.append(_run_stage("internal_recompute", lambda: _internal_recompute(True)))
    elif changed:
        stages.append(_run_stage("internal_recompute", lambda: _internal_recompute(False)))
    else:
        stages.append(_stage("internal_recompute", "skipped",
                             reason="no new analyzed rows or metric change — nothing to rebuild"))
    return stages


def _external_loop(dry_run: bool, sheets) -> list:
    stages = []
    disc = _run_stage("external_discovery", lambda: _external_discovery(dry_run, sheets))
    stages.append(disc)
    added = disc.get("_added", disc.get("created", 0))
    # analyze/match/quality process only pending rows (idempotent) — safe to run,
    # but skip cleanly when discovery is unconfigured/failed and nothing is new.
    if disc["status"] == "skipped" and not dry_run:
        for s in ("external_analyze", "external_match", "external_quality",
                  "external_connections"):
            stages.append(_stage(s, "skipped", reason="discovery unavailable"))
        return stages
    stages.append(_run_stage("external_analyze", lambda: _external_analyze(dry_run, sheets)))
    stages.append(_run_stage("external_match", lambda: _external_match(dry_run, sheets)))
    quality = _run_stage("external_quality", lambda: _external_quality(dry_run, sheets))
    stages.append(quality)
    new_quality = quality.get("_quality_80", quality.get("created", 0))
    # connections rebuild only if new quality inspiration was added (or dry-run).
    if dry_run or added > 0 or new_quality > 0:
        stages.append(_run_stage("external_connections",
                                 lambda: _external_connections(dry_run, sheets)))
    else:
        stages.append(_stage("external_connections", "skipped",
                             reason="no new quality inspiration — connections unchanged"))
    return stages


def _should_regenerate_ideas(stages: list) -> tuple:
    """True only on a material evidence shift (new profile / new connection / new
    high-quality reference cluster). Report only — never auto-regenerates."""
    by = {s["stage"]: s for s in stages}
    reasons = []
    if by.get("internal_recompute", {}).get("_profiles_created", 0) > 0:
        reasons.append("new internal winning profile")
    if by.get("external_connections", {}).get("_created", 0) > 0:
        reasons.append("new semantic connection")
    if by.get("external_quality", {}).get("_quality_80", 0) > 0:
        reasons.append("stronger external reference pool")
    return (bool(reasons), reasons)


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------
def run_intelligence_refresh(mode: str = "full", dry_run: bool = False,
                             trigger: str = "manual", limit: Optional[int] = None,
                             sheets=None) -> dict:
    """Master refresh. mode in {full, internal, external}. Read-only when
    dry_run. Fail-soft, resumable, lock-protected."""
    t0 = time.time()
    run_id = "IR-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    started = _now_iso()
    report = {"run_id": run_id, "mode": mode, "trigger": trigger, "dry_run": dry_run,
              "started_at": started, "stages": [], "locked_out": False}

    try:
        sheets = sheets or _load_sheets()
    except Exception as e:  # noqa: BLE001
        report["stages"].append(_stage("init", "failed", reason=f"sheets unavailable: {e}"))
        report["status"] = "failed"
        return report

    # ---- lock (skip for dry-run: read-only, never mutates) ----
    if not dry_run:
        active = _lock_active(sheets)
        if active:
            report["locked_out"] = True
            report["status"] = "skipped"
            report["reason"] = f"another refresh is running ({active.get('RUN_ID')}) — exiting cleanly"
            return report
        try:
            _write_run_row(sheets, {"RUN_ID": run_id, "STARTED_AT": started,
                                    "TRIGGER": trigger, "MODE": mode, "STATUS": "running"})
        except Exception as e:  # noqa: BLE001
            log.warning("intelligence: could not write lock row: %s", e)

    stages = []
    if mode in ("full", "internal"):
        stages += _internal_loop(dry_run, limit)
    if mode in ("full", "external"):
        stages += _external_loop(dry_run, sheets)
    report["stages"] = stages

    regen, regen_reasons = _should_regenerate_ideas(stages)
    report["should_regenerate_ideas"] = regen
    report["idea_regen_reasons"] = regen_reasons

    failed = sum(1 for s in stages if s["status"] == "failed")
    partial = any(s["status"] == "partial" for s in stages)
    report["failed_count"] = failed
    report["status"] = "failed" if failed and all(
        s["status"] in ("failed", "skipped") for s in stages) else (
        "partial" if (failed or partial) else "success")
    report["duration_seconds"] = round(time.time() - t0, 2)
    report["finished_at"] = _now_iso()

    if not dry_run:
        try:
            _write_run_row(sheets, _history_row(report), update_run_id=run_id)
        except Exception as e:  # noqa: BLE001
            log.warning("intelligence: run-history write failed: %s", e)
    return report


def _history_row(report: dict) -> dict:
    by = {s["stage"]: s for s in report["stages"]}
    errs = "; ".join(f"{s['stage']}:{s['reason'][:40]}" for s in report["stages"]
                     if s["status"] == "failed")[:400]
    return {
        "RUN_ID": report["run_id"], "STARTED_AT": report["started_at"],
        "FINISHED_AT": report.get("finished_at", ""), "TRIGGER": report["trigger"],
        "MODE": report["mode"],
        "INTERNAL_NEW_MEDIA": by.get("internal_append", {}).get(
            "_new_media", by.get("internal_metrics", {}).get("_new_media", 0)),
        "INTERNAL_ANALYZED": by.get("internal_analyze", {}).get("created", 0),
        "METRICS_UPDATED": by.get("internal_metrics", {}).get("updated", 0),
        "CORRELATIONS_REBUILT": by.get("internal_recompute", {}).get("_correlations_rebuilt", False),
        "PROFILES_UPDATED": by.get("internal_recompute", {}).get("_profiles_updated", 0),
        "EXTERNAL_DISCOVERED": by.get("external_discovery", {}).get("processed", 0),
        "EXTERNAL_ADDED": by.get("external_discovery", {}).get("created", 0),
        "EXTERNAL_ANALYZED": by.get("external_analyze", {}).get("created", 0),
        "EXTERNAL_QUALITY_80": by.get("external_quality", {}).get("_quality_80", 0),
        "CONNECTIONS_CREATED": by.get("external_connections", {}).get("created", 0),
        "CONNECTIONS_UPDATED": by.get("external_connections", {}).get("updated", 0),
        "IDEA_REGEN_RECOMMENDED": report.get("should_regenerate_ideas", False),
        "FAILED_COUNT": report.get("failed_count", 0), "STATUS": report["status"],
        "ERROR_SUMMARY": errs,
    }


# ---------------------------------------------------------------------------
# rendering + Slack helpers
# ---------------------------------------------------------------------------
def render_report(report: dict) -> str:
    head = ("refresh-intelligence --dry-run (NO WRITES)" if report.get("dry_run")
            else f"refresh-intelligence [{report['mode']}]")
    lines = [head, f"  run: {report['run_id']}  ·  trigger: {report['trigger']}"]
    if report.get("locked_out"):
        return f"{head}\n  {report['reason']}"
    for s in report["stages"]:
        lines.append(f"  [{s['status']:8}] {s['stage']:22} "
                     f"processed={s['processed']} created={s['created']} updated={s['updated']} "
                     f"failed={s['failed']}" + (f"  — {s['reason']}" if s["reason"] else ""))
    lines.append(f"  should_regenerate_ideas: {report.get('should_regenerate_ideas')}"
                 + (f" ({', '.join(report['idea_regen_reasons'])})"
                    if report.get("idea_regen_reasons") else ""))
    lines.append(f"  STATUS: {report['status'].upper()}  ·  "
                 f"{report.get('duration_seconds', 0)}s  ·  failed_stages={report.get('failed_count', 0)}")
    if report.get("dry_run"):
        lines.append("  Dry-run only: nothing written.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# readiness (Part 6/7) + health (Part 13) — never prints secrets
# ---------------------------------------------------------------------------
_COOKIE_REFRESH_MSG = "Instagram video acquisition credentials need refresh"


def _cookies_configured() -> bool:
    return bool(config.YTDLP_COOKIES_PATH)


def refresh_readiness() -> dict:
    """READY / BLOCKED per capability with the exact env var required. Reads only
    presence flags — never the secret values."""
    caps = {}

    def cap(name, ok, need):
        caps[name] = {"status": "READY" if ok else "BLOCKED", "required": "" if ok else need}

    ig = config.instagram_configured()
    sheets_ok = bool(config.GOOGLE_SHEET_ID and config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH)
    cap("instagram_owned_discovery", ig,
        "INSTAGRAM_ACCESS_TOKEN (or META_ACCESS_TOKEN) + INSTAGRAM_BUSINESS_ACCOUNT_ID (or IG_USER_ID)")
    cap("instagram_metrics", ig, "same as instagram_owned_discovery")
    cap("internal_video_analysis", bool(config.GEMINI_API_KEY) and _cookies_configured(),
        ("GEMINI_API_KEY" if not config.GEMINI_API_KEY else "")
        + ("" if config.GEMINI_API_KEY or _cookies_configured() else " + ")
        + ("YTDLP_COOKIES_B64 (authenticated Instagram cookies — one-time export)"
           if not _cookies_configured() else ""))
    cap("external_apify_discovery", bool(config.APIFY_TOKEN), "APIFY_TOKEN")
    cap("gemini_analysis", bool(config.GEMINI_API_KEY), "GEMINI_API_KEY")
    cap("sheets", sheets_ok, "GOOGLE_SHEET_ID + GOOGLE_SERVICE_ACCOUNT_JSON_PATH (or _B64)")
    cap("notion_sync", bool(config.NOTION_API_KEY and config.NOTION_PARENT_PAGE_ID),
        "NOTION_API_KEY + NOTION_PARENT_PAGE_ID")
    cap("run_secret", bool(config.RUN_SECRET), "RUN_SECRET (dashboard-triggered refresh)")
    caps["owned_tiktok"] = {"status": "READY" if config.owned_tiktok_configured() else "NOT_CONFIGURED",
                            "required": "STORELLI_TIKTOK_HANDLE (owned-TikTok metrics are limited — "
                                        "no official API integration)"}
    return caps


def _seconds_since(iso: str) -> Optional[float]:
    try:
        ts = datetime.strptime(iso, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return None


def health_state(sheets=None) -> dict:
    """HEALTHY / PARTIAL / BLOCKED / STALE with the specific reasons to act on."""
    caps = refresh_readiness()
    reasons = []
    if caps["sheets"]["status"] != "READY" or caps["gemini_analysis"]["status"] != "READY":
        return {"state": "BLOCKED", "reasons": ["core services down (Sheets/Gemini)"],
                "readiness": caps, "last_run": None}
    if caps["internal_video_analysis"]["status"] != "READY" and not _cookies_configured():
        reasons.append(_COOKIE_REFRESH_MSG)
    if caps["instagram_metrics"]["status"] != "READY":
        reasons.append("Instagram metrics not configured")
    if caps["external_apify_discovery"]["status"] != "READY":
        reasons.append("Apify discovery not configured")

    runs = last_runs(sheets, n=1)
    last = runs[0] if runs else None
    cadence = config.INTELLIGENCE_REFRESH_CADENCE_DAYS + config.INTELLIGENCE_STALE_TOLERANCE_DAYS
    if not last:
        return {"state": "STALE", "reasons": ["no successful refresh recorded yet"] + reasons,
                "readiness": caps, "last_run": None}
    age = _seconds_since(last.get("FINISHED_AT") or last.get("STARTED_AT", ""))
    if age is not None and age > cadence * 86400:
        return {"state": "STALE", "reasons": [f"last refresh {int(age // 86400)}d ago "
                                              f"(> {cadence}d)"] + reasons,
                "readiness": caps, "last_run": last}
    if str(last.get("STATUS", "")).lower() in ("failed", "partial") or reasons:
        return {"state": "PARTIAL", "reasons": (reasons or [f"last run {last.get('STATUS')}"]),
                "readiness": caps, "last_run": last}
    return {"state": "HEALTHY", "reasons": [], "readiness": caps, "last_run": last}


def next_scheduled_note() -> str:
    return (f"scheduled every {config.INTELLIGENCE_REFRESH_CADENCE_DAYS} days once a Railway Cron "
            "invokes `refresh-intelligence` (not enabled until that cron exists)")


def render_readiness(caps: dict) -> str:
    lines = ["Intelligence refresh readiness (no secrets shown):"]
    order = ["instagram_owned_discovery", "instagram_metrics", "internal_video_analysis",
             "external_apify_discovery", "gemini_analysis", "sheets", "notion_sync",
             "run_secret", "owned_tiktok"]
    for k in order:
        c = caps[k]
        line = f"  {k:26} {c['status']}"
        if c.get("required") and c["status"] != "READY":
            line += f"   -> needs {c['required']}"
        lines.append(line)
    return "\n".join(lines)


def last_runs(sheets=None, n: int = 5) -> list:
    try:
        sheets = sheets or _load_sheets()
        return list(reversed(_read_runs(sheets)))[:n]
    except Exception as e:  # noqa: BLE001
        log.warning("intelligence last_runs failed: %s", e)
        return []
