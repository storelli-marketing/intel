"""The weekly digest: what changed, pushed where people read it.

`render_report` already existed but is CLI-shaped — one line per stage including
the ones that did nothing. These lock the properties that make the digest worth
reading on a Monday, and the ones that keep it honest: no invented numbers,
external inspiration never described as proof, a failure always named, and
delivery that can never turn a successful refresh into a failed one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import refresh_digest as rd


def report(status="success", **stages):
    return {"run_id": "IR-t", "status": status,
            "stages": [dict(stage=k, **v) for k, v in stages.items()]}


class TestDigestContent(unittest.TestCase):
    def test_busy_week_names_each_half_with_real_numbers(self):
        out = rd.build_digest(report(
            owned_scan={"status": "success", "_new_media": 4, "updated": 26},
            internal_analyze={"status": "success", "created": 3},
            external_discovery={"status": "success", "processed": 62, "created": 18},
            external_quality={"status": "success", "_quality_80": 5},
            internal_recompute={"status": "success", "_correlations_rebuilt": True,
                                "_profiles_updated": 2}))
        self.assertIn("4 new reels picked up", out)
        self.assertIn("3 analyzed", out)
        self.assertIn("18 new candidates saved from 62 scanned", out)
        self.assertIn("5 clean enough to build on", out)
        self.assertIn("patterns recomputed", out)
        self.assertIn("2 winning profiles updated", out)

    def test_external_counts_are_never_called_proof(self):
        out = rd.build_digest(report(
            external_discovery={"status": "success", "created": 9},
            external_quality={"status": "success", "_quality_80": 4}))
        self.assertIn("reference only", out)
        self.assertNotRegex(out, r"(?i)external[^.]{0,40}prov(e|es|en)")

    def test_a_quiet_week_says_so_instead_of_padding(self):
        out = rd.build_digest(report(
            owned_scan={"status": "success", "_new_media": 0}))
        self.assertIn("nothing new this week", out)
        self.assertIn("conclusions unchanged", out)

    def test_held_reels_are_explained_not_hidden(self):
        out = rd.build_digest(report(
            internal_analyze={"status": "success", "created": 1, "_held_too_recent": 3}))
        self.assertIn(f"3 waiting out the {config.ANALYSIS_MIN_AGE_DAYS}-day window", out)

    def test_a_failed_stage_is_always_named(self):
        """A quiet weekly failure is how a pipeline dies unnoticed."""
        out = rd.build_digest(report(
            status="partial",
            internal_analyze={"status": "failed", "reason": "Gemini quota exhausted"}))
        self.assertIn("with gaps", out)
        self.assertIn("Needs you", out)
        self.assertIn("internal_analyze", out)
        self.assertIn("Gemini quota", out)

    def test_idea_regeneration_is_surfaced_as_a_human_step(self):
        r = report(internal_recompute={"status": "success", "_profiles_updated": 1})
        r["should_regenerate_ideas"] = True
        r["idea_regen_reasons"] = ["new internal evidence"]
        out = rd.build_digest(r)
        self.assertIn("regenerating ideas", out)
        self.assertIn("never automatic", out)

    def test_no_active_query_reason_reaches_the_reader(self):
        out = rd.build_digest(report(external_discovery={
            "status": "skipped", "processed": 0,
            "reason": "no ACTIVE on-domain query in APIFY_DISCOVERY_QUERIES — set ACTIVE=TRUE"}))
        self.assertIn("ACTIVE=TRUE", out)

    def test_lockout_is_one_line_not_a_full_report(self):
        out = rd.build_digest({"run_id": "IR-x", "locked_out": True,
                               "reason": "another refresh is running (IR-w)"})
        self.assertIn("skipped", out)
        self.assertIn("another refresh is running", out)
        self.assertNotIn("Our feed", out)

    def test_health_warnings_are_appended(self):
        out = rd.build_digest(report(owned_scan={"status": "success"}),
                              {"state": "STALE", "reasons": ["last refresh 21d ago"]})
        self.assertIn("last refresh 21d ago", out)

    def test_empty_report_renders_nothing(self):
        self.assertEqual(rd.build_digest({}), "")
        self.assertEqual(rd.build_digest(None), "")

    def test_no_number_appears_that_the_report_did_not_carry(self):
        out = rd.build_digest(report(owned_scan={"status": "success", "_new_media": 2}))
        self.assertIn("2 new reels", out)
        for ghost in ("3 analyzed", "5 clean", "winning profiles updated"):
            self.assertNotIn(ghost, out)


class TestDelivery(unittest.TestCase):
    def setUp(self):
        import scheduler
        self._cfg = {k: getattr(config, k) for k in
                     ("SLACK_WEBHOOK_URL", "DIGEST_EMAIL_TO", "SMTP_HOST", "SMTP_FROM")}
        config.SLACK_WEBHOOK_URL = ""
        config.DIGEST_EMAIL_TO = ""
        config.SMTP_HOST = ""
        config.SMTP_FROM = ""
        # Two of these drive scheduler.run_once(), which mutates the scheduler's
        # module-level STATE (runs_started, last_digest_*). Whoever mutates shared
        # state restores it — otherwise this leaks into test_scheduler's counters
        # and the suite fails only when run in a particular order.
        self._sched_state = dict(scheduler.STATE)

    def tearDown(self):
        import scheduler
        for k, v in self._cfg.items():
            setattr(config, k, v)
        scheduler.STATE.clear()
        scheduler.STATE.update(self._sched_state)

    def test_nothing_configured_is_a_no_op_not_an_error(self):
        out = rd.deliver(report(owned_scan={"status": "success", "_new_media": 1}))
        self.assertEqual(out["slack"], "not_configured")
        self.assertEqual(out["email"], "not_configured")
        self.assertTrue(out["text"])

    def test_email_never_sends_without_full_smtp_config(self):
        config.DIGEST_EMAIL_TO = "team@example.com"       # host/from still unset
        self.assertFalse(rd.email_configured())
        self.assertFalse(rd.send_email("s", "b"))

    def test_slack_posts_when_configured(self):
        import slack_report
        config.SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/fake"
        sent = []
        real = slack_report.post
        slack_report.post = lambda text: sent.append(text) or 200
        try:
            out = rd.deliver(report(owned_scan={"status": "success", "_new_media": 1}))
        finally:
            slack_report.post = real
        self.assertEqual(out["slack"], "posted")
        self.assertIn("1 new reel picked up", sent[0])

    def test_a_slack_failure_is_recorded_and_never_raises(self):
        import slack_report
        config.SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/fake"
        real = slack_report.post

        def boom(text):
            raise RuntimeError("slack down")
        slack_report.post = boom
        try:
            out = rd.deliver(report(owned_scan={"status": "success", "_new_media": 1}))
        finally:
            slack_report.post = real
        self.assertEqual(out["slack"], "failed")

    def test_scheduler_delivers_the_digest_after_a_run(self):
        import intelligence_refresh as ir
        import scheduler
        real_runs, real_run = ir.last_runs, ir.run_intelligence_refresh
        real_deliver, real_health = rd.deliver, ir.health_state
        delivered = []
        ir.last_runs = lambda **k: []
        ir.run_intelligence_refresh = lambda **kw: {
            "run_id": "IR-d", "status": "success", "finished_at": "2026-09-02 15:00 UTC"}
        ir.health_state = lambda *a, **k: {"state": "HEALTHY", "reasons": []}
        rd.deliver = lambda rep, health=None: (delivered.append(rep)
                                               or {"slack": "posted", "email": "sent"})
        try:
            scheduler.run_once()
        finally:
            ir.last_runs, ir.run_intelligence_refresh = real_runs, real_run
            rd.deliver, ir.health_state = real_deliver, real_health
        self.assertEqual(delivered[0]["run_id"], "IR-d")
        snap = scheduler.snapshot()
        self.assertEqual(snap["last_digest_slack"], "posted")
        self.assertEqual(snap["last_digest_email"], "sent")

    def test_a_digest_failure_does_not_fail_the_refresh(self):
        import intelligence_refresh as ir
        import scheduler
        real_runs, real_run, real_deliver = (ir.last_runs, ir.run_intelligence_refresh,
                                             rd.deliver)
        ir.last_runs = lambda **k: []
        ir.run_intelligence_refresh = lambda **kw: {"run_id": "IR-e", "status": "success"}

        def boom(rep, health=None):
            raise RuntimeError("digest exploded")
        rd.deliver = boom
        try:
            out = scheduler.run_once()
        finally:
            ir.last_runs, ir.run_intelligence_refresh = real_runs, real_run
            rd.deliver = real_deliver
        self.assertEqual(out["status"], "success", "the refresh itself must still succeed")


if __name__ == "__main__":
    unittest.main()
