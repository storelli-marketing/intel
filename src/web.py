"""Minimal internal web trigger for the Storelli intelligence MVP.

Endpoints:
  GET  /            -> tiny HTML dashboard (no framework, no login).
  POST /run/social  -> queues analyze + correlations (+ optional notion-sync)
                       in the background. Requires X-Run-Secret header that
                       matches RUN_SECRET env. Single-flight (409 if running).
  GET  /status      -> latest run state (idle/queued/running/completed/failed)
                       + counts and top winning/weak signals.

Architecture stays exactly as before: this just wraps the same CLI functions.
No database. State is kept in-memory; on Railway restart, state resets.
"""
from __future__ import annotations

import secrets as stdlib_secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import config
import correlations as corr
from logger import get_logger

log = get_logger()

app = FastAPI(title="Storelli Marketing Brain")

# In-memory state. One run at a time; latest result lives here.
STATE: dict = {
    "status": "idle",          # idle | queued | running | completed | failed
    "limit": None,
    "started_at": None,
    "finished_at": None,
    "stats": {},
    "top_winning": [],
    "top_weak": [],
    "notion": "not_configured",  # not_configured | skipped | synced | failed
    "notion_url": "",
    "error": "",
}
_LOCK = threading.Lock()


# ---- helpers ---------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(r: dict) -> dict:
    return {
        "signal": r["signal"], "layer": r["layer"], "label": r["label"],
        "lift": corr.fmt_lift(r["lift"]),
        "n": r["videos_with_signal"], "confidence": r["confidence"],
    }


def _check_secret(provided: Optional[str]) -> None:
    expected = config.RUN_SECRET
    if not expected:
        raise HTTPException(503, "RUN_SECRET not configured on the server")
    if not provided or not stdlib_secrets.compare_digest(provided, expected):
        raise HTTPException(401, "invalid run secret")


def _parse_limit(value) -> Optional[int]:
    if value in (None, "", "all"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "limit must be an integer or 'all'")
    if n <= 0:
        raise HTTPException(400, "limit must be > 0")
    return n


# ---- the actual work (runs in a thread) -----------------------------------
def _do_run(limit: Optional[int]) -> None:
    # Lazy imports so module import doesn't require Google/Gemini creds.
    from main import cmd_analyze, compute_findings, build_findings
    from sheets_client import SheetsClient

    try:
        with _LOCK:
            STATE.update(status="running", limit=limit, started_at=_now(),
                         finished_at=None, stats={}, top_winning=[], top_weak=[],
                         notion="not_configured", notion_url="", error="")

        stats = cmd_analyze(reprocess=False, limit=limit)

        sheets = SheetsClient()
        sheets.validate_columns()
        analyzed, buckets, results = compute_findings(sheets)
        top_w = [_short(x) for x in corr.winning(results)[:5]]
        top_b = [_short(x) for x in corr.weak(results)[:5]]

        notion_status = "not_configured"
        notion_url = ""
        if config.NOTION_API_KEY and config.NOTION_PARENT_PAGE_ID:
            if analyzed:
                try:
                    findings = build_findings(results, analyzed, buckets)
                    from notion_client import NotionDashboard
                    notion_url = NotionDashboard().publish(findings)
                    notion_status = "synced"
                except Exception as e:  # noqa: BLE001
                    log.exception("notion sync failed")
                    notion_status = "failed"
                    notion_url = f"error: {e}"
            else:
                notion_status = "skipped"

        with _LOCK:
            STATE.update(status="completed", finished_at=_now(), stats=stats,
                         top_winning=top_w, top_weak=top_b,
                         notion=notion_status, notion_url=notion_url)
    except Exception as e:  # noqa: BLE001
        log.exception("run failed")
        with _LOCK:
            STATE.update(status="failed", finished_at=_now(), error=str(e))


# ---- routes ----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _HTML


@app.get("/status")
def status() -> JSONResponse:
    with _LOCK:
        return JSONResponse(dict(STATE))


class RunReq(BaseModel):
    limit: Optional[str] = None  # "5"|"25"|"50"|"150"|"all"


@app.post("/run/social", status_code=202)
def run_social(req: RunReq, background: BackgroundTasks,
               x_run_secret: Optional[str] = Header(default=None,
                                                    alias="X-Run-Secret")) -> dict:
    _check_secret(x_run_secret)
    limit = _parse_limit(req.limit)
    with _LOCK:
        if STATE["status"] in ("queued", "running"):
            raise HTTPException(409, "a run is already in progress")
        STATE.update(status="queued", limit=limit, error="")
    background.add_task(_do_run, limit)
    return {"status": "queued", "limit": limit}


# ---- HTML (no framework, inline) ------------------------------------------
_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Storelli Marketing Brain</title>
<style>
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;color:#111}
  h1{margin-bottom:.25rem} .sub{color:#666;margin-top:0;margin-bottom:1.5rem}
  form{display:flex;gap:.75rem;align-items:end;flex-wrap:wrap;margin-bottom:1.5rem}
  label{display:flex;flex-direction:column;font-size:.85rem;color:#555}
  select,input,button{padding:.5rem .75rem;font-size:1rem;border:1px solid #ccc;border-radius:6px}
  button{background:#111;color:#fff;border:0;cursor:pointer}
  button:disabled{opacity:.6;cursor:not-allowed}
  pre{background:#f5f5f5;padding:.75rem;border-radius:6px;overflow:auto;font-size:.85rem}
  .pill{display:inline-block;padding:.15rem .6rem;border-radius:999px;background:#eee;color:#333;font-size:.8rem;margin-left:.5rem}
  .pill.running,.pill.queued{background:#fff3bf;color:#7a5a00}
  .pill.completed{background:#d3f9d8;color:#0b5d1a}
  .pill.failed{background:#ffe3e3;color:#a01919}
  table{width:100%;border-collapse:collapse;font-size:.85rem;margin-top:.25rem}
  th,td{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #eee}
</style>
</head>
<body>
<h1>Storelli Marketing Brain</h1>
<p class="sub">Internal trigger — Sheets ↔ Gemini ↔ Notion. Correlations only, never causation.</p>

<form id="runForm" onsubmit="return run(event)">
  <label>Limit<select id="limit">
    <option value="5">5</option>
    <option value="25">25</option>
    <option value="50">50</option>
    <option value="150">150</option>
    <option value="all">all</option>
  </select></label>
  <label>Run secret<input id="secret" type="password" required></label>
  <button id="goBtn" type="submit">Run Social Media Learning</button>
</form>

<h2>Status <span id="pill" class="pill">idle</span></h2>
<pre id="status">loading…</pre>

<h2>Last run</h2>
<div id="summary">—</div>

<script>
async function poll(){
  try{
    const r = await fetch('/status'); const j = await r.json();
    const pill = document.getElementById('pill');
    pill.textContent = j.status; pill.className = 'pill ' + j.status;
    document.getElementById('goBtn').disabled =
      (j.status === 'queued' || j.status === 'running');
    document.getElementById('status').textContent = JSON.stringify({
      status: j.status, limit: j.limit,
      started_at: j.started_at, finished_at: j.finished_at,
      notion: j.notion, error: j.error,
    }, null, 2);
    document.getElementById('summary').innerHTML = renderSummary(j);
  }catch(e){
    document.getElementById('status').textContent = 'status fetch failed: ' + e;
  }
}
function tbl(title, rows){
  if(!rows || !rows.length) return '<p><b>'+title+'</b>: (none)</p>';
  let h = '<p><b>'+title+'</b></p><table><thead><tr>'
        + '<th>signal</th><th>layer</th><th>lift</th><th>n</th><th>confidence</th>'
        + '</tr></thead><tbody>';
  for(const r of rows){
    h += '<tr><td>'+r.label+'</td><td>'+r.layer+'</td><td>'+r.lift+'</td><td>'+r.n+'</td><td>'+r.confidence+'</td></tr>';
  }
  return h + '</tbody></table>';
}
function renderSummary(j){
  const s = j.stats || {};
  const counts = `<p>scanned ${s.scanned||0} · analyzed ${s.analyzed||0} · needs_review ${s.needs_review||0} · failed ${s.failed||0} · skipped ${s.skipped||0}</p>`;
  return counts + tbl('Top winning signals', j.top_winning) + tbl('Top weak signals', j.top_weak);
}
async function run(e){
  e.preventDefault();
  const limit = document.getElementById('limit').value;
  const secret = document.getElementById('secret').value;
  const r = await fetch('/run/social', {
    method: 'POST',
    headers: {'X-Run-Secret': secret, 'Content-Type': 'application/json'},
    body: JSON.stringify({limit}),
  });
  if(!r.ok){ alert('error ' + r.status + ': ' + (await r.text())); }
  poll();
  return false;
}
poll(); setInterval(poll, 3000);
</script>
</body></html>
"""
