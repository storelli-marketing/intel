"""In-process weekly scheduler for the intelligence refresh.

Why this exists
---------------
Every stage of the weekly pipeline was already built — the owned-account scan,
the external Apify discovery ranked by view/follower ratio, analysis, correlation
rebuild, winning profiles, Notion sync — and `intelligence_refresh.
run_intelligence_refresh()` orchestrates all of it, fail-soft and lock-protected.

Nothing ever called it. The repo created no schedule, the README said recurrence
had to come from "a Railway Cron invoking `refresh-intelligence`", and no such
cron existed, so the self-updating brain was self-updating in design only.

This module closes that gap inside the service that is already deployed: no new
infrastructure, no second set of credentials, nothing to wire up in a dashboard.

Design
------
* A single daemon thread started from the FastAPI startup hook. Never started at
  import time, so importing `web` (tests, tooling) schedules nothing.
* The refresh is synchronous and slow (Sheets, Apify, Gemini), so it runs on a
  thread and never touches the event loop.
* The thread WAKES hourly and asks "has a successful run finished within the
  cadence?" rather than sleeping for a week. A restart therefore never loses or
  double-fires the schedule — the answer comes from the run history in the
  sheet, not from process memory.
* Overlap is already handled: `run_intelligence_refresh` takes a lock row in
  INTELLIGENCE_REFRESH_RUNS with a stale timeout, so two replicas, or a replica
  and someone clicking the dashboard button, cannot corrupt each other. The
  second one exits cleanly as `locked_out`.
* Fail-soft throughout. A missing token, an exhausted quota, an unreachable
  sheet — none of it may take down the web process. Every failure is recorded in
  `STATE` and retried at the next cadence window.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

import config
from logger import get_logger

log = get_logger()

# Observable state, surfaced on GET /status so "is the weekly job actually
# running?" is answerable from outside the box.
STATE: dict = {
    "enabled": False,
    "thread_alive": False,
    "cadence_days": config.INTELLIGENCE_REFRESH_CADENCE_DAYS,
    "check_interval_minutes": config.INTELLIGENCE_SCHEDULER_CHECK_MINUTES,
    "started_at": None,
    "last_check_at": None,
    "last_decision": "",
    "last_run_id": None,
    "last_run_status": None,
    "last_run_finished_at": None,
    "last_run_trigger": None,
    "runs_started": 0,
    "consecutive_errors": 0,
    "last_error": "",
    "disabled_reason": "",
    "last_digest_slack": None,
    "last_digest_email": None,
}

# RLock, not Lock: `start()` checks state under the lock and then returns
# `snapshot()`, which takes it again. A plain Lock deadlocked the idempotent
# start path — the second call to start() hung forever.
_LOCK = threading.RLock()
_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()

TRIGGER = "scheduler"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _set(**fields) -> None:
    with _LOCK:
        STATE.update(**fields)


def snapshot() -> dict:
    with _LOCK:
        s = dict(STATE)
    s["thread_alive"] = bool(_THREAD and _THREAD.is_alive())
    return s


def _disabled_reason() -> str:
    """Why the scheduler will not run, or '' when it should."""
    if not config.INTELLIGENCE_SCHEDULER_ENABLED:
        return "INTELLIGENCE_SCHEDULER_ENABLED is false"
    if not config.INTELLIGENCE_REFRESH_ENABLED:
        return "INTELLIGENCE_REFRESH_ENABLED is false"
    # Without the Sheet there is no evidence base to refresh and no run log to
    # record against, so a schedule would just log failures every hour.
    if not (config.GOOGLE_SHEET_ID and config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH):
        return "Google Sheets is not configured (GOOGLE_SHEET_ID / service account)"
    return ""


def due(now=None) -> tuple[bool, str]:
    """(is_due, why). Due when no successful refresh has finished inside the
    cadence window. The answer comes from the run history, so it survives
    restarts and is shared across replicas."""
    import intelligence_refresh as ir
    now = now or datetime.now(timezone.utc)
    try:
        runs = ir.last_runs(n=25)
    except Exception as e:  # noqa: BLE001 - unreachable history must not crash the loop
        return False, f"run history unavailable: {type(e).__name__}: {e}"

    # `last_runs` returns NEWEST FIRST (it reverses the sheet order), so the most
    # recent success is [0]. Taking [-1] would anchor on the oldest run in the
    # window and fire a refresh every hour.
    succeeded = [r for r in runs
                 if str(r.get("STATUS", "")).strip().lower() in ("success", "partial")]
    if not succeeded:
        return True, "no successful refresh on record yet"

    last = succeeded[0]
    stamp = last.get("FINISHED_AT") or last.get("STARTED_AT") or ""
    age = ir._seconds_since(stamp)
    if age is None:
        return True, f"last run {last.get('RUN_ID')} has no readable timestamp"
    days = age / 86400.0
    cadence = config.INTELLIGENCE_REFRESH_CADENCE_DAYS
    if days >= cadence:
        return True, f"last successful refresh {days:.1f}d ago (cadence {cadence}d)"
    return False, (f"last successful refresh {days:.1f}d ago — next due in "
                   f"{cadence - days:.1f}d")


def run_once(trigger: str = TRIGGER) -> Optional[dict]:
    """Run the full refresh if it is due. Returns the report, or None when it
    was not due or could not run. Never raises."""
    import intelligence_refresh as ir
    is_due, why = due()
    _set(last_check_at=_now(), last_decision=why)
    if not is_due:
        log.info("scheduler: not due — %s", why)
        return None

    log.info("scheduler: starting weekly refresh — %s", why)
    with _LOCK:
        STATE["runs_started"] += 1
    try:
        report = ir.run_intelligence_refresh(mode="full", dry_run=False, trigger=trigger)
    except Exception as e:  # noqa: BLE001 - the web process must survive any failure
        log.exception("scheduler: refresh raised")
        with _LOCK:
            STATE["consecutive_errors"] += 1
            STATE["last_error"] = f"{type(e).__name__}: {e}"
        return None

    _set(last_run_id=report.get("run_id"), last_run_status=report.get("status"),
         last_run_finished_at=report.get("finished_at"), last_run_trigger=trigger,
         consecutive_errors=0, last_error="",
         last_decision=(report.get("reason") or why))
    if report.get("locked_out"):
        log.info("scheduler: another refresh held the lock — exited cleanly")
        return report

    log.info("scheduler: refresh %s (%s)", report.get("status"), report.get("run_id"))
    # Push the weekly digest to where people actually read it. Best effort by
    # design: a Slack outage or a bad SMTP password must never turn a successful
    # refresh into a failed one, so the outcome is recorded and nothing raises.
    if config.DIGEST_ENABLED:
        try:
            import refresh_digest
            health = None
            try:
                import intelligence_refresh as _ir
                health = _ir.health_state()
            except Exception:  # noqa: BLE001 - health is a nice-to-have here
                health = None
            delivery = refresh_digest.deliver(report, health)
            _set(last_digest_slack=delivery.get("slack"),
                 last_digest_email=delivery.get("email"))
        except Exception as e:  # noqa: BLE001
            log.warning("scheduler: digest delivery failed: %s", e)
            _set(last_digest_slack="failed", last_digest_email="failed")
    return report


def _loop() -> None:
    interval = max(60, config.INTELLIGENCE_SCHEDULER_CHECK_MINUTES * 60)
    # A short settle delay so a cold start (or a redeploy loop) never fires a
    # heavy job before the process is serving traffic.
    if _STOP.wait(config.INTELLIGENCE_SCHEDULER_STARTUP_DELAY_SECONDS):
        return
    while not _STOP.is_set():
        try:
            run_once()
        except Exception as e:  # noqa: BLE001 - belt and braces; the loop must not die
            log.exception("scheduler: loop iteration failed")
            with _LOCK:
                STATE["consecutive_errors"] += 1
                STATE["last_error"] = f"{type(e).__name__}: {e}"
        if _STOP.wait(interval):
            return


def start() -> dict:
    """Start the scheduler thread if it should run and isn't already running.
    Idempotent and never raises — returns the resulting state snapshot."""
    global _THREAD
    reason = _disabled_reason()
    if reason:
        _set(enabled=False, disabled_reason=reason)
        log.info("scheduler: not started — %s", reason)
        return snapshot()
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return snapshot()
    _STOP.clear()
    t = threading.Thread(target=_loop, name="intelligence-scheduler", daemon=True)
    with _LOCK:
        _THREAD = t
    t.start()
    _set(enabled=True, disabled_reason="", started_at=_now(),
         cadence_days=config.INTELLIGENCE_REFRESH_CADENCE_DAYS,
         check_interval_minutes=config.INTELLIGENCE_SCHEDULER_CHECK_MINUTES)
    log.info("scheduler: started — full refresh every %dd, checking every %dm",
             config.INTELLIGENCE_REFRESH_CADENCE_DAYS,
             config.INTELLIGENCE_SCHEDULER_CHECK_MINUTES)
    return snapshot()


def stop(timeout: float = 5.0) -> None:
    """Signal the thread to exit (used on shutdown and by tests)."""
    _STOP.set()
    with _LOCK:
        t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _set(enabled=False)
