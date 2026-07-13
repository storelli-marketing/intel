"""Contamination safeguards for the Inspiration Layer.

Proves external inspiration rows can NEVER enter the internal Storelli learning
pipeline: not performance buckets, not correlations, not the analyzed set that
feeds Signal Library / Marketing Learnings, and not the sheet the internal
pipeline reads/writes.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import performance
import correlations as corr
from sheets_client import SheetsClient
from inspiration_sheets import SOURCE_TYPE_EXTERNAL, InspirationSheets


# A minimal "internal" tagged row: has a taxonomy signal set and a great
# performance. Column name for a real taxonomy signal is discovered from the
# taxonomy so the test stays valid if the taxonomy changes.
import taxonomy

A_SIGNAL_COL = taxonomy.all_signal_columns()[0]


def _internal_row(row_id, perf="Great", signal=1):
    return {"_row": row_id, "PERFORMANCE": perf, A_SIGNAL_COL: str(signal)}


def _inspiration_row(row_id, perf="Great", signal=1):
    # Exactly what an EXTERNAL_INSPIRATION post would look like if someone
    # wrongly merged it into the internal rows list.
    r = _internal_row(row_id, perf, signal)
    r["SOURCE_TYPE"] = SOURCE_TYPE_EXTERNAL
    return r


class TestSourceTypeClassification(unittest.TestCase):
    def test_external_inspiration_is_reference(self):
        row = _inspiration_row(10)
        self.assertTrue(performance.is_reference_row(row))
        self.assertFalse(performance.is_internal_row(row))

    def test_plain_internal_row_is_not_reference(self):
        self.assertFalse(performance.is_reference_row(_internal_row(11)))


class TestPerformanceBuckets(unittest.TestCase):
    def test_inspiration_excluded_from_buckets(self):
        rows = [_internal_row(1), _inspiration_row(2)]
        buckets = performance.buckets_for_rows(rows)
        self.assertIn(1, buckets)          # internal row is bucketed
        self.assertNotIn(2, buckets)       # inspiration row is NOT

    def test_inspiration_never_a_positive_bucket(self):
        rows = [_inspiration_row(2, perf="Great")]
        buckets = performance.buckets_for_rows(rows)
        self.assertEqual(buckets, {})      # even "Great" inspiration is dropped


class TestCorrelationsIsolation(unittest.TestCase):
    def test_inspiration_cannot_change_lift(self):
        """Correlations run over `buckets`; since inspiration rows are excluded
        from buckets, adding an inspiration row (even a 'Great' one carrying the
        signal) does not change any association result."""
        internal = [_internal_row(1, "Great", 1), _internal_row(2, "Underdog", 0)]
        buckets_before = performance.buckets_for_rows(internal)
        res_before = corr.compute(
            [r for r in internal if r["_row"] in buckets_before], buckets_before)

        contaminated = internal + [_inspiration_row(3, "Great", 1)]
        buckets_after = performance.buckets_for_rows(contaminated)
        # The analyzed set = rows that are in buckets (the real compute_findings
        # gate). Inspiration row 3 is not in buckets, so it never reaches compute.
        analyzed = [r for r in contaminated if r["_row"] in buckets_after]
        res_after = corr.compute(analyzed, buckets_after)

        self.assertEqual(buckets_before, buckets_after)
        self.assertEqual(res_before, res_after)
        self.assertNotIn(3, buckets_after)


class TestAnalyzedSetGate(unittest.TestCase):
    def test_inspiration_excluded_from_analyzed_selection(self):
        """Replicates compute_findings' selection: analyzed rows must be
        is_analyzed AND present in buckets. Signal Library and Marketing
        Learnings are both derived from this set, so exclusion here proves
        inspiration can never become proof."""
        rows = [_internal_row(1, "Great", 1), _inspiration_row(2, "Great", 1)]
        buckets = performance.buckets_for_rows(rows)
        analyzed = [r for r in rows
                    if SheetsClient.is_analyzed(r) and r["_row"] in buckets]
        analyzed_ids = {r["_row"] for r in analyzed}
        self.assertIn(1, analyzed_ids)
        self.assertNotIn(2, analyzed_ids)


class TestWorksheetIsolation(unittest.TestCase):
    def test_internal_tab_is_guarded(self):
        import config
        original = config.GOOGLE_WORKSHEET_NAME
        try:
            config.GOOGLE_WORKSHEET_NAME = "Marketing brain POC"
            # The internal sheet is off-limits to the inspiration layer...
            self.assertTrue(InspirationSheets.is_internal_tab("Marketing brain POC"))
            self.assertTrue(InspirationSheets.is_internal_tab("marketing brain poc"))
            # ...while the inspiration tabs are fine.
            self.assertFalse(InspirationSheets.is_internal_tab("INSPIRATION_CONTENT"))
            self.assertFalse(InspirationSheets.is_internal_tab("MONITORED CHANNELS"))
        finally:
            config.GOOGLE_WORKSHEET_NAME = original


if __name__ == "__main__":
    unittest.main()
