"""The weekly intelligence refresh actually runs.

Every stage of the pipeline existed and was orchestrated by
`intelligence_refresh.run_intelligence_refresh`, but nothing ever invoked it —
the repo created no schedule and the docs pointed at a Railway Cron that was
never set up. These lock the scheduler that closes that gap, and the properties
that keep it safe to run inside the web process:

  * it is INERT until the app actually starts serving (importing `web` must
    never spend Apify/Gemini quota);
  * due/not-due is decided from the run HISTORY, not process memory, so a
    restart neither loses nor double-fires the schedule;
  * it refuses to start when nothing is configured rather than failing hourly;
  * no failure inside it can take down the service.
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import intelligence_refresh as ir
import scheduler


def stamp(days_ago: float) -> str:
    """A run timestamp in the exact format the run log stores."""
    return (datetime.now(timezone.utc)
            - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M UTC")


def run_row(days_ago: float, status: str = "success", run_id: str = "IR-x") -> dict:
    return {"RUN_ID": run_id, "STARTED_AT": stamp(days_ago),
            "FINISHED_AT": stamp(days_ago), "STATUS": status, "TRIGGER": "scheduler"}


class _Base(unittest.TestCase):
    def setUp(self):
        self._real_last_runs = ir.last_runs
        self._real_run = ir.run_intelligence_refresh
        self._real_state = dict(scheduler.STATE)
        self._cfg = {k: getattr(config, k) for k in
                     ("INTELLIGENCE_SCHEDULER_ENABLED", "INTELLIGENCE_REFRESH_ENABLED",
                      "INTELLIGENCE_REFRESH_CADENCE_DAYS", "GOOGLE_SHEET_ID",
                      "GOOGLE_SERVICE_ACCOUNT_JSON_PATH",
                      "INTELLIGENCE_SCHEDULER_STARTUP_DELAY_SECONDS")}
        # Pretend the service is configured; individual tests narrow this.
        config.GOOGLE_SHEET_ID = "sheet-id"
        config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH = "/tmp/sa.json"

    def tearDown(self):
        scheduler.stop(timeout=2)
        ir.last_runs = self._real_last_runs
        ir.run_intelligence_refresh = self._real_run
        scheduler.STATE.clear()
        scheduler.STATE.update(self._real_state)
        scheduler._STOP.clear()
        for k, v in self._cfg.items():
            setattr(config, k, v)


class TestDueDecision(_Base):
    def test_no_history_is_due(self):
        ir.last_runs = lambda **k: []
        is_due, why = scheduler.due()
        self.assertTrue(is_due)
        self.assertIn("no successful refresh", why)

    def test_recent_success_is_not_due(self):
        ir.last_runs = lambda **k: [run_row(1)]
        is_due, why = scheduler.due()
        self.assertFalse(is_due)
        self.assertIn("next due in", why)

    def test_older_than_cadence_is_due(self):
        ir.last_runs = lambda **k: [run_row(8)]
        is_due, why = scheduler.due()
        self.assertTrue(is_due)
        self.assertIn("8.0d ago", why)

    def test_exactly_at_cadence_is_due(self):
        ir.last_runs = lambda **k: [run_row(config.INTELLIGENCE_REFRESH_CADENCE_DAYS)]
        self.assertTrue(scheduler.due()[0])

    def test_newest_run_wins_not_the_oldest_in_the_window(self):
        """`last_runs` returns NEWEST FIRST. Anchoring on the oldest row in the
        window would fire a refresh every single hour."""
        ir.last_runs = lambda **k: [run_row(1, run_id="new"), run_row(40, run_id="old")]
        is_due, why = scheduler.due()
        self.assertFalse(is_due, f"anchored on the wrong run: {why}")

    def test_a_failed_run_does_not_satisfy_the_cadence(self):
        ir.last_runs = lambda **k: [run_row(1, status="failed")]
        self.assertTrue(scheduler.due()[0])

    def test_a_partial_run_does_satisfy_the_cadence(self):
        """Partial means stages ran and the evidence moved; re-running hourly
        would just burn quota on the stages that already succeeded."""
        ir.last_runs = lambda **k: [run_row(1, status="partial")]
        self.assertFalse(scheduler.due()[0])

    def test_unreadable_timestamp_is_due_rather_than_stuck(self):
        ir.last_runs = lambda **k: [{"RUN_ID": "IR-y", "STATUS": "success",
                                     "FINISHED_AT": "not a date"}]
        self.assertTrue(scheduler.due()[0])

    def test_unreachable_history_is_not_due_and_does_not_raise(self):
        def boom(**k):
            raise RuntimeError("sheets down")
        ir.last_runs = boom
        is_due, why = scheduler.due()
        self.assertFalse(is_due)
        self.assertIn("run history unavailable", why)


class TestRunOnce(_Base):
    def test_runs_the_full_refresh_when_due(self):
        calls = []
        ir.last_runs = lambda **k: []
        ir.run_intelligence_refresh = lambda **kw: (
            calls.append(kw) or {"run_id": "IR-1", "status": "success",
                                 "finished_at": stamp(0)})
        report = scheduler.run_once()
        self.assertEqual(report["status"], "success")
        self.assertEqual(calls[0]["mode"], "full")
        self.assertFalse(calls[0]["dry_run"])
        self.assertEqual(calls[0]["trigger"], "scheduler")

    def test_does_not_run_when_not_due(self):
        calls = []
        ir.last_runs = lambda **k: [run_row(1)]
        ir.run_intelligence_refresh = lambda **kw: calls.append(kw)
        self.assertIsNone(scheduler.run_once())
        self.assertEqual(calls, [])

    def test_records_the_outcome_for_observability(self):
        ir.last_runs = lambda **k: []
        ir.run_intelligence_refresh = lambda **kw: {
            "run_id": "IR-42", "status": "partial", "finished_at": stamp(0)}
        scheduler.run_once()
        snap = scheduler.snapshot()
        self.assertEqual(snap["last_run_id"], "IR-42")
        self.assertEqual(snap["last_run_status"], "partial")
        self.assertEqual(snap["runs_started"], 1)

    def test_a_raising_refresh_is_swallowed_and_recorded(self):
        """The web process must survive any failure inside the job."""
        ir.last_runs = lambda **k: []

        def boom(**kw):
            raise RuntimeError("apify exploded")
        ir.run_intelligence_refresh = boom
        self.assertIsNone(scheduler.run_once())
        snap = scheduler.snapshot()
        self.assertEqual(snap["consecutive_errors"], 1)
        self.assertIn("apify exploded", snap["last_error"])

    def test_lockout_is_handled_cleanly(self):
        ir.last_runs = lambda **k: []
        ir.run_intelligence_refresh = lambda **kw: {
            "run_id": "IR-9", "status": "skipped", "locked_out": True,
            "reason": "another refresh is running (IR-8) — exiting cleanly"}
        report = scheduler.run_once()
        self.assertTrue(report["locked_out"])
        self.assertIn("another refresh is running", scheduler.snapshot()["last_decision"])

    def test_a_successful_run_clears_a_previous_error(self):
        ir.last_runs = lambda **k: []
        scheduler._set(consecutive_errors=3, last_error="old failure")
        ir.run_intelligence_refresh = lambda **kw: {
            "run_id": "IR-3", "status": "success", "finished_at": stamp(0)}
        scheduler.run_once()
        self.assertEqual(scheduler.snapshot()["consecutive_errors"], 0)
        self.assertEqual(scheduler.snapshot()["last_error"], "")


class TestStartGating(_Base):
    def test_not_started_when_scheduler_disabled(self):
        config.INTELLIGENCE_SCHEDULER_ENABLED = False
        state = scheduler.start()
        self.assertFalse(state["enabled"])
        self.assertIn("INTELLIGENCE_SCHEDULER_ENABLED", state["disabled_reason"])
        self.assertFalse(state["thread_alive"])

    def test_not_started_when_refresh_disabled(self):
        config.INTELLIGENCE_REFRESH_ENABLED = False
        state = scheduler.start()
        self.assertFalse(state["enabled"])
        self.assertIn("INTELLIGENCE_REFRESH_ENABLED", state["disabled_reason"])

    def test_not_started_without_sheets(self):
        """Nothing to refresh and nowhere to log — better to say so once than to
        fail every hour."""
        config.GOOGLE_SHEET_ID = ""
        state = scheduler.start()
        self.assertFalse(state["enabled"])
        self.assertIn("Google Sheets", state["disabled_reason"])

    def test_starts_and_is_idempotent_when_configured(self):
        config.INTELLIGENCE_SCHEDULER_STARTUP_DELAY_SECONDS = 3600   # never tick
        ir.last_runs = lambda **k: [run_row(1)]
        first = scheduler.start()
        self.assertTrue(first["enabled"])
        self.assertTrue(first["thread_alive"])
        before = threading.active_count()
        second = scheduler.start()
        self.assertTrue(second["thread_alive"])
        self.assertEqual(threading.active_count(), before,
                         "start() must not spawn a second thread")

    def test_thread_is_a_daemon_so_it_never_blocks_shutdown(self):
        config.INTELLIGENCE_SCHEDULER_STARTUP_DELAY_SECONDS = 3600
        ir.last_runs = lambda **k: [run_row(1)]
        scheduler.start()
        self.assertTrue(scheduler._THREAD.daemon)

    def test_stop_is_safe_when_never_started(self):
        scheduler.stop(timeout=1)          # must not raise


class TestInertUntilServing(unittest.TestCase):
    def test_importing_web_does_not_start_the_scheduler(self):
        """A CLI or a test importing `web` must never spend Apify/Gemini quota."""
        import web                                        # noqa: F401
        self.assertFalse(scheduler.snapshot()["thread_alive"])

    def test_startup_hook_is_registered(self):
        import web
        names = [getattr(h, "__name__", "") for h in
                 web.app.router.on_startup + web.app.router.on_shutdown]
        self.assertIn("_start_scheduler", names)
        self.assertIn("_stop_scheduler", names)

    def test_status_exposes_the_scheduler_and_the_build(self):
        """So "is the weekly job running?" is answerable without host access."""
        import json
        import web
        payload = json.loads(web.status().body)
        self.assertIn("scheduler", payload)
        self.assertIn("enabled", payload["scheduler"])
        self.assertIn("cadence_days", payload["scheduler"])
        self.assertIn("build", payload)
        self.assertIn("commit", payload["build"])
        self.assertEqual(payload["build"]["refresh_cadence_days"],
                         config.INTELLIGENCE_REFRESH_CADENCE_DAYS)

    def test_a_broken_scheduler_import_does_not_break_status(self):
        import json
        import web
        real = scheduler.snapshot

        def boom():
            raise RuntimeError("nope")
        scheduler.snapshot = boom
        try:
            payload = json.loads(web.status().body)
            self.assertFalse(payload["scheduler"]["enabled"])
            self.assertIn("unavailable", payload["scheduler"]["disabled_reason"])
        finally:
            scheduler.snapshot = real


if __name__ == "__main__":
    unittest.main()


class TestDiscoveryNoOpIsVisible(unittest.TestCase):
    """A weekly scrape with no ACTIVE query is a guaranteed no-op. Reporting it
    as "success, 0 discovered" is indistinguishable from a healthy run that
    found nothing new — so the schedule could look fine for months while
    scraping nothing."""

    def setUp(self):
        self._token = config.APIFY_TOKEN
        config.APIFY_TOKEN = "fake-token"

    def tearDown(self):
        config.APIFY_TOKEN = self._token

    class _Sheets:
        def __init__(self, rows):
            self._rows = rows

        def read_active_queries(self):
            return list(self._rows)

    def test_no_active_query_is_skipped_with_an_actionable_reason(self):
        stage = ir._external_discovery(dry_run=False, sheets=self._Sheets([]))
        self.assertEqual(stage["status"], "skipped")
        self.assertIn("no ACTIVE on-domain query", stage["reason"])
        self.assertIn("ACTIVE=TRUE", stage["reason"])
        self.assertEqual(stage["processed"], 0)

    def test_missing_token_is_still_reported_separately(self):
        config.APIFY_TOKEN = ""
        stage = ir._external_discovery(dry_run=False, sheets=self._Sheets([]))
        self.assertEqual(stage["status"], "skipped")
        self.assertIn("APIFY_TOKEN", stage["reason"])

    def test_an_active_query_is_not_skipped(self):
        rows = [{"QUERY_ID": "Q1", "QUERY": "goalkeeper landing technique drills",
                 "ACTIVE": "TRUE", "PLATFORM": "Instagram"}]
        called = []
        import inspiration_discovery
        real = inspiration_discovery.discover_inspiration
        inspiration_discovery.discover_inspiration = lambda **kw: (
            called.append(kw) or {"STATUS": "Success", "POSTS_DISCOVERED": 7,
                                  "POSTS_ADDED": 5, "POSTS_SKIPPED_EXISTING": 2,
                                  "CHANNELS_FAILED": 0})
        try:
            stage = ir._external_discovery(dry_run=False, sheets=self._Sheets(rows))
        finally:
            inspiration_discovery.discover_inspiration = real
        self.assertEqual(stage["status"], "success")
        self.assertEqual(stage["created"], 5)
        self.assertTrue(called, "discovery was never invoked")
