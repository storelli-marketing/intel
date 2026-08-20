"""Performance maturity, provenance, and the real follower denominator.

The invariants under test are the ones that protect the correlation set:
  * a human PERFORMANCE label is NEVER withheld, overwritten, or cleared;
  * an automation label on a too-young post is withheld (not guessed) and kept
    out of correlations until the view count settles;
  * every automation label carries provenance (source + measured-at);
  * today's account follower count is used as a measurement denominator and is
    never written into a field claiming it was the count at publication.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import owned_discovery as od
import performance


def row(**kw):
    r = {"_row": 10, "LINK": "https://www.instagram.com/reel/AAA/", "VIEWS": "17000",
         "PERFORMANCE": "", "PERFORMANCE_SOURCE": "", "POST_DATE": ""}
    r.update(kw)
    return r


def days_ago(n):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


class TestAgeParsing(unittest.TestCase):
    def test_reads_common_date_shapes(self):
        for value in (days_ago(10), days_ago(10) + " 12:00", days_ago(10) + "T08:30:00Z"):
            age = performance.post_age_days(row(POST_DATE=value))
            self.assertIsNotNone(age, value)
            self.assertAlmostEqual(age, 10, delta=1.5)

    def test_unknown_date_is_none(self):
        self.assertIsNone(performance.post_age_days(row(POST_DATE="")))
        self.assertIsNone(performance.post_age_days(row(POST_DATE="sometime last spring")))

    def test_accepts_post_date_aliases(self):
        r = {"_row": 1, "Post Date": days_ago(4)}
        self.assertAlmostEqual(performance.post_age_days(r), 4, delta=1.5)


class TestMaturity(unittest.TestCase):
    def test_unknown_age_counts_as_mature(self):
        """The 196 pre-existing rows have no POST_DATE. Treating unknown age as
        immature would silently delete the entire historical evidence base."""
        r = row(POST_DATE="", PERFORMANCE="Great")
        self.assertTrue(performance.is_mature(r))
        self.assertFalse(performance.is_immature_auto(r))

    def test_young_auto_row_is_immature(self):
        r = row(POST_DATE=days_ago(2), PERFORMANCE="Underdog",
                PERFORMANCE_SOURCE=config.PERF_SOURCE_AUTO)
        self.assertFalse(performance.is_mature(r))
        self.assertTrue(performance.is_immature_auto(r))

    def test_matured_auto_row_is_included(self):
        r = row(POST_DATE=days_ago(config.PERFORMANCE_MATURITY_DAYS + 3),
                PERFORMANCE="Underdog", PERFORMANCE_SOURCE=config.PERF_SOURCE_AUTO)
        self.assertTrue(performance.is_mature(r))
        self.assertFalse(performance.is_immature_auto(r))

    def test_young_human_label_is_never_withheld(self):
        """A human graded it deliberately; the maturity rule is about the
        automation guessing too early, not about second-guessing a person."""
        r = row(POST_DATE=days_ago(1), PERFORMANCE="Great",
                PERFORMANCE_SOURCE=config.PERF_SOURCE_HUMAN)
        self.assertFalse(performance.is_immature_auto(r))

    def test_immature_auto_row_excluded_from_correlations(self):
        old = row(_row=1, POST_DATE=days_ago(40), PERFORMANCE="Great",
                  PERFORMANCE_SOURCE=config.PERF_SOURCE_AUTO)
        young = row(_row=2, POST_DATE=days_ago(1), PERFORMANCE="Underdog",
                    PERFORMANCE_SOURCE=config.PERF_SOURCE_AUTO)
        buckets = performance.buckets_for_rows([old, young])
        self.assertIn(1, buckets)
        self.assertNotIn(2, buckets)

    def test_pending_rows_are_reported_not_silently_dropped(self):
        young = row(_row=2, POST_DATE=days_ago(1), PERFORMANCE="Underdog",
                    PERFORMANCE_SOURCE=config.PERF_SOURCE_AUTO)
        self.assertEqual(performance.pending_maturity_rows([young]), [2])


class TestProvenance(unittest.TestCase):
    def test_source_is_read_from_the_column(self):
        self.assertEqual(
            performance.performance_source(row(PERFORMANCE_SOURCE="AUTO_PUBLIC_METRICS")),
            config.PERF_SOURCE_AUTO)
        self.assertTrue(performance.is_auto_labelled(
            row(PERFORMANCE_SOURCE=config.PERF_SOURCE_AUTO)))

    def test_blank_source_is_not_assumed_to_be_automation(self):
        """Uncertain provenance must never be treated as ours to overwrite."""
        r = row(PERFORMANCE="Great")
        self.assertEqual(performance.performance_source(r), "")
        self.assertFalse(performance.is_auto_labelled(r))

    def test_human_label_survives_reprocess_decisions(self):
        from main import _determine_performance
        r = row(PERFORMANCE="Great", VIEWS="1000")   # views imply a far worse label
        label, write_value, analyze_ok = _determine_performance(r, reprocess=False)
        self.assertEqual(label, "Great")
        self.assertEqual(write_value, "", "existing label must not be rewritten")
        self.assertTrue(analyze_ok)

    def test_young_unlabelled_row_is_analyzed_but_not_labelled(self):
        from main import _determine_performance
        r = row(POST_DATE=days_ago(1), PERFORMANCE="", VIEWS="20000")
        label, write_value, analyze_ok = _determine_performance(r, reprocess=False)
        self.assertIsNone(label)
        self.assertEqual(write_value, "")
        self.assertTrue(analyze_ok, "taxonomy analysis still happens")

    def test_mature_unlabelled_row_is_labelled_with_provenance(self):
        from main import _determine_performance
        r = row(POST_DATE=days_ago(30), PERFORMANCE="", VIEWS="500000")
        label, write_value, analyze_ok = _determine_performance(r, reprocess=False)
        self.assertTrue(write_value, "a matured row must get a computed label")
        self.assertEqual(label, write_value)
        self.assertTrue(analyze_ok)


class TestFollowerDenominator(unittest.TestCase):
    def test_row_measurement_denominator_beats_config_fallback(self):
        from main import _determine_performance
        hi = row(POST_DATE=days_ago(30), VIEWS="200000",
                 FOLLOWERS_AT_MEASUREMENT="1000000")
        lo = row(POST_DATE=days_ago(30), VIEWS="200000",
                 FOLLOWERS_AT_MEASUREMENT="10000")
        self.assertNotEqual(_determine_performance(hi, False)[1],
                            _determine_performance(lo, False)[1])

    def test_profile_details_supplies_followers_when_posts_do_not(self):
        """Reel results carry no ownerFollowersCount, so the denominator has to
        come from a profile lookup rather than a hardcoded number."""
        calls = []

        class FakeClient:
            def run_actor(self, actor, run_input):
                calls.append(run_input.get("resultsType"))
                if run_input.get("resultsType") == "details":
                    return [{"username": config.STORELLI_INSTAGRAM_HANDLE,
                             "followersCount": 169209}]
                return [{"ownerUsername": config.STORELLI_INSTAGRAM_HANDLE,
                         "shortCode": "AAA", "videoPlayCount": 36026}]

        scan = od.scan_owned_media(client=FakeClient(), max_results=5)
        self.assertTrue(scan["ok"])
        self.assertEqual(scan["followers"], 169209)
        self.assertEqual(scan["follower_source"], "profile_details")
        self.assertIn("reels", calls, "owned scan should request reels")
        self.assertIn("details", calls, "one profile call for the denominator")

    def test_no_extra_profile_call_when_posts_carry_followers(self):
        class FakeClient:
            def __init__(self):
                self.types = []

            def run_actor(self, actor, run_input):
                self.types.append(run_input.get("resultsType"))
                return [{"ownerUsername": config.STORELLI_INSTAGRAM_HANDLE,
                         "shortCode": "AAA", "ownerFollowersCount": 170500}]

        c = FakeClient()
        scan = od.scan_owned_media(client=c, max_results=5)
        self.assertEqual(scan["followers"], 170500)
        self.assertEqual(scan["follower_source"], "post_results")
        self.assertNotIn("details", c.types, "no wasted second Apify call")

    def test_follower_lookup_failure_is_soft(self):
        class Boom:
            def run_actor(self, actor, run_input):
                if run_input.get("resultsType") == "details":
                    raise RuntimeError("rate limited")
                return [{"ownerUsername": config.STORELLI_INSTAGRAM_HANDLE,
                         "shortCode": "AAA"}]

        scan = od.scan_owned_media(client=Boom(), max_results=5)
        self.assertTrue(scan["ok"], "a missing denominator must not fail the scan")
        self.assertIsNone(scan["followers"])
        self.assertEqual(scan["follower_source"], "unavailable")

    def test_measurement_followers_are_not_written_as_at_post(self):
        """Today's count describes the measurement, not publication day."""
        import social_metrics_ingest as smi
        values = smi.build_metric_values(
            {"timestamp": "2026-08-01T10:00:00Z"},
            {"views": 36026, "followers_at_measurement": 169209,
             "metrics_measured_at": "2026-08-20 09:00 UTC"})
        self.assertEqual(values["FOLLOWERS_AT_MEASUREMENT"], "169209")
        self.assertEqual(values["METRICS_MEASURED_AT"], "2026-08-20 09:00 UTC")
        self.assertNotIn("FOLLOWERS_AT_POST", values,
                         "today's count must never claim to be the count at publication")
        self.assertEqual(values["POST_DATE"], "2026-08-01")


class TestMaturityStage(unittest.TestCase):
    def test_dry_run_reports_what_it_would_classify(self):
        import intelligence_refresh as ir
        import sheets_client

        class FakePoc:
            meta_col = {"PERFORMANCE": 1, "PERFORMANCE_SOURCE": 2,
                        "PERFORMANCE_MEASURED_AT": 3, "VIEWS": 4, "POST_DATE": 5}

            def validate_columns(self):
                pass

            def read_rows(self):
                return [row(_row=7, POST_DATE=days_ago(30), VIEWS="800000"),
                        row(_row=8, POST_DATE=days_ago(1), VIEWS="900")]

        real = sheets_client.SheetsClient
        sheets_client.SheetsClient = FakePoc
        try:
            st = ir._internal_maturity(dry_run=True)
        finally:
            sheets_client.SheetsClient = real
        self.assertEqual(st["status"], "success")
        self.assertEqual(st["updated"], 1, "only the matured row is classified")
        self.assertIn("row 7", st["reason"])
        self.assertNotIn("row 8", st["reason"])


if __name__ == "__main__":
    unittest.main()


class TestAnalyzerRobustness(unittest.TestCase):
    """A single odd model response must not throw away a whole analyzed row."""

    def test_list_valued_string_fields_do_not_crash(self):
        import analyzer
        meta = analyzer.to_signal_columns({
            "summary": ["Keeper makes a save.", "Product shown at the end."],
            "icp_suggested": ["Aspiring Pro"],
            "product_suggested": 42,
            "confidence": {"hook": "high"}})
        self.assertIn("Keeper makes a save.", meta["ai_summary"])
        self.assertEqual(meta["icp_suggested"], "Aspiring Pro")
        self.assertEqual(meta["product_suggested"], "42")

    def test_missing_fields_still_default_cleanly(self):
        import analyzer
        meta = analyzer.to_signal_columns({})
        self.assertEqual(meta["ai_summary"], "")
        self.assertEqual(meta["conf_hook"], "medium")


class TestRunLogTab(unittest.TestCase):
    def test_missing_run_log_tab_is_created_not_raised(self):
        """The run log lives in its own tab; asking for it before it exists must
        create it, not abort the refresh's history record."""
        import gspread
        import intelligence_refresh as ir
        created = {}

        class FakeWs:
            def update(self, **kw):
                created["header"] = kw.get("values")

        class FakeSpreadsheet:
            def worksheet(self, title):
                raise gspread.WorksheetNotFound(title)

            def add_worksheet(self, title, rows, cols):
                created["title"] = title
                return FakeWs()

        class FakeSheets:
            _sh = FakeSpreadsheet()

            def _ws(self, tab):
                raise AssertionError("must not request the missing tab directly")

        ir._runs_ws(FakeSheets())
        self.assertEqual(created["title"], ir.RUNS_TAB)
        self.assertEqual(created["header"], [list(ir.RUNS_COLUMNS)])
