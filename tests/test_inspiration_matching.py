"""Tests for Milestone 3B — matching external inspiration to winning profiles.

Proves eligibility gating, taxonomy-driven MATCH_SCORE, priority-as-secondary
FINAL_SCORE, shortlist gating, idempotent writeback, and full isolation from
internal Storelli proof.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import inspiration_matcher as im
from inspiration_matcher import (active_profiles, eligible_external, final_score,
                                 match_inspiration, match_row, match_score,
                                 novelty_score)
from inspiration_sheets import SOURCE_TYPE_EXTERNAL
import performance
import correlations as corr
from sheets_client import SheetsClient
import taxonomy

A_SIGNAL_COL = taxonomy.all_signal_columns()[0]


def _profile(pid="WFP-gloves-aspiring_pro", active="TRUE", conf="High", **over):
    p = {"PROFILE_ID": pid, "ACTIVE": active, "CONFIDENCE": conf,
         "PROFILE_NAME": "Gloves / Aspiring Pro: Education + Tutorial",
         "PRODUCT": "Gloves", "ICP": "Aspiring Pro",
         "HOOK_TAGS": "Education, Fear / Risk", "FORMAT_TAGS": "Tutorial, Demo",
         "VISUAL_STYLE_TAGS": "Action", "PROBLEM_TAGS": "Chronic Pain",
         "SOLUTION_TAGS": "Prevention", "FUNNEL_STAGE_TAGS": "Consideration",
         "INTERNAL_SAMPLE_SIZE": "5", "SUPPORTING_VIDEO_URLS": "https://ig/1/;https://ig/2/",
         "SUPPORTING_LEARNING_IDS": "signal_hook_education;signal_format_tutorial"}
    p.update(over)
    return p


def _ext(**over):
    r = {"_row": 3, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL, "SAFETY_STATUS": "Safe",
         "ANALYSIS_STATUS": "Analyzed", "POST_URL": "https://tiktok/x",
         "HANDLE": "gk_coach", "CAPTION": "goalkeeper tutorial how to improve",
         "HOOK_TAGS": "Education, Fear / Risk", "FORMAT_TAGS": "Tutorial",
         "VISUAL_STYLE_TAGS": "Action", "PROBLEM_TAGS": "Chronic Pain",
         "SOLUTION_TAGS": "Prevention", "FUNNEL_STAGE_TAGS": "Consideration",
         "PRIORITY_SCORE": "0.8"}
    r.update(over)
    return r


class TestEligibility(unittest.TestCase):
    def test_safe_analyzed_external_eligible(self):
        self.assertTrue(eligible_external(_ext()))

    def test_rejected_skipped_unanalyzed_ignored(self):
        self.assertFalse(eligible_external(_ext(SAFETY_STATUS="Rejected")))
        self.assertFalse(eligible_external(_ext(ANALYSIS_STATUS="Skipped")))
        self.assertFalse(eligible_external(_ext(ANALYSIS_STATUS="Not Analyzed")))
        self.assertFalse(eligible_external(_ext(SOURCE_TYPE="INTERNAL")))

    def test_active_profiles_filter(self):
        good = _profile()
        self.assertEqual(len(active_profiles([good])), 1)
        self.assertEqual(active_profiles([_profile(active="FALSE")]), [])
        self.assertEqual(active_profiles([_profile(conf="Low")]), [])
        self.assertEqual(active_profiles([_profile(INTERNAL_SAMPLE_SIZE="")]), [])
        self.assertEqual(active_profiles([_profile(SUPPORTING_VIDEO_URLS="",
                                                   SUPPORTING_LEARNING_IDS="")]), [])


class TestScoring(unittest.TestCase):
    def test_match_score_is_taxonomy_based_not_engagement(self):
        p = _profile()
        base, _ = match_score(_ext(), p)
        # Changing views / priority must NOT change MATCH_SCORE.
        hi, _ = match_score(_ext(VIEW_COUNT="9999999", PRIORITY_SCORE="0.99"), p)
        self.assertEqual(base, hi)
        self.assertGreater(base, 60)     # strong taxonomy overlap -> high match

    def test_low_overlap_low_match(self):
        p = _profile()
        weak = _ext(HOOK_TAGS="Humor", FORMAT_TAGS="Reaction", VISUAL_STYLE_TAGS="Polished",
                    PROBLEM_TAGS="Latent", SOLUTION_TAGS="Fix", FUNNEL_STAGE_TAGS="Retention")
        score, _ = match_score(weak, p)
        self.assertLess(score, 30)

    def test_final_uses_priority_only_as_secondary(self):
        # Same match+novelty; priority differs -> FINAL differs only within 15%.
        f_lo = final_score(80.0, 40.0, 0.0)
        f_hi = final_score(80.0, 40.0, 1.0)
        self.assertAlmostEqual(f_hi - f_lo, 15.0, delta=0.1)   # priority band = 15
        # A high-priority but low-match row cannot be lifted to a strong final.
        weak_final = final_score(20.0, 10.0, 1.0)
        self.assertLess(weak_final, 40)

    def test_final_without_priority_renormalizes(self):
        f = final_score(80.0, 40.0, None)   # manual-queue row, no priority
        self.assertAlmostEqual(f, (0.70*80 + 0.15*40) / 0.85, places=1)

    def test_novelty_high_for_same_mechanism_fresh_execution(self):
        p = _profile()
        # Same problem/solution/funnel, different hook/format/visual.
        fresh = _ext(HOOK_TAGS="Curiosity Gap", FORMAT_TAGS="POV", VISUAL_STYLE_TAGS="Raw / UGC")
        copy = _ext()   # identical execution
        self.assertGreater(novelty_score(fresh, p), novelty_score(copy, p))


class TestMatchRowAndShortlist(unittest.TestCase):
    def test_strong_row_shortlisted(self):
        cells = match_row(_ext(), [_profile()])
        self.assertEqual(cells["SHORTLISTED"], "TRUE")
        self.assertEqual(cells["BEST_MATCHED_PROFILE_ID"], "WFP-gloves-aspiring_pro")
        self.assertGreaterEqual(float(cells["MATCH_SCORE"]), 60)
        self.assertIn("signal_hook_education", cells["MATCHED_INTERNAL_LEARNING_IDS"])

    def test_weak_match_not_shortlisted(self):
        weak = _ext(HOOK_TAGS="Humor", FORMAT_TAGS="Reaction", VISUAL_STYLE_TAGS="Polished",
                    PROBLEM_TAGS="Latent", SOLUTION_TAGS="Fix", FUNNEL_STAGE_TAGS="Retention",
                    PRIORITY_SCORE="0.99")
        cells = match_row(weak, [_profile()])
        self.assertEqual(cells["SHORTLISTED"], "FALSE")
        self.assertIn("MATCH_SCORE", cells["SHORTLIST_REASON"])

    def test_famous_player_match_content_not_shortlisted(self):
        risky = _ext(CAPTION="Messi highlights vs Real Madrid full match")
        cells = match_row(risky, [_profile()])
        self.assertEqual(cells["SHORTLISTED"], "FALSE")
        self.assertIn("Excluded", cells["SHORTLIST_REASON"])

    def test_no_active_profiles_handled(self):
        cells = match_row(_ext(), [_profile(active="FALSE")])
        self.assertEqual(cells["SHORTLISTED"], "FALSE")
        self.assertIn("No active", cells["SHORTLIST_REASON"])


class FakeMatchSheets:
    def __init__(self, rows, profiles):
        self._rows = rows
        self._profiles = profiles
        self.updates = {}       # row -> cells
        self.runs = []
        self.profile_writes = 0

    def ensure_content_columns(self, cols):
        return []

    def read_profiles(self):
        return list(self._profiles)

    def read_content_rows(self):
        return list(self._rows)

    def update_content_cells(self, row_index, values):
        self.updates[row_index] = dict(values)

    def update_content_cells_bulk(self, updates):
        for row_index, values in updates:
            self.updates[row_index] = dict(values)

    def append_run(self, run):
        self.runs.append(run)

    # If matching ever tried to write a profile, this would be called; it isn't.
    def upsert_profiles(self, profiles):
        self.profile_writes += 1


class TestOrchestrator(unittest.TestCase):
    def test_only_eligible_rows_written_and_run_logged(self):
        rows = [
            _ext(),                                   # eligible
            _ext(SAFETY_STATUS="Rejected"),           # ignored
            _ext(ANALYSIS_STATUS="Not Analyzed"),     # ignored
        ]
        # give each a distinct row index
        for i, r in enumerate(rows, start=3):
            r["_row"] = i
        sheets = FakeMatchSheets(rows, [_profile()])
        run = match_inspiration(sheets=sheets)
        self.assertEqual(run["RUN_TYPE"], "Match")
        self.assertEqual(run["POSTS_DISCOVERED"], 1)      # only 1 eligible
        self.assertEqual(set(sheets.updates), {3})        # only eligible row written
        self.assertEqual(run["POSTS_SHORTLISTED"], 1)
        self.assertEqual(len(sheets.runs), 1)

    def test_idempotent_rerun(self):
        rows = [_ext()]
        sheets = FakeMatchSheets(rows, [_profile()])
        match_inspiration(sheets=sheets)
        first = dict(sheets.updates[3])
        match_inspiration(sheets=sheets)
        second = dict(sheets.updates[3])
        first.pop("LAST_UPDATED_AT", None); second.pop("LAST_UPDATED_AT", None)
        self.assertEqual(first, second)                   # same fields, no dup rows

    def test_matching_never_writes_profiles(self):
        sheets = FakeMatchSheets([_ext()], [_profile()])
        match_inspiration(sheets=sheets)
        self.assertEqual(sheets.profile_writes, 0)        # profiles untouched


class TestIsolationFromInternal(unittest.TestCase):
    def test_external_matched_row_still_excluded_from_evidence(self):
        cells = match_row(_ext(), [_profile()])
        ext = {"_row": 999, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL,
               "PERFORMANCE": "Great", A_SIGNAL_COL: "1", **cells}
        internal = {"_row": 1, "PERFORMANCE": "Great", A_SIGNAL_COL: "1"}
        rows = [internal, ext]
        buckets = performance.buckets_for_rows(rows)
        self.assertIn(1, buckets)
        self.assertNotIn(999, buckets)
        analyzed = [r for r in rows if SheetsClient.is_analyzed(r) and r["_row"] in buckets]
        self.assertEqual(corr.compute(analyzed, buckets), corr.compute([internal], buckets))


if __name__ == "__main__":
    unittest.main()
