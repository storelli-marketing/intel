"""Tests for the Social Metrics Import + Sheet Schema Setup workflow.

Proves the workflow prepares the Sheet safely and never corrupts taxonomy or
existing analyzed data:
  * the insertion point is detected between Status and HOOK (not appended right);
  * the staging tab schema is exactly as specified;
  * the dry-run import writes nothing, reports unmatched links, and never
    overwrites an already-populated POC cell;
  * demographic split strings and numeric fields validate honestly;
  * the Content audit duration-bucket report works;
  * the Slack schema/import questions route to the right answers;
  * nothing mutates internal rows / no Notion writes.

Run: python -m unittest tests.test_social_metrics_import
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import social_analytics as sa
import taxonomy


# Two-row header mirroring the live POC (Status=col7 G, HOOK=col8 H).
_ROW1 = ["", "", "", "", "", "", "", "HOOK", "", "", "", "", "", "", "FORMAT"]
_ROW2 = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status",
         "Curiosity Gap", "Fear / Risk", "Aspiration", "Education", "Humor", "Social Proof",
         "Authority", "POV"]


def _poc(n, link, perf, **extra):
    r = {"_row": n, "LINK": link, "PERFORMANCE": perf, "Storytelling structure": "Demo",
         "ICP": "", "Product": "", "Status": "completed"}
    for c in taxonomy.all_signal_columns():
        r[c] = ""
    r.update(extra)
    return r


class _Guard(unittest.TestCase):
    """Restore the two network indirections after each test."""

    def setUp(self):
        self._i = sa._internal_sheet
        self._w = sa._read_named_worksheet

    def tearDown(self):
        sa._internal_sheet = self._i
        sa._read_named_worksheet = self._w


# --------------------------------------------------------------------------- #
# Task 1/2 — preflight + insertion plan
# --------------------------------------------------------------------------- #
class TestInsertionPlan(unittest.TestCase):
    def test_insertion_point_detected(self):
        pf = sa.preflight_poc_structure([_ROW1, _ROW2])
        self.assertEqual(pf["status_col"], 7)
        self.assertEqual(pf["hook_col"], 8)
        self.assertEqual(pf["first_category"], "HOOK")
        self.assertTrue(pf["status_immediately_before_hook"])
        self.assertTrue(pf["safe"])
        self.assertEqual(pf["metadata_columns"][-1], "Status")

    def test_plan_inserts_between_status_and_hook_not_appended(self):
        plan = sa.insertion_plan(include_optional=True, values=[_ROW1, _ROW2])
        # first new column sits exactly at the old HOOK column (col 8)
        self.assertEqual(plan["insert_at_col"], 8)
        self.assertEqual(plan["positions"][0]["name"], "REEL_TYPE")
        self.assertEqual(plan["positions"][0]["a1_row2"], "H2")
        # every inserted column lands in [Status+1 .. old_hook + count) — never to the far right
        cols = [p["col"] for p in plan["positions"]]
        self.assertEqual(cols, list(range(8, 8 + plan["count"])))
        # taxonomy is pushed right by exactly the number of inserted columns (not overwritten)
        self.assertEqual(plan["new_hook_col_after"], 8 + plan["count"])
        self.assertGreater(plan["new_hook_col_after"], 8)      # HOOK moved right

    def test_required_only_plan(self):
        plan = sa.insertion_plan(include_optional=False, values=[_ROW1, _ROW2])
        self.assertEqual(plan["count"], 14)
        self.assertNotIn("REACH", [p["name"] for p in plan["positions"]])

    def test_render_names_insertion_location(self):
        out = sa.render_insertion_plan(sa.insertion_plan(values=[_ROW1, _ROW2]))
        self.assertIn("row 1 (category) = BLANK", out)
        self.assertIn("Do NOT append to the far right", out)
        self.assertIn("NOTHING WRITTEN", out)


class TestAutoInserter(_Guard):
    def test_dry_run_plans_but_writes_nothing(self):
        sa._internal_sheet = self._i        # not used; inserter reads _poc_values
        sa._poc_values = lambda: [list(_ROW1), list(_ROW2)]
        try:
            r = sa.insert_poc_metric_columns(apply=False)
        finally:
            pass
        self.assertTrue(r["ok"])
        self.assertFalse(r["wrote"])
        self.assertTrue(r["dry_run"])
        self.assertIn("DRY-RUN", sa.render_insert_result(r))

    def test_extra_metadata_columns_are_still_safe(self):
        # Metadata columns accumulate over time, so Status need not be adjacent
        # to HOOK — what matters is that everything before HOOK has a blank
        # row-1 category. This layout IS safe.
        row1 = ["", "", "", "", "", "", "", "", "HOOK"]
        row2 = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product",
                "Status", "VIEWS", "Curiosity Gap"]
        sa._poc_values = lambda: [row1, row2]
        pf = sa.preflight_poc_structure([row1, row2])
        self.assertTrue(pf["safe"])
        self.assertTrue(pf["metadata_block_contiguous"])
        self.assertFalse(pf["status_immediately_before_hook"])
        r = sa.insert_poc_metric_columns(apply=False)     # dry-run plans cleanly
        self.assertTrue(r["ok"])
        self.assertEqual(r["plan"]["insert_at_col"], 9)   # right before HOOK

    def test_refuses_unsafe_boundary(self):
        # A pre-HOOK column carrying a NON-blank row-1 category means the
        # metadata block is not contiguous — inserting could land inside a
        # taxonomy group, so this must be refused even with apply=True.
        row1 = ["", "", "", "", "", "", "", "HOOK", "", "FORMAT"]
        row2 = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product",
                "Status", "Curiosity Gap", "Fear / Risk", "POV"]
        # inject a stray categorised column before the first blank-run ends
        row1[6] = "HOOK"          # Status column now sits under a category
        sa._poc_values = lambda: [row1, row2]
        r = sa.insert_poc_metric_columns(apply=True)
        self.assertFalse(r["ok"])
        self.assertFalse(r["wrote"])
        self.assertIn("unsafe", r["error"].lower())

    def test_idempotent_when_columns_present(self):
        row2 = list(_ROW2)
        row2.insert(7, "VIEWS")            # a target column already exists
        row1 = list(_ROW1)
        row1.insert(7, "")
        sa._poc_values = lambda: [row1, row2]
        r = sa.insert_poc_metric_columns(apply=True)     # must NOT insert
        self.assertFalse(r["wrote"])
        self.assertIn("VIEWS", r["already_present"])

    def setUp(self):
        super().setUp()
        self._pv = sa._poc_values

    def tearDown(self):
        sa._poc_values = self._pv
        super().tearDown()


# --------------------------------------------------------------------------- #
# Task 3 — staging tab schema
# --------------------------------------------------------------------------- #
class TestStagingSchema(unittest.TestCase):
    def test_staging_columns_exact(self):
        self.assertEqual(list(sa.STAGING_COLUMNS), [
            "LINK", "REEL_TYPE", "DURATION_SECONDS", "POST_DATE", "VIEWS", "LIKES", "COMMENTS",
            "SAVES", "SHARES", "ENGAGEMENT_RATE", "FOLLOWERS_AT_POST", "AGE_SPLIT", "GENDER_SPLIT",
            "LOCATION_SPLIT", "FOLLOWER_NONFOLLOWER_SPLIT", "REACH", "IMPRESSIONS",
            "PROFILE_VISITS", "WEBSITE_CLICKS", "PRODUCT_CLICKS", "TRIAL_CLICKS", "QUALIFIED_DMS",
            "SOURCE", "IMPORTED_AT", "NOTES"])
        self.assertEqual(sa.STAGING_TAB, "SOCIAL_METRICS_IMPORT_STAGING")


# --------------------------------------------------------------------------- #
# Task 4 — validation
# --------------------------------------------------------------------------- #
class TestValidation(unittest.TestCase):
    def test_numeric_fields(self):
        self.assertEqual(sa.validate_import_value("VIEWS", "12,000")[0], True)
        self.assertEqual(sa.validate_import_value("VIEWS", "abc")[0], False)
        self.assertEqual(sa.validate_import_value("ENGAGEMENT_RATE", "4.2%")[0], True)

    def test_split_fields(self):
        ok, parsed = sa.validate_import_value("GENDER_SPLIT", "F 58% / M 42%")
        self.assertTrue(ok)
        self.assertEqual(parsed, {"F": 58.0, "M": 42.0})
        self.assertFalse(sa.validate_import_value("AGE_SPLIT", "nonsense")[0])

    def test_text_and_empty(self):
        self.assertEqual(sa.validate_import_value("REEL_TYPE", "Trial"), (True, "Trial"))
        self.assertEqual(sa.validate_import_value("VIEWS", ""), (True, ""))     # empty is skip-ok


# --------------------------------------------------------------------------- #
# Task 5 — dry-run import
# --------------------------------------------------------------------------- #
class TestDryRunImport(_Guard):
    def _wire(self, staging_rows, poc_rows, poc_cols):
        sa._internal_sheet = lambda: ([dict(r) for r in poc_rows], list(poc_cols), "")
        sa._read_named_worksheet = lambda title: (
            [list(sa.STAGING_COLUMNS)] + staging_rows if title == sa.STAGING_TAB else None)

    def test_dry_run_matches_and_reports_unmatched(self):
        poc = [_poc(3, "https://ig/a", "Great"), _poc(4, "https://ig/b", "Ok")]
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status"]
        # one matched (a), one unmatched (z)
        srow_a = ["https://ig/a"] + [""] * (len(sa.STAGING_COLUMNS) - 1)
        srow_a[sa.STAGING_COLUMNS.index("VIEWS")] = "20000"
        srow_z = ["https://ig/z"] + [""] * (len(sa.STAGING_COLUMNS) - 1)
        srow_z[sa.STAGING_COLUMNS.index("VIEWS")] = "5000"
        self._wire([srow_a, srow_z], poc, cols)
        rep = sa.import_social_metrics_dry_run()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["matched"], 1)
        self.assertIn("https://ig/z", rep["unmatched"])
        self.assertEqual(rep["would_fill"].get("VIEWS"), 1)

    def test_dry_run_never_overwrites_populated_cell(self):
        # POC already HAS a VIEWS column with a value for row a
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product",
                "Status", "VIEWS"]
        poc = [_poc(3, "https://ig/a", "Great", VIEWS="999")]
        srow = ["https://ig/a"] + [""] * (len(sa.STAGING_COLUMNS) - 1)
        srow[sa.STAGING_COLUMNS.index("VIEWS")] = "20000"       # staging has a different value
        self._wire([srow], poc, cols)
        rep = sa.import_social_metrics_dry_run()
        self.assertEqual(rep["would_fill"].get("VIEWS"), None)   # NOT filled
        self.assertEqual(rep["already_populated"].get("VIEWS"), 1)

    def test_dry_run_reports_parse_errors(self):
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status"]
        poc = [_poc(3, "https://ig/a", "Great")]
        srow = ["https://ig/a"] + [""] * (len(sa.STAGING_COLUMNS) - 1)
        srow[sa.STAGING_COLUMNS.index("LIKES")] = "not-a-number"
        self._wire([srow], poc, cols)
        rep = sa.import_social_metrics_dry_run()
        self.assertEqual(len(rep["parse_errors"]), 1)
        self.assertEqual(rep["parse_errors"][0]["column"], "LIKES")

    def test_dry_run_writes_nothing(self):
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status"]
        poc = [_poc(3, "https://ig/a", "Great")]
        snap = copy.deepcopy(poc)
        srow = ["https://ig/a"] + [""] * (len(sa.STAGING_COLUMNS) - 1)
        srow[sa.STAGING_COLUMNS.index("VIEWS")] = "20000"
        self._wire([srow], poc, cols)
        out = sa.render_import_dry_run(sa.import_social_metrics_dry_run())
        self.assertIn("nothing was written", out.lower())
        self.assertEqual(poc, snap, "dry-run must not mutate POC rows")

    def test_no_write_mode_exists_in_cli(self):
        import main
        rc = main.cmd_import_social_metrics(dry_run=False)
        self.assertEqual(rc, 1)     # refuses; dry-run only

    def test_missing_staging_tab_is_clean(self):
        sa._internal_sheet = lambda: ([_poc(3, "https://ig/a", "Great")],
                                      ["LINK", "PERFORMANCE", "Status"], "")
        sa._read_named_worksheet = lambda title: None
        rep = sa.import_social_metrics_dry_run()
        self.assertFalse(rep["ok"])
        self.assertIn("not found", rep["error"])


# --------------------------------------------------------------------------- #
# Task 6 — duration-bucket audit
# --------------------------------------------------------------------------- #
class TestDurationBucketAudit(_Guard):
    def test_bucket_report_and_best_bucket(self):
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status"]
        rows = [_poc(3, "https://ig/a", "Great"), _poc(4, "https://ig/b", "Great"),
                _poc(5, "https://ig/c", "Underdog"), _poc(6, "https://ig/d", "Ok")]
        buckets = {"https://ig/a": "10-22 sec", "https://ig/b": "10-22 sec",
                   "https://ig/c": "> 60 sec", "https://ig/d": "10-22 sec"}
        a = sa.audit_duration_buckets(rows, cols, audit_buckets=buckets)
        self.assertEqual(a["rows_with_bucket"], 4)
        self.assertEqual(a["distribution"]["10-22 sec"]["Great"], 2)
        self.assertEqual(a["best_bucket"], "10-22 sec")
        out = sa.render_duration_bucket_audit(a)
        self.assertIn("not exact seconds", out.lower())
        self.assertIn("10-22 sec", out)

    def test_no_buckets_is_honest(self):
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status"]
        rows = [_poc(3, "https://ig/a", "Great")]
        a = sa.audit_duration_buckets(rows, cols, audit_buckets={})
        self.assertEqual(a["rows_with_bucket"], 0)
        self.assertIn("No Content audit duration buckets", sa.render_duration_bucket_audit(a))

    def test_audit_does_not_mutate_rows(self):
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status"]
        rows = [_poc(3, "https://ig/a", "Great")]
        snap = copy.deepcopy(rows)
        sa.audit_duration_buckets(rows, cols, audit_buckets={"https://ig/a": "< 10 sec"})
        self.assertEqual(rows, snap)


# --------------------------------------------------------------------------- #
# Task 7 — Slack routing
# --------------------------------------------------------------------------- #
class TestSlackRouting(_Guard):
    def setUp(self):
        super().setUp()
        cols = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product", "Status"]
        rows = [_poc(3, "https://ig/a", "Great")]
        sa._internal_sheet = lambda: ([dict(r) for r in rows], list(cols), "")
        sa._read_named_worksheet = lambda title: None

    def test_routing_flags(self):
        for q in ("what metrics are missing?", "how do I import IG metrics?",
                  "can we use the Content audit duration buckets?",
                  "what duration bucket performs best?", "what fields do we need to add?"):
            self.assertTrue(sa.is_social_analytics_query(q), q)

    def test_missing_metrics_answer(self):
        out = sa.answer_social_analytics_question("what metrics are missing?")
        self.assertIn("missing", out.lower())
        self.assertIn("SOCIAL_METRICS_IMPORT_STAGING", out)

    def test_import_howto_answer(self):
        out = sa.answer_social_analytics_question("how do I import IG metrics?")
        self.assertIn("SOCIAL_METRICS_IMPORT_STAGING", out)
        self.assertIn("dry-run", out.lower())

    def test_bucket_usability_answer(self):
        out = sa.answer_social_analytics_question("can we use the Content audit duration buckets?")
        self.assertIn("proxy", out.lower())

    def test_best_bucket_answer_honest_without_data(self):
        out = sa.answer_social_analytics_question("what duration bucket performs best?")
        # offline (no content audit) -> honest "don't have" answer, never fabricated
        self.assertIn("don't have", out.lower())

    def test_missing_metrics_does_not_hijack_content_gap(self):
        # "what are we missing" (content-gap skill) must NOT be caught here
        self.assertFalse(any(k in " what are we missing in the calendar? "
                             for k in sa._MISSING_METRICS_KW))


if __name__ == "__main__":
    unittest.main()
