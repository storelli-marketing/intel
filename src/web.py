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
import secrets as stdlib_secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
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
    <div class="btn-row">
      <button class="btn-secondary" id="btnCorr" onclick="run('correlations')">Run Correlations</button>
      <button class="btn-secondary" id="btnSyn" onclick="run('synthesize')">Generate Learnings</button>
    </div>
    <div class="hint">QA off = 1 Gemini call/row (stretches free-tier quota). Already-analyzed rows are always skipped.</div>
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
    ['btnSocial','btnCorr','btnSyn','btnNotion','btnSlack'].forEach(b=>$(b).disabled=busy);
    const s=j.stats||{};
    const skipped=(s.skipped_already_analyzed||0)+(s.skipped_no_performance||0);
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
  const paths = {social:'/run/social', correlations:'/run/correlations', synthesize:'/run/synthesize',
                 'notion-sync':'/run/notion-sync', 'slack-report':'/run/slack-report'};
  const path = paths[action];
  const body = action==='social' ? JSON.stringify({limit:$('limit').value, qa:$('qa').checked}) : '{}';
  try{
    const r = await fetch(path, {method:'POST',
      headers:{'X-Run-Secret':secret,'Content-Type':'application/json'}, body});
    if(!r.ok){ showErr('error '+r.status+': '+(await r.text())); }
    await poll();
    if(action!=='social') setTimeout(()=>{poll();loadLearnings();}, 1500);
  }catch(e){ showErr('request failed: '+e); }
}

poll(); loadLearnings();
setInterval(poll, 3000);
setInterval(loadLearnings, 15000);
</script>
</body></html>
"""
