"""Tests for the Parents / Youth Internal Evidence Gap Audit.

Proves the audit detects a thin Parents/youth gap from real internal evidence,
that external inspiration can never close the gap or justify a profile, that no
winning profile is created without sufficient internal proof, that the proposed
tests are labelled evidence-building TESTS (not proven ideas), that Slack answers
are cautious and source-linked, and that only the EVIDENCE_GAPS artifact is ever
written (no internal/Notion/profile writes).

Run: python -m unittest tests.test_evidence_audit
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import evidence_audit as ea


def _row(icp, product, perf, link="", **sig):
    r = {"ICP": icp, "Product": product, "PERFORMANCE": perf,
         "LINK": link or f"https://www.instagram.com/storellisoccer/reel/{abs(hash((icp, product, perf))) % 9999}/",
         "Storytelling structure": ""}
    r.update(sig)
    return r


# Thin Parents evidence: 2 Great across DIFFERENT products (largest cluster = 1).
INTERNAL = [
    _row("Parents", "BodyShield NoBurn GK Leggings", "Great",
         signal_hook_curiosity_gap="1", signal_format_do_dont="1"),
    _row("Parents", "ExoShield Head Guards", "Great",
         signal_hook_curiosity_gap="1", signal_format_demo="1"),
    _row("Parents", "Pants & Leggings", "Underdog"),
    _row("Aspiring Pro", "Gloves", "Great"),
    _row("Adult Amateur", "Sliders", "Underdog"),
]

# Enough evidence: 3 Great in ONE product cluster + sample >= 4.
STRONG = [
    _row("Parents", "BodyShield GK Leggings", "Great", signal_hook_curiosity_gap="1"),
    _row("Parents", "BodyShield GK Leggings", "Great", signal_hook_curiosity_gap="1"),
    _row("Parents", "BodyShield GK Leggings", "Great", signal_format_demo="1"),
    _row("Parents", "BodyShield GK Leggings", "Ok"),
]


class FakeSheets:
    def __init__(self):
        self.gap_writes = 0
        self.other_writes = 0
        self._gaps = []
        self._tracker = []

    def read_profiles(self):
        return []                          # no Parents profile exists

    def read_calendar_ratings(self):
        return [{"CALENDAR_TITLE": "Back-to-School", "ICP": "Parents", "RECOMMENDATION": "Revise"}]

    def read_content_rows(self):
        return [{"USE_FOR_IDEA_GEN": "TRUE"}] * 40   # external refs exist (reference only)

    def ensure_evidence_gaps_tab(self):
        return False

    def ensure_evidence_test_tracker_tab(self):
        return False

    def upsert_evidence_gaps(self, gaps):
        self.gap_writes += 1
        self._gaps = gaps
        return len(gaps), 0

    def seed_evidence_tests(self, rows):
        # create-if-absent semantics: only append TEST_IDs not already present
        have = {r["TEST_ID"] for r in self._tracker}
        new = [r for r in rows if r["TEST_ID"] not in have]
        self._tracker.extend(new)
        return len(new), len(rows) - len(new)

    def __getattr__(self, name):
        # Any profile/internal/other write must never happen.
        if name.startswith("upsert_") or name.startswith("update_") or name.startswith("append_"):
            def _w(*a, **k):
                self.other_writes += 1
                return (0, 0)
            return _w
        if name.startswith("read_"):
            return lambda *a, **k: []
        if name.startswith("ensure_"):
            return lambda *a, **k: False
        raise AttributeError(name)


class TestAudit(unittest.TestCase):
    def test_thin_parents_gap_detected(self):
        a = ea.audit(INTERNAL)
        self.assertEqual(a["parents_rows"], 3)             # 2 Parents + Pants row
        self.assertEqual(a["great"], 2)
        self.assertEqual(a["top_cluster_great"], 1)        # split across products
        justified, reason = ea.profile_justified(a)
        self.assertFalse(justified)
        self.assertIn("thin", reason.lower())

    def test_profile_justified_only_with_real_cluster(self):
        justified, _ = ea.profile_justified(ea.audit(STRONG))
        self.assertTrue(justified)                          # 3 Great in one product cluster

    def test_external_cannot_close_gap(self):
        a = ea.audit(INTERNAL)
        low = ea.evidence_gaps(a, INTERNAL, external_use_count=0)
        high = ea.evidence_gaps(a, INTERNAL, external_use_count=500)
        parents_low = next(g for g in low if g["GAP_NAME"].startswith("Parents"))
        parents_high = next(g for g in high if g["GAP_NAME"].startswith("Parents"))
        # internal evidence count + confidence are identical regardless of external volume
        self.assertEqual(parents_low["CURRENT_INTERNAL_EVIDENCE_COUNT"],
                         parents_high["CURRENT_INTERNAL_EVIDENCE_COUNT"])
        self.assertEqual(parents_high["CURRENT_CONFIDENCE"], "None")
        self.assertIn("not proof", parents_high["EXISTING_EXTERNAL_REFERENCES"].lower())

    def test_tests_are_labelled_tests_not_ideas(self):
        for t in ea.evidence_building_tests():
            self.assertEqual(t["label"], "evidence-building test")
            self.assertIn("hypothesis", t)
            self.assertIn("success_proves", t)
            self.assertIn("failure_means", t)

    def test_run_audit_writes_only_gaps_no_profile(self):
        sheets = FakeSheets()
        r = ea.run_audit(sheets=sheets, internal_rows=INTERNAL)
        self.assertFalse(r["profile_justified"])           # no profile justified
        self.assertEqual(sheets.gap_writes, 1)             # only the EVIDENCE_GAPS artifact
        self.assertEqual(sheets.other_writes, 0)           # no profile / internal writes
        self.assertEqual(len(r["gaps"]), 5)
        self.assertTrue(any(g["GAP_NAME"] == "Parents / Youth Safety Proof" for g in r["gaps"]))

    def test_tracker_has_three_tests_plus_control(self):
        rows = ea.test_tracker_rows()
        self.assertEqual(len(rows), 4)
        controls = [r for r in rows if r["IS_CONTROL"] == "TRUE"]
        self.assertEqual(len(controls), 1)                 # exactly one baseline
        self.assertNotIn("Parents", controls[0]["ICP"])    # control is NOT tagged Parents
        self.assertTrue(all(r["PERFORMANCE_GRADE"] == "" for r in rows))   # results left blank
        self.assertTrue(all(r["STATUS"] == "Planned" for r in rows))

    def test_tracker_seed_is_create_if_absent(self):
        sheets = FakeSheets()
        c1, s1 = ea.seed_test_tracker(sheets)
        self.assertEqual((c1, s1), (4, 0))                 # first run creates 4
        # simulate the user logging a result on one row
        sheets._tracker[0]["PERFORMANCE_GRADE"] = "Great"
        c2, s2 = ea.seed_test_tracker(sheets)
        self.assertEqual((c2, s2), (0, 4))                 # re-seed creates nothing
        self.assertEqual(sheets._tracker[0]["PERFORMANCE_GRADE"], "Great")  # result preserved


class TestSlack(unittest.TestCase):
    def setUp(self):
        self._real = ea._load_audit
        ea._load_audit = lambda sheets=None: (ea.audit(INTERNAL), INTERNAL, "")

    def tearDown(self):
        ea._load_audit = self._real

    def test_routing(self):
        self.assertTrue(ea.is_evidence_gap_query("should we make Parents content?"))
        self.assertTrue(ea.is_evidence_gap_query("what proof are we missing?"))
        self.assertTrue(ea.is_evidence_gap_query("what Parents/youth tests should we run?"))
        self.assertTrue(ea.is_evidence_gap_query("what would we need to prove before scaling Parents?"))
        self.assertFalse(ea.is_evidence_gap_query("give me 5 BodyShield ideas"))

    def test_should_we_make_parents_is_cautious(self):
        out = ea.answer_evidence_gap("should we make Parents content?")
        self.assertIn("not proven yet", out.lower())
        self.assertIn("test", out.lower())
        self.assertIn("*Why:*", out)                       # a trace
        self.assertIn("not proof", out.lower())            # external reference-only

    def test_what_proof_missing_returns_trace(self):
        out = ea.answer_evidence_gap("what proof are we missing?")
        self.assertIn("*Internal proof:*", out)
        self.assertIn("*Test needed:*", out)
        self.assertIn("[S1]", out)                          # linked internal proof (pre-format letter)

    def test_test_plan_labels_tests(self):
        out = ea.answer_evidence_gap("what Parents/youth tests should we run?")
        self.assertIn("Test 1:", out)
        self.assertIn("not proven ideas", out.lower())

    def test_logging_convention_is_consistent(self):
        c = ea.logging_convention()
        for key in ("views", "retention", "saves", "shares", "comments", "parent_intent"):
            self.assertIn(key, c["format"])
        self.assertEqual(set(c["primary_kpi"]), {"Parent POV", "Before/After", "Coach-Trust", "Control"})

    def test_slack_logging_convention(self):
        self.assertTrue(ea.is_evidence_gap_query("how do we log the Parents tests?"))
        out = ea.answer_evidence_gap("how do we measure the Parents tests?")
        self.assertIn("SAVES_OR_KPI", out)
        self.assertIn("retention", out.lower())
        self.assertIn("control", out.lower())            # graded against the baseline
        self.assertIn("7 days", out.lower())


if __name__ == "__main__":
    unittest.main()
