"""Tests for the Inspiration Candidate Quality Review layer.

Proves eligibility gating, risk detection, that engagement alone can't pass a
low-relevance row, metadata-only never claims full-video confidence, USE gate
correctness, isolation from internal Storelli proof, and idempotent review.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import inspiration_quality as iq
from inspiration_quality import (eligible_for_review, quality_review_inspiration,
                                 review_row, risk_assessment)
from inspiration_sheets import SOURCE_TYPE_EXTERNAL
import performance
import correlations as corr
from sheets_client import SheetsClient
import taxonomy

A_SIGNAL_COL = taxonomy.all_signal_columns()[0]


def _good(**over):
    """A strong, safe, relevant, adaptable goalkeeper candidate."""
    r = {"_row": 3, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL, "SAFETY_STATUS": "Safe",
         "ANALYSIS_STATUS": "Analyzed", "POST_URL": "https://tiktok/x",
         "HANDLE": "gk_coach",
         "CAPTION": "goalkeeper diving tutorial: prevent turf burn injury, build confidence",
         "HOOK_TAGS": "Education, Fear / Risk", "FORMAT_TAGS": "Tutorial, Demo",
         "VISUAL_STYLE_TAGS": "Action", "PROBLEM_TAGS": "Acute Pain",
         "SOLUTION_TAGS": "Prevention", "FUNNEL_STAGE_TAGS": "Consideration",
         "MATCH_SCORE": "70", "FINAL_SCORE": "62", "VIEW_FOLLOWER_RATIO": "6.0",
         "PRIORITY_SCORE": "0.8"}
    r.update(over)
    return r


class TestEligibility(unittest.TestCase):
    def test_safe_analyzed_eligible(self):
        self.assertTrue(eligible_for_review(_good()))

    def test_rejected_skipped_unanalyzed_ignored(self):
        self.assertFalse(eligible_for_review(_good(SAFETY_STATUS="Rejected")))
        self.assertFalse(eligible_for_review(_good(ANALYSIS_STATUS="Skipped")))
        self.assertFalse(eligible_for_review(_good(ANALYSIS_STATUS="Not Analyzed")))
        self.assertFalse(eligible_for_review(_good(SOURCE_TYPE="INTERNAL")))


class TestRiskAndUseGate(unittest.TestCase):
    def test_good_candidate_used(self):
        cells = review_row(_good(), review_method=iq.REVIEW_FULL)
        self.assertEqual(cells["USE_FOR_IDEA_GEN"], "TRUE")
        self.assertGreaterEqual(float(cells["INSPIRATION_QUALITY_SCORE"]), 70)
        self.assertTrue(cells["CREATIVE_MECHANISM"])

    def test_famous_player_blocks_use(self):
        cells = review_row(_good(CAPTION="Messi highlights vs Real Madrid"),
                           review_method=iq.REVIEW_FULL)
        self.assertEqual(cells["FAMOUS_PLAYER_RISK"], "High")
        self.assertEqual(cells["USE_FOR_IDEA_GEN"], "FALSE")

    def test_match_footage_blocks_use(self):
        cells = review_row(_good(CAPTION="full match highlights extended goals"),
                           review_method=iq.REVIEW_FULL)
        self.assertEqual(cells["MATCH_FOOTAGE_RISK"], "High")
        self.assertEqual(cells["USE_FOR_IDEA_GEN"], "FALSE")

    def test_off_domain_blocks_use(self):
        cells = review_row(_good(CAPTION="stab proof body armor knife protection"),
                           review_method=iq.REVIEW_FULL)
        self.assertEqual(cells["OFF_DOMAIN_RISK"], "High")
        self.assertEqual(cells["USE_FOR_IDEA_GEN"], "FALSE")

    def test_high_views_cannot_rescue_low_relevance(self):
        # No Storelli relevance, no adaptable mechanism, but huge ratio/priority.
        weak = {"_row": 4, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL, "SAFETY_STATUS": "Safe",
                "ANALYSIS_STATUS": "Analyzed", "CAPTION": "random viral dance clip",
                "HOOK_TAGS": "Humor", "FORMAT_TAGS": "Reaction", "MATCH_SCORE": "5",
                "VIEW_FOLLOWER_RATIO": "50", "PRIORITY_SCORE": "0.99"}
        cells = review_row(weak, review_method=iq.REVIEW_FULL)
        self.assertLess(float(cells["INSPIRATION_QUALITY_SCORE"]), 70)
        self.assertEqual(cells["USE_FOR_IDEA_GEN"], "FALSE")

    def test_ratio_helps_only_when_relevant_and_safe(self):
        base = _good(VIEW_FOLLOWER_RATIO="0.1", PRIORITY_SCORE="0.0")
        boosted = _good(VIEW_FOLLOWER_RATIO="8.0", PRIORITY_SCORE="0.9")
        q_base = float(review_row(base)["INSPIRATION_QUALITY_SCORE"])
        q_boost = float(review_row(boosted)["INSPIRATION_QUALITY_SCORE"])
        self.assertGreater(q_boost, q_base)      # ratio helps a good candidate
        # ...but never rescues an unsafe one:
        unsafe = review_row(_good(CAPTION="Messi highlights", VIEW_FOLLOWER_RATIO="50"),
                            review_method=iq.REVIEW_FULL)
        self.assertEqual(unsafe["USE_FOR_IDEA_GEN"], "FALSE")


class TestReviewMethod(unittest.TestCase):
    def test_metadata_only_labeled_and_noted(self):
        cells = review_row(_good(), review_method=iq.REVIEW_METADATA)
        self.assertEqual(cells["REVIEW_METHOD"], "Metadata Only")
        self.assertIn("metadata-based review", cells["QUALITY_REVIEW_NOTES"])

    def test_full_video_labeled(self):
        cells = review_row(_good(), review_method=iq.REVIEW_FULL)
        self.assertEqual(cells["REVIEW_METHOD"], "Full Video")
        self.assertNotIn("metadata-based review", cells["QUALITY_REVIEW_NOTES"])


class FakeQualitySheets:
    def __init__(self, rows):
        self._rows = rows
        self.updates = {}
        self.runs = []

    def ensure_content_columns(self, cols):
        return []

    def read_content_rows(self):
        return list(self._rows)

    def update_content_cells_bulk(self, updates):
        for row_index, values in updates:
            self.updates[row_index] = dict(values)

    def append_run(self, run):
        self.runs.append(run)


class TestOrchestrator(unittest.TestCase):
    def _rows(self):
        return [
            _good(_row=3),
            _good(_row=4, SAFETY_STATUS="Rejected"),          # ignored
            _good(_row=5, ANALYSIS_STATUS="Not Analyzed"),    # ignored
        ]

    def test_only_eligible_reviewed_and_run_logged(self):
        sheets = FakeQualitySheets(self._rows())
        run = quality_review_inspiration(sheets=sheets, enable_full_video=False)
        self.assertEqual(run["RUN_TYPE"], "QualityReview")
        self.assertEqual(run["POSTS_DISCOVERED"], 1)
        self.assertEqual(set(sheets.updates), {3})
        self.assertEqual(len(sheets.runs), 1)

    def test_idempotent(self):
        sheets = FakeQualitySheets([_good(_row=3)])
        quality_review_inspiration(sheets=sheets, enable_full_video=False)
        a = dict(sheets.updates[3]); a.pop("LAST_UPDATED_AT", None)
        quality_review_inspiration(sheets=sheets, enable_full_video=False)
        b = dict(sheets.updates[3]); b.pop("LAST_UPDATED_AT", None)
        self.assertEqual(a, b)


class TestIsolationFromInternal(unittest.TestCase):
    def test_reviewed_external_row_excluded_from_evidence(self):
        cells = review_row(_good(), review_method=iq.REVIEW_FULL)
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
