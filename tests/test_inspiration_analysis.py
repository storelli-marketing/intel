"""Unit tests for Milestone 2: external inspiration analysis (tagging).

Proves: external rows get tagged; queue context is used as a hint but never as
proof; internal Storelli rows are never touched; the SOURCE_TYPE guard holds;
low-information rows get LOW/Needs Review (never a fake HIGH); external
engagement never affects confidence; and tagged external rows still cannot enter
Storelli evidence calculations.

Run: python -m unittest discover -s tests
"""
import os
import sys
import json
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import inspiration_analyzer as ia
from inspiration_analyzer import (analyze_inspiration, analyze_row, build_metadata_prompt,
                                  decide_confidence, eligible_for_analysis,
                                  status_for_confidence, validate_tags,
                                  CONF_LOW, CONF_MEDIUM, CONF_HIGH)
from inspiration_sheets import SOURCE_TYPE_EXTERNAL
import performance
import correlations as corr
from sheets_client import SheetsClient
import taxonomy

A_SIGNAL_COL = taxonomy.all_signal_columns()[0]

# A well-formed model response covering several layers.
GOOD_JSON = json.dumps({
    "hook": ["Fear / Risk", "bogus-label"],   # bogus dropped
    "format": ["Demo"],
    "visual_style": ["Action"],
    "problem_type": ["Acute Pain"],
    "solution_type": ["Prevention"],
    "conversion": ["Learn More"],
    "offer": ["No Offer"],
    "product_presence": ["Hard Focus"],
    "funnel_stage": ["Consideration"],
})


class FakeGemini:
    """Captures the last prompt; returns a canned JSON response."""
    def __init__(self, response=GOOD_JSON):
        self.response = response
        self.prompts = []

    def summarize_findings(self, prompt):
        self.prompts.append(prompt)
        return self.response


class FakeContentSheets:
    def __init__(self, rows):
        self._rows = rows
        self.updates = {}       # row_index -> cells written
        self.runs = []

    def read_content_rows(self):
        return list(self._rows)

    def update_content_cells(self, row_index, values):
        self.updates[row_index] = dict(values)

    def append_run(self, run):
        self.runs.append(run)


def _ext_row(row=2, **over):
    r = {"_row": row, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL,
         "POST_URL": "https://ig/reel/A/", "ANALYSIS_STATUS": "Not Analyzed",
         "CAPTION": "Ultimate protection for keepers #storellimode",
         "THUMBNAIL_URL": "https://cdn/thumb.jpg", "PUBLISHED_AT": "2025-03-03T00:00:00+00:00",
         "DURATION_SECONDS": "18", "POST_TYPE": "Reel", "HANDLE": "@rival"}
    r.update(over)
    return r


# ---------------------------------------------------------------------------
class TestEligibility(unittest.TestCase):
    def test_external_pending_row_is_eligible(self):
        self.assertTrue(eligible_for_analysis(_ext_row(ANALYSIS_STATUS="")))
        self.assertTrue(eligible_for_analysis(_ext_row(ANALYSIS_STATUS="Needs Review")))

    def test_source_type_guard(self):
        self.assertFalse(eligible_for_analysis(_ext_row(SOURCE_TYPE="")))
        self.assertFalse(eligible_for_analysis(_ext_row(SOURCE_TYPE="INTERNAL")))

    def test_skipped_or_test_excluded(self):
        self.assertFalse(eligible_for_analysis(_ext_row(ANALYSIS_STATUS="Skipped")))
        self.assertFalse(eligible_for_analysis(_ext_row(SCRAPE_STATUS="Skipped")))
        self.assertFalse(eligible_for_analysis(_ext_row(ANALYSIS_STATUS="Test")))

    def test_already_analyzed_or_failed_not_re_run(self):
        self.assertFalse(eligible_for_analysis(_ext_row(ANALYSIS_STATUS="Analyzed")))
        self.assertFalse(eligible_for_analysis(_ext_row(ANALYSIS_STATUS="Failed")))

    def test_missing_url_excluded(self):
        self.assertFalse(eligible_for_analysis(_ext_row(POST_URL="")))


class TestConfidence(unittest.TestCase):
    def test_high_only_with_full_video(self):
        row = _ext_row()  # caption + thumbnail + metadata all present
        self.assertEqual(decide_confidence(row, full_video_analyzed=False), CONF_MEDIUM)
        self.assertEqual(decide_confidence(row, full_video_analyzed=True), CONF_HIGH)

    def test_caption_only_is_low(self):
        row = _ext_row(THUMBNAIL_URL="", PUBLISHED_AT="", DURATION_SECONDS="", POST_TYPE="")
        self.assertEqual(decide_confidence(row, full_video_analyzed=False), CONF_LOW)

    def test_engagement_never_affects_confidence(self):
        low = _ext_row(LIKE_COUNT="0", VIEW_COUNT="0", COMMENT_COUNT="0")
        high = _ext_row(LIKE_COUNT="999999", VIEW_COUNT="999999", COMMENT_COUNT="999999")
        self.assertEqual(decide_confidence(low, False), decide_confidence(high, False))

    def test_low_confidence_flags_needs_review(self):
        self.assertEqual(status_for_confidence(CONF_LOW), "Needs Review")
        self.assertEqual(status_for_confidence(CONF_MEDIUM), "Analyzed")
        self.assertEqual(status_for_confidence(CONF_HIGH), "Analyzed")


class TestTagValidation(unittest.TestCase):
    def test_unknown_labels_dropped_and_single_label_capped(self):
        raw = {"hook": ["Fear / Risk", "nope"], "conversion": ["Learn More", "Direct Purchase"]}
        out = validate_tags(raw)
        self.assertEqual(out["hook"], ["Fear / Risk"])
        self.assertEqual(len(out["conversion"]), 1)   # single-label capped


class TestAnalyzeRow(unittest.TestCase):
    def test_tags_written_with_medium_confidence(self):
        cells = analyze_row(_ext_row(), FakeGemini(), full_video_enabled=False)
        self.assertEqual(cells["ANALYSIS_STATUS"], "Analyzed")
        self.assertEqual(cells["ANALYSIS_CONFIDENCE"], CONF_MEDIUM)
        self.assertEqual(cells["HOOK_TAGS"], "Fear / Risk")
        self.assertEqual(cells["FORMAT_TAGS"], "Demo")
        self.assertEqual(cells["TAXONOMY_VERSION"], ia.TAXONOMY_VERSION)
        self.assertEqual(cells["ERROR_MESSAGE"], "")

    def test_low_info_never_fake_high(self):
        row = _ext_row(THUMBNAIL_URL="", PUBLISHED_AT="", DURATION_SECONDS="", POST_TYPE="")
        cells = analyze_row(row, FakeGemini(), full_video_enabled=False)
        self.assertEqual(cells["ANALYSIS_CONFIDENCE"], CONF_LOW)
        self.assertEqual(cells["ANALYSIS_STATUS"], "Needs Review")
        self.assertNotEqual(cells["ANALYSIS_CONFIDENCE"], CONF_HIGH)

    def test_empty_model_output_needs_review(self):
        empty = json.dumps({k: [] for k in taxonomy.LAYERS})
        cells = analyze_row(_ext_row(), FakeGemini(empty), full_video_enabled=False)
        self.assertEqual(cells["ANALYSIS_STATUS"], "Needs Review")
        self.assertEqual(cells["ANALYSIS_CONFIDENCE"], CONF_LOW)

    def test_gemini_missing_flags_not_fake(self):
        cells = analyze_row(_ext_row(), None, full_video_enabled=False)
        self.assertEqual(cells["ANALYSIS_STATUS"], "Needs Review")
        self.assertEqual(cells["ANALYSIS_CONFIDENCE"], CONF_LOW)
        self.assertIn("Gemini", cells["ERROR_MESSAGE"])

    def test_model_error_marked_failed(self):
        class Boom:
            def summarize_findings(self, p):
                raise RuntimeError("model exploded")
        cells = analyze_row(_ext_row(), Boom(), full_video_enabled=False)
        self.assertEqual(cells["ANALYSIS_STATUS"], "Failed")
        self.assertIn("model exploded", cells["ERROR_MESSAGE"])


class TestQueueContextUsedNotProof(unittest.TestCase):
    def test_context_in_prompt_but_not_forced(self):
        row = _ext_row(REASON_FOR_ADDING="great save montage",
                       TARGET_PRODUCT="GK Gloves", TARGET_ICP="Aspiring Pro")
        prompt = build_metadata_prompt(row)
        # Context is included as hints...
        self.assertIn("great save montage", prompt)
        self.assertIn("GK Gloves", prompt)
        self.assertIn("Aspiring Pro", prompt)
        # ...and the prompt explicitly forbids treating it as ground truth /
        # judging performance.
        self.assertIn("HINTS ONLY", prompt)
        self.assertRegex(prompt, r"do NOT judge performance|not.*judge performance|Ignore any like")

    def test_context_does_not_inflate_confidence(self):
        # Rich curator context but poor content signal → still LOW.
        row = _ext_row(THUMBNAIL_URL="", PUBLISHED_AT="", DURATION_SECONDS="", POST_TYPE="",
                       REASON_FOR_ADDING="amazing", TARGET_PRODUCT="GK Gloves",
                       TARGET_ICP="Aspiring Pro")
        self.assertEqual(decide_confidence(row, full_video_analyzed=False), CONF_LOW)


class TestOrchestratorIsolation(unittest.TestCase):
    def test_only_external_rows_processed(self):
        rows = [
            _ext_row(2),
            _ext_row(3, SOURCE_TYPE="INTERNAL"),      # not external — must skip
            _ext_row(4, ANALYSIS_STATUS="Skipped"),    # skipped — must skip
        ]
        sheets = FakeContentSheets(rows)
        run = analyze_inspiration(sheets=sheets, gemini=FakeGemini(), full_video_enabled=False)
        self.assertEqual(run["RUN_TYPE"], "Analyze")
        self.assertEqual(run["POSTS_DISCOVERED"], 1)
        self.assertEqual(set(sheets.updates), {2})      # only row 2 written
        self.assertEqual(run["POSTS_ANALYZED"], 1)
        self.assertEqual(len(sheets.runs), 1)

    def test_internal_rows_never_written(self):
        rows = [_ext_row(3, SOURCE_TYPE="INTERNAL")]
        sheets = FakeContentSheets(rows)
        analyze_inspiration(sheets=sheets, gemini=FakeGemini(), full_video_enabled=False)
        self.assertEqual(sheets.updates, {})            # nothing written at all


class TestTaggedExternalStillExcludedFromEvidence(unittest.TestCase):
    def test_tagged_external_row_not_in_buckets_or_correlations(self):
        # Build the external row as it looks AFTER tagging, then hand it to the
        # internal learning functions with a taxonomy signal + 'Great' perf.
        cells = analyze_row(_ext_row(), FakeGemini(), full_video_enabled=False)
        ext = {"_row": 999, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL,
               "PERFORMANCE": "Great", A_SIGNAL_COL: "1", **cells}
        internal = {"_row": 1, "PERFORMANCE": "Great", A_SIGNAL_COL: "1"}
        rows = [internal, ext]

        buckets = performance.buckets_for_rows(rows)
        self.assertIn(1, buckets)
        self.assertNotIn(999, buckets)                  # external excluded

        analyzed = [r for r in rows
                    if SheetsClient.is_analyzed(r) and r["_row"] in buckets]
        self.assertNotIn(999, {r["_row"] for r in analyzed})
        self.assertEqual(corr.compute(analyzed, buckets), corr.compute([internal], buckets))


if __name__ == "__main__":
    unittest.main()
