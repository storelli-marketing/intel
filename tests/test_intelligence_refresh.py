"""Tests for the self-updating social intelligence scheduler.

Covers the orchestration contract (Part M): bounded/queued query selection,
change-detection, idea-regen recommendation (never auto-regeneration), no-op
success, partial failure, quota-stop respect, locking + stale-lock recovery,
run-history, evidence-loop isolation, and the Slack refresh-status answers.
Existing suites (444 + golden + multi-turn) stay green.

Run: python -m unittest tests.test_intelligence_refresh
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import intelligence_refresh as ir
import query_economics as qe


class FakeSheets:
    class _WS:
        class spreadsheet:
            pass
    ws = _WS()

    def read_active_queries(self):
        return []


# --------------------------------------------------------------------------- #
# Part E — query economics
# --------------------------------------------------------------------------- #
class TestQueryEconomics(unittest.TestCase):
    def test_family_classification(self):
        self.assertEqual(qe.classify_family({"QUERY": "goalkeeper diving landing training"})[0], "good")
        self.assertEqual(qe.classify_family({"QUERY": "motocross protection gear", "RESEARCH_RING": "3"})[0], "bad")
        self.assertEqual(qe.classify_family({"QUERY": "mindset confidence hack for athletes"})[0], "bad")
        self.assertEqual(qe.classify_family({"QUERY": "generic protection gear", "RESEARCH_RING": "5"})[0], "bad")

    def test_utility_formula(self):
        # 40% quality + 30% new-row + 20% connection + 10% novelty
        s = {"results_found": 10, "new_rows_added": 6, "quality_80_plus": 4, "quality_70_plus": 0,
             "connection_usage_count": 2, "duplicates": 1}
        self.assertAlmostEqual(qe.query_utility(s), round(100 * (0.40 * 0.4 + 0.30 * 0.6 + 0.20 * (2 / 3) + 0.10 * 0.9), 1), places=1)
        self.assertEqual(qe.query_utility({"results_found": 0}), 0.0)   # unproven -> 0

    def test_bad_family_paused_good_selected(self):
        queries = [{"QUERY_ID": "q1", "QUERY": "goalkeeper mistakes training"},
                   {"QUERY_ID": "q2", "QUERY": "motocross gear", "RESEARCH_RING": "3"},
                   {"QUERY_ID": "q3", "QUERY": "youth soccer safety coach"}]
        sel = qe.select_active(queries, max_n=12)
        sel_ids = {q["QUERY_ID"] for q in sel["selected"]}
        self.assertIn("q1", sel_ids)
        self.assertIn("q3", sel_ids)
        self.assertNotIn("q2", sel_ids)                 # bad family paused, never selected
        self.assertEqual(len(sel["paused"]), 1)

    def test_bounded_selection(self):
        queries = [{"QUERY_ID": f"q{i}", "QUERY": "goalkeeper diving save training"} for i in range(30)]
        sel = qe.select_active(queries, max_n=12)
        self.assertLessEqual(len(sel["selected"]), 12)   # bounded


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
class OrchBase(unittest.TestCase):
    def setUp(self):
        self._orig = {k: getattr(ir, k) for k in (
            "_internal_append", "_internal_metrics", "_internal_analyze", "_internal_recompute",
            "_external_discovery", "_external_analyze", "_external_match",
            "_external_quality", "_external_connections", "_write_run_row",
            "_lock_active", "_read_runs")}
        self.written = []
        ir._write_run_row = lambda sheets, row, update_run_id=None: self.written.append(row)
        ir._lock_active = lambda s: None
        ir._read_runs = lambda s: []
        # default: everything succeeds with a material change
        ir._internal_append = lambda dry: {"stage": "internal_append", "status": "success", "created": 4, "_appended": 4, "_new_media": 4}
        ir._internal_metrics = lambda dry: {"stage": "internal_metrics", "status": "success", "updated": 12, "_new_media": 2}
        ir._internal_analyze = lambda dry, limit: {"stage": "internal_analyze", "status": "success", "created": 3, "_analyzed": 3}
        ir._internal_recompute = lambda dry: {"stage": "internal_recompute", "status": "success", "created": 1, "updated": 2, "_correlations_rebuilt": True, "_profiles_created": 1, "_profiles_updated": 2}
        ir._external_discovery = lambda dry, s: {"stage": "external_discovery", "status": "success", "processed": 40, "created": 31, "_added": 31}
        ir._external_analyze = lambda dry, s: {"stage": "external_analyze", "status": "success", "created": 31}
        ir._external_match = lambda dry, s: {"stage": "external_match", "status": "success", "updated": 20, "created": 8}
        ir._external_quality = lambda dry, s: {"stage": "external_quality", "status": "success", "created": 6, "_quality_80": 6}
        ir._external_connections = lambda dry, s: {"stage": "external_connections", "status": "success", "created": 2, "updated": 1, "_created": 2}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(ir, k, v)

    def run_refresh(self, **kw):
        kw.setdefault("sheets", FakeSheets())
        return ir.run_intelligence_refresh(**kw)


class TestOrchestration(OrchBase):
    def test_full_run_all_stages_and_regen(self):
        rep = self.run_refresh(mode="full", trigger="test")
        names = [s["stage"] for s in rep["stages"]]
        self.assertEqual(names, ["internal_append", "internal_metrics", "internal_analyze",
                                 "internal_recompute", "external_discovery", "external_analyze",
                                 "external_match", "external_quality", "external_connections"])
        self.assertEqual(rep["status"], "success")
        self.assertTrue(rep["should_regenerate_ideas"])
        self.assertEqual(len(self.written), 2)          # lock row + final history row

    def test_change_detection_skips_recompute(self):
        ir._internal_append = lambda dry: {"stage": "internal_append", "status": "skipped", "_appended": 0}
        ir._internal_metrics = lambda dry: {"stage": "internal_metrics", "status": "success", "updated": 0}
        ir._internal_analyze = lambda dry, limit: {"stage": "internal_analyze", "status": "skipped", "_analyzed": 0}
        rep = self.run_refresh(mode="internal", trigger="test")
        rec = next(s for s in rep["stages"] if s["stage"] == "internal_recompute")
        self.assertEqual(rec["status"], "skipped")

    def test_ideas_never_auto_regenerate(self):
        rep = self.run_refresh(mode="full", trigger="test")
        names = [s["stage"] for s in rep["stages"]]
        self.assertNotIn("generate_ideas", " ".join(names))
        self.assertIn("should_regenerate_ideas", rep)   # it's a recommendation field only

    def test_connections_skip_when_no_new_quality(self):
        ir._external_discovery = lambda dry, s: {"stage": "external_discovery", "status": "success", "created": 0, "_added": 0}
        ir._external_quality = lambda dry, s: {"stage": "external_quality", "status": "success", "created": 0, "_quality_80": 0}
        rep = self.run_refresh(mode="external", trigger="test")
        conn = next(s for s in rep["stages"] if s["stage"] == "external_connections")
        self.assertEqual(conn["status"], "skipped")

    def test_partial_failure_continues(self):
        def boom(dry, s):
            raise RuntimeError("apify blew up")
        ir._external_discovery = boom
        rep = self.run_refresh(mode="full", trigger="test")
        disc = next(s for s in rep["stages"] if s["stage"] == "external_discovery")
        self.assertEqual(disc["status"], "failed")       # isolated failure
        # internal stages still ran and succeeded
        self.assertEqual(next(s for s in rep["stages"] if s["stage"] == "internal_analyze")["status"], "success")
        self.assertEqual(rep["status"], "partial")

    def test_quota_stop_respected(self):
        ir._internal_analyze = lambda dry, limit: {"stage": "internal_analyze", "status": "partial",
                                                   "created": 5, "reason": "Gemini quota stop", "_analyzed": 5}
        rep = self.run_refresh(mode="internal", trigger="test")
        a = next(s for s in rep["stages"] if s["stage"] == "internal_analyze")
        self.assertEqual(a["status"], "partial")
        self.assertIn("quota", a["reason"].lower())

    def test_no_op_success(self):
        ir._internal_append = lambda dry: {"stage": "internal_append", "status": "skipped", "_appended": 0}
        ir._internal_analyze = lambda dry, limit: {"stage": "internal_analyze", "status": "skipped", "_analyzed": 0}
        ir._internal_metrics = lambda dry: {"stage": "internal_metrics", "status": "skipped", "updated": 0}
        rep = self.run_refresh(mode="internal", trigger="test")
        self.assertEqual(rep["status"], "success")       # a quiet no-op is a success

    def test_dry_run_writes_nothing_no_lock(self):
        rep = self.run_refresh(mode="full", dry_run=True, trigger="test")
        self.assertTrue(rep["dry_run"])
        self.assertEqual(self.written, [])               # no lock row, no history row

    def test_lock_blocks_overlap(self):
        ir._lock_active = lambda s: {"RUN_ID": "IR-prev"}
        rep = self.run_refresh(mode="full", trigger="test")
        self.assertTrue(rep["locked_out"])
        self.assertEqual(rep["status"], "skipped")
        self.assertEqual(self.written, [])               # never started

    def test_evidence_loops_isolated(self):
        # internal loop never contains an external stage and vice versa
        internal = self.run_refresh(mode="internal", trigger="test")
        external = self.run_refresh(mode="external", trigger="test")
        self.assertTrue(all(s["stage"].startswith("internal_") for s in internal["stages"]))
        self.assertTrue(all(s["stage"].startswith("external_") for s in external["stages"]))

    def test_history_row_built(self):
        rep = self.run_refresh(mode="full", trigger="test")
        row = ir._history_row(rep)
        self.assertEqual(row["INTERNAL_ANALYZED"], 3)
        self.assertEqual(row["EXTERNAL_ADDED"], 31)
        self.assertEqual(row["CONNECTIONS_CREATED"], 2)
        self.assertTrue(row["IDEA_REGEN_RECOMMENDED"])
        self.assertEqual(row["STATUS"], "success")
        # no secret-shaped strings anywhere
        import re
        blob = " ".join(str(v) for v in row.values())
        self.assertNotRegex(blob, r"\b(EAA|IGQ|xox[bp]|sk-|AIza)[A-Za-z0-9_-]{12,}")


class TestStaleLock(unittest.TestCase):
    def test_stale_lock_recovered(self):
        old = "2020-01-01 00:00 UTC"                     # far past -> stale
        orig = ir._read_runs
        try:
            ir._read_runs = lambda s: [{"RUN_ID": "IR-old", "STATUS": "running", "STARTED_AT": old}]
            self.assertIsNone(ir._lock_active(None))     # stale -> not active
            from datetime import datetime, timezone
            fresh = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            ir._read_runs = lambda s: [{"RUN_ID": "IR-now", "STATUS": "running", "STARTED_AT": fresh}]
            self.assertIsNotNone(ir._lock_active(None))  # fresh -> active
        finally:
            ir._read_runs = orig


# --------------------------------------------------------------------------- #
# Part K — Slack refresh-status
# --------------------------------------------------------------------------- #
class TestSlackStatus(unittest.TestCase):
    def setUp(self):
        import social_analytics  # noqa: F401
        self._orig = ir.last_runs
        ir.last_runs = lambda n=1: [{
            "FINISHED_AT": "2026-08-12 09:00 UTC", "STATUS": "success",
            "INTERNAL_NEW_MEDIA": "7", "INTERNAL_ANALYZED": "7", "EXTERNAL_ADDED": "31",
            "EXTERNAL_QUALITY_80": "6", "PROFILES_UPDATED": "0", "IDEA_REGEN_RECOMMENDED": "False"}]

    def tearDown(self):
        ir.last_runs = self._orig

    def _ask(self, q):
        import social_analytics as sa
        return sa.answer_social_analytics_question(q)

    def test_last_update(self):
        out = self._ask("when did the brain last update?")
        self.assertIn("2026-08-12", out)
        self.assertIn("31", out)

    def test_should_regenerate(self):
        out = self._ask("should we regenerate ideas?")
        self.assertIn("Not yet", out)

    def test_new_inspiration_is_reference_only(self):
        out = self._ask("what new inspiration did we find?")
        self.assertIn("31", out)
        low = out.lower()
        self.assertIn("reference", low)
        self.assertIn("not proof", low)                 # external never becomes proof

    def test_no_history_is_honest(self):
        ir.last_runs = lambda n=1: []
        out = self._ask("when did the brain last update?")
        self.assertIn("hasn't run", out.lower())


if __name__ == "__main__":
    unittest.main()
