"""Internal dashboard + control panel for the Storelli intelligence MVP.

Stupid-simple FastAPI app (no framework, no DB). Wraps the CLI functions behind
buttons. One run at a time; state kept in-memory (resets on restart). Errors are
captured into STATE.error and shown in the UI rather than crashing.

Endpoints:
  GET  /                 dashboard (HTML)
  GET  /status           current run state (JSON)
  GET  /learnings        data/latest_learnings.md (JSON: {exists, content})
  POST /run/social       analyze with {limit, qa} (background; RUN_SECRET)
  POST /run/correlations recompute correlations (background; RUN_SECRET)
  POST /run/synthesize   regenerate latest_learnings.md (background; RUN_SECRET)
"""
from __future__ import annotations

import os
import re
import secrets as stdlib_secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import correlations as corr
from logger import get_logger

log = get_logger()

app = FastAPI(title="Storelli Marketing Brain")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

_LEARNINGS = os.path.join(os.path.dirname(__file__), "..", "data", "latest_learnings.md")
_GUIDELINES_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "guidelines")

GUIDELINE_TYPES = [
    "Social Content Guidelines", "Email Guidelines", "Ads Guidelines",
    "Brand Voice Guidelines", "Product Messaging Guidelines",
]


def _slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")

STATE: dict = {
    "status": "idle",        # idle | queued | running | completed | failed
    "action": "",            # social | correlations | synthesize
    "limit": None,
    "qa": True,
    "started_at": None,
    "finished_at": None,
    "stats": {},
    "top_winning": [],
    "top_weak": [],
    "learnings_ready": os.path.exists(_LEARNINGS),
    "notion": "not_run",     # not_run | synced | failed | not_configured
    "notion_summary": {},
    "notion_url": config.NOTION_DASHBOARD_URL,
    "slack": "not_run",      # not_run | posted | failed
    "inspiration": {},       # last inspiration scan run summary (isolated layer)
    "error": "",
}
_LOCK = threading.Lock()


# ---- helpers ---------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(r: dict) -> dict:
    return {"signal": r["signal"], "layer": r["layer"], "label": r["label"],
            "lift": corr.fmt_lift(r["lift"]), "n": r["videos_with_signal"],
            "confidence": r["confidence"]}


def _check_secret(provided: Optional[str]) -> None:
    expected = config.RUN_SECRET
    if not expected:
        raise HTTPException(503, "RUN_SECRET not configured on the server")
    if not provided or not stdlib_secrets.compare_digest(provided, expected):
        raise HTTPException(401, "invalid run secret")


def _parse_limit(value) -> Optional[int]:
    if value in (None, "", "all", "All", "ALL"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "limit must be an integer or 'all'")
    if n <= 0:
        raise HTTPException(400, "limit must be > 0")
    return n


def _begin(action: str, **fields) -> None:
    with _LOCK:
        STATE.update(status="running", action=action, started_at=_now(),
                     finished_at=None, error="", **fields)


def _finish(**fields) -> None:
    with _LOCK:
        STATE.update(status="completed", finished_at=_now(), **fields)


def _fail(msg: str) -> None:
    log.exception("dashboard action failed")
    with _LOCK:
        STATE.update(status="failed", finished_at=_now(), error=msg)


# ---- background workers ----------------------------------------------------
def _refresh_correlations() -> None:
    from main import compute_findings
    from sheets_client import SheetsClient
    sheets = SheetsClient()
    sheets.validate_columns()
    _analyzed, _buckets, results = compute_findings(sheets)
    with _LOCK:
        STATE["top_winning"] = [_short(x) for x in corr.winning(results)[:5]]
        STATE["top_weak"] = [_short(x) for x in corr.weak(results)[:5]]


def _maybe_slack(videos_analyzed) -> None:
    """Best-effort Slack post after a run; never fails the run."""
    if not config.SLACK_WEBHOOK_URL:
        return
    try:
        from main import gather_slack_inputs
        import slack_report
        data = gather_slack_inputs(videos_analyzed=videos_analyzed,
                                   notion_updated=(STATE.get("notion") == "synced"))
        slack_report.post(slack_report.build_message(**data))
        with _LOCK:
            STATE["slack"] = "posted"
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            STATE["slack"] = "failed"
        log.warning("auto slack post failed: %s", e)


def _do_social(limit: Optional[int], qa: bool) -> None:
    from main import cmd_analyze
    try:
        _begin("social", limit=limit, qa=qa)
        stats = cmd_analyze(reprocess=False, limit=limit, qa_enabled=qa)
        with _LOCK:
            STATE["stats"] = stats
        try:
            _refresh_correlations()
        except Exception as e:  # noqa: BLE001 - correlations are best-effort here
            log.warning("post-analyze correlations refresh failed: %s", e)
        _maybe_slack(stats.get("analyzed"))
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_analyze_all(limit: Optional[int], qa: bool) -> None:
    """Full-sheet taxonomy tagging. Tags every LINK regardless of PERFORMANCE.
    Does NOT run correlations, synthesize, Notion, or Slack automatically —
    those stay behind the existing 'Run Social Media Learning' path."""
    from main import cmd_analyze_all
    try:
        _begin("analyze-all", limit=limit, qa=qa)
        stats = cmd_analyze_all(reprocess=False, limit=limit, qa_enabled=qa)
        with _LOCK:
            STATE["stats"] = stats
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_scan_inspiration() -> None:
    """Inspiration Layer ingestion. Reads ACTIVE monitored channels and appends
    external post metadata to INSPIRATION_CONTENT. Structurally isolated from
    the internal learning pipeline — never touches correlations/synthesis/
    Notion/Slack, and writes to a different worksheet entirely."""
    import inspiration_scanner
    try:
        _begin("scan-inspiration")
        run = inspiration_scanner.scan_channels()
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_process_inspiration_queue() -> None:
    """Human-in-the-loop URL queue ingestion. Fetches metadata for each pending
    INSPIRATION_URL_QUEUE URL and appends it to INSPIRATION_CONTENT. Same
    isolation guarantees as the channel scan — separate worksheet, never touches
    correlations/synthesis/Notion/Slack."""
    import inspiration_scanner
    try:
        _begin("process-inspiration-queue")
        run = inspiration_scanner.process_queue()
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_discover_inspiration() -> None:
    """Apify Research + Discovery. Finds safe, high-signal external candidates
    and appends them to INSPIRATION_CONTENT. Isolated from internal Storelli
    learning — separate worksheet, external inspiration is never proof."""
    import inspiration_discovery
    try:
        _begin("discover-inspiration")
        run = inspiration_discovery.discover_inspiration()
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_build_winning_profiles() -> None:
    """Build Storelli Winning Format Profiles from internal evidence only.
    Reads the internal POC sheet + correlations; writes only to
    WINNING_FORMAT_PROFILES. External inspiration is never used as proof."""
    import winning_profiles
    try:
        _begin("build-winning-profiles")
        run = winning_profiles.build_winning_profiles()
        run.pop("_profiles", None)   # keep STATE JSON-clean
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_build_semantic_connections() -> None:
    """Build semantic connections (internal proof -> external inspiration via
    storytelling structure). Writes only to SEMANTIC_CONNECTIONS; internal
    Storelli rows untouched; external inspiration never proof."""
    import semantic_connections
    try:
        _begin("build-semantic-connections")
        run = semantic_connections.build_semantic_connections(
            products=["BodyShield GK Leggings", "Pants & Leggings", "Gloves"], max_concepts=10)
        for k in ("_connections", "_weak"):
            run.pop(k, None)
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_evaluate_notion_idea(url: str) -> None:
    """Evaluate a single pasted Notion idea page into ADHOC_IDEA_EVALUATIONS.
    Read-only w.r.t. Notion; never writes to the page, internal rows, profiles,
    or calendar ratings. External inspiration is reference only, never proof."""
    import adhoc_idea_evaluator
    try:
        _begin("evaluate-notion-idea")
        result = adhoc_idea_evaluator.evaluate_notion_url(url)
        if result.get("error"):
            _fail(result["error"])
            return
        with _LOCK:
            STATE["inspiration"] = {
                "RUN_TYPE": "AdHocIdeaEvaluation",
                "IDEA_TITLE": result.get("IDEA_TITLE"),
                "IDEA_EVALUATION_SCORE": result.get("IDEA_EVALUATION_SCORE"),
                "RECOMMENDATION": result.get("RECOMMENDATION"),
                "CONFIDENCE": result.get("CONFIDENCE"),
            }
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_rate_calendar_ideas() -> None:
    """Rate proposed Notion calendar ideas into CONTENT_CALENDAR_IDEA_RATINGS.
    Read-only w.r.t. Notion; never writes to the calendar, internal rows, or
    profiles. External inspiration is reference only, never proof."""
    import calendar_rater
    try:
        _begin("rate-calendar-ideas")
        run = calendar_rater.rate_calendar_ideas(limit=10)
        run.pop("_ratings", None)
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_refine_ideas() -> None:
    """Creative-director refinement of existing rated ideas. Writes only the
    refinement columns of INSPIRATION_IDEAS; preserves originals + sources;
    never touches internal rows, profiles, or scoring."""
    import idea_refiner
    try:
        _begin("refine-ideas")
        run = idea_refiner.refine_ideas()
        run.pop("_refinements", None)
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_generate_ideas() -> None:
    """Generate + rate Storelli creative ideas from internal profiles + safe
    high-quality external inspiration. Writes only to INSPIRATION_IDEAS; never
    touches internal rows or profiles. External inspiration is never proof."""
    import idea_generator
    try:
        _begin("generate-ideas")
        run = idea_generator.generate_ideas()
        run.pop("_ideas", None)
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_quality_review_inspiration() -> None:
    """Quality-control review of safe/analyzed external candidates. Writes only
    to INSPIRATION_CONTENT; never touches internal rows or profiles. Not idea
    generation/scoring."""
    import inspiration_quality
    try:
        _begin("quality-review-inspiration")
        run = inspiration_quality.quality_review_inspiration()
        run.pop("_full_video_count", None)
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_match_inspiration() -> None:
    """Match safe/analyzed external inspiration to active winning profiles and
    shortlist. Writes only to INSPIRATION_CONTENT; never modifies profiles or
    internal rows. Discovery priority is a secondary ranking signal only."""
    import inspiration_matcher
    try:
        _begin("match-inspiration")
        run = inspiration_matcher.match_inspiration()
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_analyze_inspiration() -> None:
    """Tag EXTERNAL_INSPIRATION rows in INSPIRATION_CONTENT with the creative
    taxonomy. Reads/writes only the inspiration tab — never touches internal
    Storelli rows, correlations, synthesis, Notion, or Slack. External tags are
    never Storelli proof."""
    import inspiration_analyzer
    try:
        _begin("analyze-inspiration")
        run = inspiration_analyzer.analyze_inspiration()
        with _LOCK:
            STATE["inspiration"] = run
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_notion_sync() -> None:
    try:
        _begin("notion-sync")
        from main import notion_sync
        summary = notion_sync()
        _finish(notion="synced", notion_summary=summary)
    except RuntimeError as e:
        # known clean prerequisite (not configured / no learnings) — show, don't crash
        with _LOCK:
            STATE.update(status="failed", finished_at=_now(),
                         notion="not_configured", error=str(e))
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            STATE["notion"] = "failed"
        _fail(str(e))


def _do_slack() -> None:
    try:
        _begin("slack-report")
        if not config.SLACK_WEBHOOK_URL:
            with _LOCK:
                STATE.update(status="failed", finished_at=_now(), slack="failed",
                             error="SLACK_WEBHOOK_URL not configured.")
            return
        from main import gather_slack_inputs
        import slack_report
        data = gather_slack_inputs(notion_updated=(STATE.get("notion") == "synced"))
        slack_report.post(slack_report.build_message(**data))
        _finish(slack="posted")
    except Exception as e:  # noqa: BLE001
        with _LOCK:
            STATE["slack"] = "failed"
        _fail(str(e))


def _do_correlations() -> None:
    try:
        _begin("correlations")
        _refresh_correlations()
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_generate_social_ideas() -> None:
    """Generate grounded social ideas from the current context and persist
    them (Notion if configured, else data/generated_social_ideas.jsonl).
    Never writes to the Sheet."""
    try:
        _begin("generate-social-ideas")
        from datetime import datetime, timezone

        import content_context
        import interpretation
        import notion_brain
        from main import compute_findings
        from sheets_client import SheetsClient

        sheets = SheetsClient()
        sheets.validate_columns()
        analyzed, _buckets, results = compute_findings(sheets)
        rows = sheets.read_rows()
        ctx = content_context.gather_context()
        ideas = interpretation.build_idea_candidates(
            question="", rows=rows, findings=results, context=ctx, limit=5)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        persist_summary = notion_brain.sync_or_persist_ideas(ideas, date_str)
        with _LOCK:
            STATE["generated_ideas"] = {
                "count": len(ideas),
                "target": persist_summary.get("target"),
                "note": persist_summary.get("note", ""),
                "path": persist_summary.get("path", ""),
            }
        _finish()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _do_synthesize() -> None:
    try:
        _begin("synthesize")
        from main import compute_findings
        from sheets_client import SheetsClient
        import synthesizer
        sheets = SheetsClient()
        sheets.validate_columns()
        analyzed, buckets, results = compute_findings(sheets)
        if not analyzed:
            _finish(learnings_ready=os.path.exists(_LEARNINGS),
                    error="No tagged rows yet — nothing to synthesize.")
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        synthesizer.write_learnings(analyzed, buckets, results, ts)
        _finish(learnings_ready=True)
    except Exception as e:  # noqa: BLE001
        _fail(str(e))


def _guarded(action_fn, background: BackgroundTasks) -> dict:
    with _LOCK:
        if STATE["status"] in ("queued", "running"):
            raise HTTPException(409, "a run is already in progress")
        STATE.update(status="queued", error="")
    background.add_task(action_fn)
    return {"status": "queued"}


# ---- request models --------------------------------------------------------
class SocialReq(BaseModel):
    limit: Optional[str] = None   # "5"|"18"|"25"|"50"|"150"|"all"
    qa: bool = True


# ---- routes ----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _HTML


@app.get("/status")
def status() -> JSONResponse:
    with _LOCK:
        return JSONResponse(dict(STATE))


@app.get("/learnings")
def learnings() -> JSONResponse:
    if not os.path.exists(_LEARNINGS):
        return JSONResponse({"exists": False, "content": "",
                             "error": "No learnings yet — run Generate Learnings."})
    try:
        with open(_LEARNINGS, encoding="utf-8") as f:
            return JSONResponse({"exists": True, "content": f.read(), "error": ""})
    except OSError as e:
        return JSONResponse({"exists": False, "content": "", "error": str(e)})


@app.post("/run/social", status_code=202)
def run_social(req: SocialReq, background: BackgroundTasks,
               x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    limit = _parse_limit(req.limit)
    qa = bool(req.qa)
    return _guarded(lambda: _do_social(limit, qa), background) | {"limit": limit, "qa": qa}


@app.post("/run/analyze-all", status_code=202)
def run_analyze_all(req: SocialReq, background: BackgroundTasks,
                    x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    limit = _parse_limit(req.limit)
    qa = bool(req.qa)
    return _guarded(lambda: _do_analyze_all(limit, qa), background) | {"limit": limit, "qa": qa}


@app.post("/run/correlations", status_code=202)
def run_correlations(background: BackgroundTasks,
                     x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_correlations, background)


@app.post("/run/synthesize", status_code=202)
def run_synthesize(background: BackgroundTasks,
                   x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_synthesize, background)


@app.post("/run/scan-inspiration", status_code=202)
def run_scan_inspiration(background: BackgroundTasks,
                         x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_scan_inspiration, background)


@app.post("/run/process-inspiration-queue", status_code=202)
def run_process_inspiration_queue(background: BackgroundTasks,
                                  x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_process_inspiration_queue, background)


@app.post("/run/analyze-inspiration", status_code=202)
def run_analyze_inspiration(background: BackgroundTasks,
                            x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_analyze_inspiration, background)


@app.post("/run/discover-inspiration", status_code=202)
def run_discover_inspiration(background: BackgroundTasks,
                             x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_discover_inspiration, background)


@app.post("/run/build-winning-profiles", status_code=202)
def run_build_winning_profiles(background: BackgroundTasks,
                               x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_build_winning_profiles, background)


@app.post("/run/match-inspiration", status_code=202)
def run_match_inspiration(background: BackgroundTasks,
                          x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_match_inspiration, background)


@app.post("/run/quality-review-inspiration", status_code=202)
def run_quality_review_inspiration(background: BackgroundTasks,
                                   x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_quality_review_inspiration, background)


@app.post("/run/generate-ideas", status_code=202)
def run_generate_ideas(background: BackgroundTasks,
                       x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_generate_ideas, background)


@app.post("/run/refine-ideas", status_code=202)
def run_refine_ideas(background: BackgroundTasks,
                     x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_refine_ideas, background)


@app.post("/run/rate-calendar-ideas", status_code=202)
def run_rate_calendar_ideas(background: BackgroundTasks,
                            x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_rate_calendar_ideas, background)


@app.post("/run/build-semantic-connections", status_code=202)
def run_build_semantic_connections(background: BackgroundTasks,
                                   x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_build_semantic_connections, background)


class EvaluateNotionReq(BaseModel):
    url: str


@app.post("/run/evaluate-notion-idea", status_code=202)
def run_evaluate_notion_idea(req: EvaluateNotionReq, background: BackgroundTasks,
                             x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    if not (req.url or "").strip():
        raise HTTPException(400, "url is required")
    return _guarded(lambda: _do_evaluate_notion_idea(req.url.strip()), background)


@app.post("/run/notion-sync", status_code=202)
def run_notion_sync(background: BackgroundTasks,
                    x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_notion_sync, background)


@app.post("/run/slack-report", status_code=202)
def run_slack_report(background: BackgroundTasks,
                     x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_slack, background)


@app.post("/run/generate-social-ideas", status_code=202)
def run_generate_social_ideas(background: BackgroundTasks,
                              x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    return _guarded(_do_generate_social_ideas, background)


# ---- Slack chat (app_mention / DM / active-thread reply) -------------------
# Conversational mode: each turn tries live Slack thread history first
# (conversations.replies — best-effort, needs channels:history/groups:history/
# im:history depending on channel type; returns None cleanly without those
# scopes), then falls back to the lightweight in-memory per-thread cache in
# slack_bot.py. Either way, read/synthesize only — never writes to the Sheet
# or Notion, never triggers video analysis.
#
# Progress UI: shows short, PUBLIC status stages while the real answer is
# composed — never private chain-of-thought. Uses slack_bot.ProgressReporter,
# which prefers Slack's native assistant.threads.setStatus and falls back to
# posting one message and editing it in place, so no duplicate "thinking"
# messages are ever left behind.
_STAGE_TEXT = {
    "notion": "🔎 Checking Notion Brain",
    "backend_context": "🔎 Checking backend context",
    "evidence": "🧩 Choosing strongest evidence",
    "writing": "✍️ Writing concise recommendation",
}


def _converse(channel: str, thread_ts: str, user_text: str, user_id: str = "") -> None:
    """Shared background worker for app_mention, DMs, and active-thread
    replies: build a grounded, thread-aware answer and post it back.

    Routes to Dev Brain (backend self-awareness / build-request handoff) when
    the message looks like a backend/build question, else to the marketing
    strategist — both are read-only with respect to the Sheet and Notion; the
    one exception (an explicitly-configured build-request GitHub handoff) is
    itself gated to approved user_id's inside dev_brain.py, never blind."""
    import dev_brain
    import slack_bot
    import social_brain
    progress = slack_bot.ProgressReporter(channel, thread_ts)
    try:
        progress.start("🧠 Thinking… reading Storelli context")
        clean = slack_bot.strip_mention(user_text)
        context = slack_bot.fetch_thread_context(channel, thread_ts)
        if context is None:
            context = slack_bot.cached_context(channel, thread_ts)

        def on_stage(stage: str) -> None:
            text = _STAGE_TEXT.get(stage)
            if text:
                progress.update(text)

        if dev_brain.is_dev_question(clean):
            answer = dev_brain.handle(clean, context, requesting_user_id=user_id, progress_cb=on_stage)
        else:
            answer = social_brain.answer_conversation(clean, context, progress_cb=on_stage)
        slack_bot.remember(channel, thread_ts, "user", clean)
        slack_bot.remember(channel, thread_ts, "assistant", answer)
        progress.finish(answer)
    except Exception as e:  # noqa: BLE001 - Slack never sees a stack trace
        log.exception("slack conversation handling failed: %s", e)
        try:
            progress.fail(str(type(e).__name__))
        except Exception:  # noqa: BLE001
            log.exception("slack error-reply also failed")


@app.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks):
    import json as _json

    import slack_bot

    if not (config.SLACK_BOT_TOKEN and config.SLACK_SIGNING_SECRET):
        raise HTTPException(503, "Slack bot not configured (SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET)")

    raw = await request.body()
    ts = request.headers.get("x-slack-request-timestamp", "")
    sig = request.headers.get("x-slack-signature", "")
    if not slack_bot.verify_request(raw, ts, sig):
        raise HTTPException(401, "invalid slack signature")

    try:
        payload = _json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        raise HTTPException(400, "invalid json")

    if payload.get("type") == "url_verification":
        return PlainTextResponse(payload.get("challenge", ""))

    if slack_bot.is_retry(request.headers):
        return {"ok": True, "skipped": "retry"}

    if payload.get("type") == "event_callback":
        event = payload.get("event") or {}
        etype = event.get("type")
        # Ignore any message the bot itself (or another bot) authored.
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return {"ok": True, "skipped": "bot"}

        if etype == "app_mention":
            channel = event.get("channel", "")
            thread_ts = event.get("thread_ts") or event.get("ts") or ""
            user_text = event.get("text", "") or ""
            user_id = event.get("user", "") or ""
            if channel:
                background.add_task(_converse, channel, thread_ts, user_text, user_id)
            return {"ok": True}

        if etype == "message":
            channel = event.get("channel", "")
            channel_type = event.get("channel_type", "")
            thread_ts = event.get("thread_ts") or ""
            user_text = event.get("text", "") or ""
            if not channel or not user_text:
                return {"ok": True}
            # app_mention already handles messages that mention the bot — skip
            # here so a mention inside a channel doesn't get answered twice.
            bot_id = slack_bot.get_bot_user_id()
            if bot_id and f"<@{bot_id}>" in user_text:
                return {"ok": True, "skipped": "mention-handled-elsewhere"}
            is_dm = channel_type == "im"
            # Never over-listen to general channel chatter: a plain (non-DM)
            # message only gets a reply when it's inside a thread the bot has
            # already replied in this process's lifetime.
            is_active_reply = bool(thread_ts) and slack_bot.is_active_thread(channel, thread_ts)
            if is_dm or is_active_reply:
                user_id = event.get("user", "") or ""
                background.add_task(_converse, channel, thread_ts, user_text, user_id)
            return {"ok": True}
    return {"ok": True}


# ---- guidelines (operator-uploaded brand/content guidelines) ---------------
class GuidelineReq(BaseModel):
    guideline_type: str
    content: str


@app.get("/guidelines")
def list_guidelines() -> JSONResponse:
    items = []
    if os.path.isdir(_GUIDELINES_DIR):
        for fn in sorted(os.listdir(_GUIDELINES_DIR)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(_GUIDELINES_DIR, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    txt = f.read()
            except OSError:
                continue
            first = txt.splitlines()[0] if txt else ""
            gtype = first.lstrip("# ").strip() if first.startswith("#") else fn
            mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            items.append({"filename": fn, "type": gtype, "chars": len(txt),
                          "modified": mtime.strftime("%Y-%m-%d %H:%M UTC")})
    return JSONResponse({"types": GUIDELINE_TYPES, "saved": items})


@app.post("/guidelines")
def save_guideline(req: GuidelineReq,
                   x_run_secret: Optional[str] = Header(default=None, alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    if req.guideline_type not in GUIDELINE_TYPES:
        raise HTTPException(400, f"unknown guideline_type (expected one of {GUIDELINE_TYPES})")
    if not req.content.strip():
        raise HTTPException(400, "content is empty")
    os.makedirs(_GUIDELINES_DIR, exist_ok=True)
    fn = _slug(req.guideline_type) + ".md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(os.path.join(_GUIDELINES_DIR, fn), "w", encoding="utf-8") as f:
        f.write(f"# {req.guideline_type}\n\n_Saved {ts}_\n\n{req.content.strip()}\n")
    log.info("Saved guideline '%s' (%d chars)", req.guideline_type, len(req.content))
    return {"saved": fn, "guideline_type": req.guideline_type, "chars": len(req.content)}


# ---- dashboard HTML --------------------------------------------------------
_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Storelli Marketing Brain</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Saira:ital,wght@0,400;0,500;0,600;0,700;0,800;0,900;1,800;1,900&display=swap" rel="stylesheet">
<style>
  :root{--bg:#050505;--yellow:#e4f000;--panel:rgba(16,16,16,.72);--border:rgba(255,255,255,.08);
        --gray:#8d8d8d;--gray-dim:#6b6b6b;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:#fff;font-family:'Saira',sans-serif;
       padding:32px 18px 64px;display:flex;justify-content:center}
  .wrap{width:100%;max-width:720px}
  header{display:flex;flex-direction:column;align-items:center;text-align:center;margin-bottom:30px}
  header img{width:180px;height:auto;filter:drop-shadow(0 2px 14px rgba(0,0,0,.5));margin-bottom:18px}
  h1{margin:0;line-height:.9;font-style:italic;font-weight:900;letter-spacing:-1px}
  h1 .a{color:#fff;font-size:42px;display:block}
  h1 .b{color:var(--yellow);font-size:42px;display:block}
  .sub{color:var(--gray);font-weight:600;font-size:13px;letter-spacing:7px;margin-top:14px}
  section{background:var(--panel);border:1px solid var(--border);border-radius:18px;
          padding:22px 22px 24px;margin-top:18px;backdrop-filter:blur(2px)}
  h2{margin:0 0 16px;font-size:13px;font-weight:700;letter-spacing:3px;color:#fff;text-transform:uppercase}
  h2 .pin{color:var(--yellow);margin-right:10px}
  label.fld{display:block;font-size:11px;letter-spacing:1.5px;color:var(--gray);
            text-transform:uppercase;margin:0 0 6px}
  .row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
  .row > div{flex:1;min-width:130px}
  select,input{width:100%;padding:11px 12px;font-size:15px;font-family:inherit;color:#fff;
       background:#0c0c0c;border:1px solid var(--border);border-radius:10px}
  select:focus,input:focus{outline:none;border-color:rgba(228,240,0,.55)}
  .toggle{display:flex;align-items:center;gap:10px;height:43px}
  .toggle input{width:auto}
  button{font-family:inherit;cursor:pointer;border:0;border-radius:12px;font-weight:800;
         font-style:italic;letter-spacing:.5px;transition:transform .12s,box-shadow .2s,background .2s}
  button:disabled{opacity:.5;cursor:not-allowed}
  .btn-primary{width:100%;height:62px;background:var(--yellow);color:#000;font-size:22px;
       margin-top:16px;box-shadow:0 0 50px rgba(228,240,0,.16)}
  .btn-primary:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 0 70px rgba(228,240,0,.28)}
  .btn-row{display:flex;gap:12px;margin-top:14px}
  .btn-secondary{flex:1;height:52px;background:rgba(228,240,0,.04);color:var(--yellow);
       border:1.5px solid rgba(228,240,0,.45);font-size:16px}
  .btn-secondary:hover:not(:disabled){background:rgba(228,240,0,.1);border-color:var(--yellow)}
  .notion{display:flex;align-items:center;justify-content:center;gap:12px;width:100%;height:60px;
       background:rgba(228,240,0,.04);color:var(--yellow);border:1.5px solid rgba(228,240,0,.55);
       font-size:20px;text-decoration:none;border-radius:14px}
  .notion:hover{background:rgba(228,240,0,.1);border-color:var(--yellow)}
  .notion.off{color:var(--gray-dim);border-color:var(--border);pointer-events:none}
  .pill{display:inline-block;padding:.2rem .7rem;border-radius:999px;font-size:12px;font-weight:700;
        letter-spacing:1px;text-transform:uppercase;background:#222;color:#bbb}
  .pill.queued,.pill.running{background:#3a3300;color:#ffe066}
  .pill.completed{background:#0c3d18;color:#7ee29a}
  .pill.failed{background:#3d0c0c;color:#ff8f8f}
  .err{background:#3d0c0c;border:1px solid #5a1a1a;color:#ffb3b3;padding:10px 14px;border-radius:10px;
       margin-top:12px;font-size:13px;white-space:pre-wrap;word-break:break-word;display:none}
  .stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:14px}
  .stat{background:#0c0c0c;border:1px solid var(--border);border-radius:12px;padding:12px 6px;text-align:center}
  .stat .num{font-size:30px;font-weight:800;line-height:1}
  .stat .lbl{font-size:10px;letter-spacing:1px;color:var(--gray);text-transform:uppercase;margin-top:6px}
  table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
  th,td{text-align:left;padding:5px 6px;border-bottom:1px solid rgba(255,255,255,.06)}
  th{color:var(--gray);font-weight:600;letter-spacing:1px}
  .meta{font-size:12px;color:var(--gray);margin-top:8px}
  pre{background:#0c0c0c;border:1px solid var(--border);border-radius:12px;padding:14px;
      max-height:380px;overflow:auto;font-size:12px;line-height:1.5;white-space:pre-wrap;color:#d8d8d8}
  textarea{width:100%;margin-top:12px;padding:11px 12px;font-family:inherit;font-size:14px;color:#fff;
      background:#0c0c0c;border:1px solid var(--border);border-radius:10px;resize:vertical;min-height:120px}
  textarea:focus{outline:none;border-color:rgba(228,240,0,.55)}
  .hint{font-size:11px;color:var(--gray-dim);margin-top:6px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <img src="/static/logo-accent.png" alt="Storelli" onerror="this.style.display='none'">
    <h1><span class="a">STORELLI</span><span class="b">MARKETING BRAIN</span></h1>
    <div class="sub">SOCIAL LEARNING</div>
  </header>

  <div id="err" class="err"></div>

  <!-- 1 + 2: Social Media Learning + Run Controls -->
  <section>
    <h2><span class="pin">+</span>Run Controls</h2>
    <div class="row">
      <div>
        <label class="fld">Limit (rows)</label>
        <select id="limit">
          <option>5</option><option selected>18</option><option>25</option>
          <option>50</option><option>150</option><option value="all">All</option>
        </select>
      </div>
      <div style="flex:0 0 140px">
        <label class="fld">QA pass</label>
        <div class="toggle"><input type="checkbox" id="qa"><span id="qaLbl">off (1 call/row)</span></div>
      </div>
      <div>
        <label class="fld">Run secret</label>
        <input id="secret" type="password" placeholder="RUN_SECRET" autocomplete="off">
      </div>
    </div>
    <button class="btn-primary" id="btnSocial" onclick="run('social')">⚡ Run Social Media Learning</button>
    <button class="btn-secondary" id="btnTagAll" onclick="run('analyze-all')"
            style="width:100%;height:52px;margin-top:12px">Analyze All Untagged Videos</button>
    <div class="btn-row">
      <button class="btn-secondary" id="btnCorr" onclick="run('correlations')">Run Correlations</button>
      <button class="btn-secondary" id="btnSyn" onclick="run('synthesize')">Generate Learnings</button>
    </div>
    <div class="hint">QA off = 1 Gemini call/row (stretches free-tier quota). Already-analyzed rows are always skipped.
      <br><b>Run Social Media Learning</b> = performance-safe learning run (requires PERFORMANCE).
      <b>Analyze All Untagged Videos</b> = taxonomy tags every LINK, no PERFORMANCE required; those rows still stay out of correlations.</div>
  </section>

  <!-- 3: Run Status -->
  <section>
    <h2><span class="pin">+</span>Run Status &nbsp;<span id="pill" class="pill">idle</span></h2>
    <div class="meta" id="meta">—</div>
    <div class="stats">
      <div class="stat"><div class="num" id="s_scanned">–</div><div class="lbl">Scanned</div></div>
      <div class="stat"><div class="num" id="s_analyzed">–</div><div class="lbl">Analyzed</div></div>
      <div class="stat"><div class="num" id="s_skipped">–</div><div class="lbl">Skipped</div></div>
      <div class="stat"><div class="num" id="s_review">–</div><div class="lbl">Needs review</div></div>
      <div class="stat"><div class="num" id="s_failed">–</div><div class="lbl">Failed</div></div>
    </div>
    <div id="signals"></div>
  </section>

  <!-- 4: Latest Learnings -->
  <section>
    <h2><span class="pin">+</span>Latest Learnings</h2>
    <pre id="learnings">Loading…</pre>
  </section>

  <!-- 5: Notion Link -->
  <section>
    <h2><span class="pin">+</span>Notion Brain</h2>
    <div class="btn-row">
      <button class="btn-secondary" id="btnNotion" onclick="run('notion-sync')">Update Notion Brain</button>
      <button class="btn-secondary" id="btnSlack" onclick="run('slack-report')">Send Slack Report</button>
    </div>
    <div class="meta" id="notionSummary"></div>
    <a id="notion" class="notion off" target="_blank" rel="noopener" style="margin-top:14px">Open Notion Dashboard</a>
  </section>

  <!-- 5b: Inspiration Layer (external monitoring — isolated) -->
  <section>
    <h2><span class="pin">+</span>Inspiration Layer</h2>
    <button class="btn-secondary" id="btnQueue" onclick="run('process-inspiration-queue')"
            style="width:100%;height:52px">Process Inspiration URL Queue</button>
    <button class="btn-secondary" id="btnDiscover" onclick="run('discover-inspiration')"
            style="width:100%;height:52px;margin-top:12px">Discover Inspiration from Apify</button>
    <button class="btn-secondary" id="btnProfiles" onclick="run('build-winning-profiles')"
            style="width:100%;height:52px;margin-top:12px">Build Winning Format Profiles</button>
    <button class="btn-secondary" id="btnMatch" onclick="run('match-inspiration')"
            style="width:100%;height:52px;margin-top:12px">Match Inspiration to Winning Profiles</button>
    <button class="btn-secondary" id="btnQuality" onclick="run('quality-review-inspiration')"
            style="width:100%;height:52px;margin-top:12px">Quality Review Inspiration Candidates</button>
    <button class="btn-primary" id="btnIdeas" onclick="run('generate-ideas')"
            style="width:100%;height:52px;margin-top:12px">Generate Rated Creative Ideas</button>
    <button class="btn-secondary" id="btnRefine" onclick="run('refine-ideas')"
            style="width:100%;height:52px;margin-top:12px">Refine Creative Ideas</button>
    <button class="btn-secondary" id="btnCalRate" onclick="run('rate-calendar-ideas')"
            style="width:100%;height:52px;margin-top:12px">Rate Notion Calendar Ideas</button>
    <button class="btn-secondary" id="btnSemantic" onclick="run('build-semantic-connections')"
            style="width:100%;height:52px;margin-top:12px">Build Semantic Connections</button>
    <button class="btn-secondary" id="btnAnalyzeInsp" onclick="run('analyze-inspiration')"
            style="width:100%;height:52px;margin-top:12px">Analyze Inspiration Content</button>
    <button class="btn-secondary" id="btnScanInsp" onclick="run('scan-inspiration')"
            style="width:100%;height:52px;margin-top:12px">Scan Monitored Channels</button>
    <div class="meta" id="inspSummary"></div>
    <div class="hint">Paste promising reel/post URLs into <b>INSPIRATION_URL_QUEUE</b> and click
      <b>Process Inspiration URL Queue</b> — each URL's metadata is fetched via yt-dlp + cookies
      (no profile enumeration, no Apify) and stored in <b>INSPIRATION_CONTENT</b>
      (SOURCE_TYPE = EXTERNAL_INSPIRATION). External inspiration is kept fully separate from
      Storelli performance, correlations, and learnings — it is never Storelli proof.</div>
  </section>

  <!-- 6: Upload Guidelines -->
  <section>
    <h2><span class="pin">+</span>Upload Guidelines</h2>
    <div class="hint" style="margin-top:0;margin-bottom:10px">Paste brand/content guidelines. Saved to data/guidelines/ for future content, email &amp; ad generation.</div>
    <div class="row">
      <div><label class="fld">Guideline type</label>
        <select id="gtype">
          <option>Social Content Guidelines</option>
          <option>Email Guidelines</option>
          <option>Ads Guidelines</option>
          <option>Brand Voice Guidelines</option>
          <option>Product Messaging Guidelines</option>
        </select></div>
    </div>
    <textarea id="gcontent" placeholder="Paste guidelines here…"></textarea>
    <button class="btn-secondary" id="btnGuide" style="margin-top:12px" onclick="saveGuideline()">Save Guideline</button>
    <div class="meta" id="guideMsg"></div>
    <div class="meta" id="guideList">Loading…</div>
  </section>
</div>

<script>
const $ = id => document.getElementById(id);
function showErr(m){ const e=$('err'); if(m){e.textContent=m;e.style.display='block';} else {e.style.display='none';} }

$('qa').addEventListener('change', e => {
  $('qaLbl').textContent = e.target.checked ? 'on (2 calls/row)' : 'off (1 call/row)';
});

function tbl(title, rows){
  if(!rows || !rows.length) return '<p class="meta"><b>'+title+'</b>: (none yet)</p>';
  let h='<p class="meta" style="margin-bottom:2px"><b>'+title+'</b></p><table><thead><tr>'
       +'<th>signal</th><th>layer</th><th>lift</th><th>n</th><th>conf</th></tr></thead><tbody>';
  for(const r of rows) h+='<tr><td>'+r.label+'</td><td>'+r.layer+'</td><td>'+r.lift+'</td><td>'+r.n+'</td><td>'+r.confidence+'</td></tr>';
  return h+'</tbody></table>';
}

async function poll(){
  try{
    const j = await (await fetch('/status')).json();
    const p=$('pill'); p.textContent=j.status; p.className='pill '+j.status;
    const busy = (j.status==='queued'||j.status==='running');
    ['btnSocial','btnTagAll','btnCorr','btnSyn','btnNotion','btnSlack','btnScanInsp','btnQueue','btnAnalyzeInsp','btnDiscover','btnProfiles','btnMatch','btnQuality','btnIdeas','btnRefine','btnCalRate','btnSemantic'].forEach(b=>{const el=$(b); if(el) el.disabled=busy;});
    const s=j.stats||{};
    const skipped=(s.skipped_already_analyzed||0)+(s.skipped_no_performance||0)+(s.skipped_no_link||0);
    $('s_scanned').textContent = s.scanned ?? '–';
    $('s_analyzed').textContent= s.analyzed ?? '–';
    $('s_skipped').textContent = (s.scanned!=null)?skipped:'–';
    $('s_review').textContent  = s.needs_review ?? '–';
    $('s_failed').textContent  = s.failed ?? '–';
    let meta = 'action: '+(j.action||'—')+' · limit: '+(j.limit==null?'all':j.limit)+' · qa: '+(j.qa?'on':'off');
    if(j.started_at) meta+=' · started '+j.started_at;
    if(j.finished_at) meta+=' · finished '+j.finished_at;
    if(s.quota_stopped) meta+=' · ⚠ stopped on Gemini quota (429)';
    $('meta').textContent=meta;
    $('signals').innerHTML = tbl('Top winning signals', j.top_winning)+tbl('Top weak signals', j.top_weak);
    // Notion button + sync summary
    const n=$('notion');
    if(j.notion_url){ n.href=j.notion_url; n.classList.remove('off'); n.textContent='Open Notion Dashboard ↗'; }
    else { n.removeAttribute('href'); n.classList.add('off'); n.textContent='Notion not configured'; }
    const ns=j.notion_summary||{};
    const sumKeys=Object.keys(ns);
    if(sumKeys.length){
      $('notionSummary').textContent = 'Last Notion sync — ' + sumKeys.map(k=>{
        const d=ns[k]; return k+': +'+(d.created||0)+' / ~'+(d.updated||0);
      }).join(' · ');
    } else {
      $('notionSummary').textContent = 'Notion: '+(j.notion||'not run')+(j.slack&&j.slack!=='not_run'?(' · Slack: '+j.slack):'');
    }
    // Inspiration Layer — last scan summary (isolated from internal learning)
    const ins=j.inspiration||{};
    if(ins && ins.RUN_ID){
      const t = ins.RUN_TYPE;
      let txt;
      if(t==='Analyze'){
        txt = 'Last analysis ('+ins.STATUS+') — eligible: '+(ins.POSTS_DISCOVERED||0)
          +' · analyzed: '+(ins.POSTS_ANALYZED||0)
          +((ins.POSTS_FAILED)?(' · failed: '+ins.POSTS_FAILED):'');
      } else if(t==='Match'){
        txt = 'Last match ('+ins.STATUS+') — external rows: '+(ins.POSTS_DISCOVERED||0)
          +' · matched: '+(ins.POSTS_ANALYZED||0)+' · shortlisted: '+(ins.POSTS_SHORTLISTED||0);
      } else if(t==='QualityReview'){
        txt = 'Last quality review ('+ins.STATUS+') — reviewed: '+(ins.POSTS_DISCOVERED||0)
          +' · use-for-idea-gen: '+(ins.POSTS_SHORTLISTED||0);
      } else if(t==='Ideas'){
        txt = 'Last idea run ('+ins.STATUS+') — profiles: '+(ins.POSTS_DISCOVERED||0)
          +' · ideas: '+(ins.POSTS_ADDED||0)+' · high-priority: '+(ins.POSTS_SHORTLISTED||0);
      } else if(t==='Refine'){
        txt = 'Last refinement ('+ins.STATUS+') — ideas: '+(ins.POSTS_DISCOVERED||0)
          +' · refined: '+(ins.POSTS_ANALYZED||0)+' · clean: '+(ins.POSTS_SHORTLISTED||0);
      } else if(t==='CalendarRatings'){
        txt = 'Last calendar rating ('+ins.STATUS+') — rated: '+(ins.POSTS_ANALYZED||0)
          +' · excluded: '+(ins.POSTS_SKIPPED_EXISTING||0)+' · keep: '+(ins.POSTS_SHORTLISTED||0);
      } else if(t==='Profiles'){
        txt = 'Last profiles build ('+ins.STATUS+') — internal rows: '+(ins.POSTS_DISCOVERED||0)
          +' · created: '+(ins.POSTS_ADDED||0)+' · updated: '+(ins.POSTS_ANALYZED||0)
          +' · active: '+(ins.POSTS_SHORTLISTED||0);
      } else if(t==='Discovery'){
        txt = 'Last discovery ('+ins.STATUS+') — queries: '+(ins.CHANNELS_SCANNED||0)
          +' · candidates: '+(ins.POSTS_DISCOVERED||0)+' · added: '+(ins.POSTS_ADDED||0)
          +' · skipped: '+(ins.POSTS_SKIPPED_EXISTING||0)
          +((ins.CHANNELS_FAILED)?(' · failed: '+ins.CHANNELS_FAILED):'');
      } else if(t==='Queue'){
        txt = 'Last queue run ('+ins.STATUS+') — URLs: '+(ins.POSTS_DISCOVERED||0)
          +' · added: '+(ins.POSTS_ADDED||0)+' · dupes: '+(ins.POSTS_SKIPPED_EXISTING||0)
          +((ins.POSTS_FAILED)?(' · failed: '+ins.POSTS_FAILED):'');
      } else {
        txt = 'Last scan ('+ins.STATUS+') — channels: '+(ins.CHANNELS_SCANNED||0)
          +' · added: '+(ins.POSTS_ADDED||0)+' · dupes: '+(ins.POSTS_SKIPPED_EXISTING||0)
          +((ins.CHANNELS_FAILED)?(' · failed: '+ins.CHANNELS_FAILED):'');
      }
      $('inspSummary').textContent = txt;
    }
    showErr(j.error);
  }catch(e){ showErr('status fetch failed: '+e); }
}

async function loadLearnings(){
  try{
    const j = await (await fetch('/learnings')).json();
    $('learnings').textContent = j.exists ? j.content : (j.error||'No learnings yet.');
  }catch(e){ $('learnings').textContent='failed to load learnings: '+e; }
}

async function run(action){
  showErr('');
  const secret=$('secret').value;
  const paths = {social:'/run/social', 'analyze-all':'/run/analyze-all',
                 correlations:'/run/correlations', synthesize:'/run/synthesize',
                 'notion-sync':'/run/notion-sync', 'slack-report':'/run/slack-report',
                 'scan-inspiration':'/run/scan-inspiration',
                 'process-inspiration-queue':'/run/process-inspiration-queue',
                 'analyze-inspiration':'/run/analyze-inspiration',
                 'discover-inspiration':'/run/discover-inspiration',
                 'build-winning-profiles':'/run/build-winning-profiles',
                 'match-inspiration':'/run/match-inspiration',
                 'quality-review-inspiration':'/run/quality-review-inspiration',
                 'generate-ideas':'/run/generate-ideas',
                 'refine-ideas':'/run/refine-ideas',
                 'rate-calendar-ideas':'/run/rate-calendar-ideas',
                 'build-semantic-connections':'/run/build-semantic-connections'};
  const path = paths[action];
  const body = (action==='social' || action==='analyze-all')
    ? JSON.stringify({limit:$('limit').value, qa:$('qa').checked}) : '{}';
  try{
    const r = await fetch(path, {method:'POST',
      headers:{'X-Run-Secret':secret,'Content-Type':'application/json'}, body});
    if(!r.ok){ showErr('error '+r.status+': '+(await r.text())); }
    await poll();
    if(action!=='social') setTimeout(()=>{poll();loadLearnings();}, 1500);
  }catch(e){ showErr('request failed: '+e); }
}

async function loadGuidelines(){
  try{
    const j = await (await fetch('/guidelines')).json();
    $('guideList').innerHTML = (j.saved && j.saved.length)
      ? '<b>Saved:</b> ' + j.saved.map(g => g.type+' ('+g.chars+' chars · '+g.modified+')').join('<br>')
      : 'No guidelines saved yet.';
  }catch(e){ $('guideList').textContent='failed to load guidelines: '+e; }
}
async function saveGuideline(){
  const secret=$('secret').value;
  $('guideMsg').textContent='';
  try{
    const r = await fetch('/guidelines', {method:'POST',
      headers:{'X-Run-Secret':secret,'Content-Type':'application/json'},
      body: JSON.stringify({guideline_type:$('gtype').value, content:$('gcontent').value})});
    if(!r.ok){ $('guideMsg').textContent='error '+r.status+': '+(await r.text()); return; }
    $('guideMsg').textContent='Saved.'; $('gcontent').value=''; loadGuidelines();
  }catch(e){ $('guideMsg').textContent='request failed: '+e; }
}

poll(); loadLearnings(); loadGuidelines();
setInterval(poll, 3000);
setInterval(loadLearnings, 15000);
</script>
</body></html>
"""
