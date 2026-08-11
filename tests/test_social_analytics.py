"""Unit tests for the Social Analytics + Creative Test Planning layer.

Proves the honesty guarantees the feature is built around:
  * the metrics audit reports available/missing fields truthfully;
  * a demographic question never invents demographic data;
  * trial vs standard compares only fields that exist;
  * duration analysis uses real duration when present, and gives a clear
    missing-data + backfill answer when absent;
  * highest-performing reels use the best available metric (hierarchy);
  * the 20-test plan is internal-anchored + external-reference + bridge, never
    treats external inspiration as proof, and labels KPI as proxy/inferred;
  * nothing writes the Sheet/Notion or mutates internal rows.

Run: python -m unittest tests.test_social_analytics
"""
import copy
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import slack_response_style as st
import social_analytics as sa
import taxonomy


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _row(n, perf, product="Gloves", icp="Aspiring Pro", hook=None, fmt=None, **extra):
    r = {"_row": n, "LINK": f"https://www.instagram.com/storellisoccer/reel/{n}/",
         "PERFORMANCE": perf, "Storytelling structure": "Demo", "ICP": icp,
         "Product": product, "Status": "completed"}
    for c in taxonomy.all_signal_columns():
        r[c] = ""
    if hook:
        r[taxonomy.column_for("hook", hook)] = "1"
    if fmt:
        r[taxonomy.column_for("format", fmt)] = "1"
    r.update(extra)
    return r


# rows WITHOUT demographics and WITHOUT duration, but WITH a Reel Type + Views
_COLS_BASE = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product",
              "Status", "Reel Type", "Views"]
_ROWS_BASE = [
    _row(3, "Great", hook="Curiosity Gap", fmt="Demo", **{"Reel Type": "Trial", "Views": "20000"}),
    _row(4, "Underdog", hook="Fear / Risk", fmt="Story", **{"Reel Type": "Standard", "Views": "3000"}),
    _row(5, "Good", hook="Curiosity Gap", fmt="Demo", **{"Reel Type": "Trial", "Views": "9000"}),
    _row(6, "Ok", hook="Authority", fmt="Tutorial", **{"Reel Type": "Standard", "Views": "6000"}),
]

_BRAIN = {
    "profiles": [
        {"PROFILE_ID": "WFP-bs", "ACTIVE": "TRUE", "CONFIDENCE": "High",
         "PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur: Curiosity Gap + Demo",
         "PRODUCT": "BodyShield GK Leggings", "ICP": "Adult Amateur",
         "HOOK_TAGS": "Curiosity Gap, Fear / Risk", "FORMAT_TAGS": "Demo",
         "INTERNAL_SAMPLE_SIZE": "6",
         "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/"},
        {"PROFILE_ID": "WFP-gk", "ACTIVE": "TRUE", "CONFIDENCE": "Medium",
         "PROFILE_NAME": "Gloves / Aspiring Pro: Authority + Tutorial",
         "PRODUCT": "Gloves", "ICP": "Aspiring Pro", "HOOK_TAGS": "Authority",
         "FORMAT_TAGS": "Tutorial", "INTERNAL_SAMPLE_SIZE": "4",
         "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/BBB/"},
    ],
    "connections": [
        {"CONNECTION_ID": "SC-1", "CONCEPT_NAME": "BodyShield turf-burn protection",
         "PRODUCT": "BodyShield GK Leggings", "CONNECTION_SCORE": "89",
         "STORYTELLING_STRUCTURE": "Curiosity Gap → Demo → Pain Reveal → Protection Reveal → CTA",
         "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1"},
    ],
    "ideas": [
        {"IDEA_ID": "IDEA-bs-1", "IDEA_SCORE": "94", "PRODUCT": "BodyShield GK Leggings",
         "ICP": "Adult Amateur", "REFINED_IDEA_TITLE": "Dive Without The Sting",
         "HOOK_TAGS": "Curiosity Gap", "FORMAT_TAGS": "Demo",
         "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/"},
    ],
    "calendar": [],
}


# --------------------------------------------------------------------------- #
# Part A — metrics audit
# --------------------------------------------------------------------------- #
class TestMetricsAudit(unittest.TestCase):
    def test_audit_reports_available_and_missing_honestly(self):
        a = sa.audit_metrics_schema(rows=_ROWS_BASE, columns=_COLS_BASE)
        self.assertTrue(a["ok"])
        # present in the fixture
        for f in ("url", "performance_label", "views", "reel_type", "product", "icp",
                  "hook", "format"):
            self.assertIn(f, a["available"], f"{f} should be reported available")
        # genuinely absent
        for f in ("duration", "comments", "saves", "engagement_rate",
                  "demo_age", "demo_gender", "demo_location"):
            self.assertIn(f, a["missing"], f"{f} should be reported missing")
        self.assertFalse(a["demographics_present"])
        self.assertFalse(a["duration_present"])
        self.assertTrue(a["reel_type_classifiable"])

    def test_audit_degrades_cleanly_when_sheet_unreachable(self):
        a = sa.audit_metrics_schema(rows=[], columns=[])
        # no columns => everything missing, but no crash and honest report
        self.assertIn("duration", a["missing"])
        self.assertFalse(a["demographics_present"])

    def test_metric_hierarchy_selection(self):
        # engagement rate wins when present
        self.assertEqual(sa.choose_metric(
            sa.detect_available_metrics(["Engagement Rate", "Views", "Likes"], [])), "engagement_rate")
        # then saves/comments/shares
        self.assertEqual(sa.choose_metric(
            sa.detect_available_metrics(["Saves", "Views"], [])), "saves")
        # then views/likes
        self.assertEqual(sa.choose_metric(
            sa.detect_available_metrics(["Views"], [])), "views")
        # falls back to the manual label when no raw metric exists
        self.assertEqual(sa.choose_metric(
            sa.detect_available_metrics(["PERFORMANCE"], [])), "performance_label")


# --------------------------------------------------------------------------- #
# Part A — reel classification + top performers
# --------------------------------------------------------------------------- #
class TestClassificationAndTopPosts(unittest.TestCase):
    def test_classify_reel_type(self):
        self.assertEqual(sa.classify_reel_type({"Reel Type": "Trial"}), "trial")
        self.assertEqual(sa.classify_reel_type({"Reel Type": "Standard"}), "standard")
        # keyword fallback in the storytelling text
        self.assertEqual(sa.classify_reel_type({"Storytelling structure": "trial reel test"}), "trial")
        # never guesses
        self.assertEqual(sa.classify_reel_type({"Storytelling structure": "Demo"}), "unknown")

    def test_top_posts_use_best_available_metric(self):
        top = sa.find_top_performing_posts(_ROWS_BASE, _COLS_BASE, limit=2)
        self.assertEqual(top[0]["metric"], "views")
        self.assertEqual(top[0]["value"], 20000.0)      # the Great/20k reel first
        self.assertEqual(len(top), 2)

    def test_top_posts_fall_back_to_label_without_raw_metrics(self):
        cols = ["LINK", "PERFORMANCE"]
        rows = [_row(3, "Underdog"), _row(4, "Great"), _row(5, "Ok")]
        # strip Views/Reel Type so only the label remains
        for r in rows:
            r.pop("Views", None)
        top = sa.find_top_performing_posts(rows, cols, limit=1)
        self.assertEqual(top[0]["metric"], "performance_label")
        self.assertEqual(top[0]["performance"], "Great")

    def test_external_rows_never_counted(self):
        rows = _ROWS_BASE + [_row(9, "Great", **{"Reel Type": "Trial", "Views": "999999",
                                                  "Source Type": "External"})]
        top = sa.find_top_performing_posts(rows, _COLS_BASE + ["Source Type"], limit=5)
        self.assertTrue(all("/9/" not in t["link"] for t in top),
                        "external/inspiration row must not appear as a top Storelli post")


# --------------------------------------------------------------------------- #
# Part B — trial vs standard + demographic honesty
# --------------------------------------------------------------------------- #
class TestTrialVsStandard(unittest.TestCase):
    def test_demographic_question_does_not_hallucinate(self):
        cmp = sa.compare_trial_vs_standard(_ROWS_BASE, _COLS_BASE)
        self.assertTrue(cmp["classifiable"])
        self.assertFalse(cmp["demographics_present"])
        self.assertNotIn("demographics", cmp["comparisons"])   # no fabricated demo dim
        out = sa._render_trial_vs_standard(
            "trial reels vs standard reels in terms of demographic", cmp)
        low = out.lower()
        self.assertTrue("not demographic" in low or "demographic fields aren't" in low
                        or "demographic fields not" in low)
        # what it CAN compare is named
        self.assertIn("performance", low)
        # never claims an age/gender/location value it doesn't have
        self.assertNotRegex(low, r"\b\d+%\s*(male|female|men|women)\b")

    def test_compare_uses_only_available_fields(self):
        cmp = sa.compare_trial_vs_standard(_ROWS_BASE, _COLS_BASE)
        dims = cmp["comparisons"]
        self.assertIn("performance", dims)
        self.assertIn("hook_format", dims)
        self.assertIn("product", dims)
        self.assertNotIn("duration", dims)               # no duration column -> not compared
        self.assertEqual(cmp["n_trial"], 2)
        self.assertEqual(cmp["n_standard"], 2)

    def test_not_classifiable_says_so(self):
        # no Reel Type column and no 'trial' keyword anywhere
        cols = ["LINK", "PERFORMANCE", "Product", "ICP", "Storytelling structure"]
        rows = [_row(3, "Great"), _row(4, "Ok")]
        for r in rows:
            r.pop("Reel Type", None)
        cmp = sa.compare_trial_vs_standard(rows, cols)
        self.assertFalse(cmp["classifiable"])
        out = sa._render_trial_vs_standard("trial vs standard difference", cmp)
        self.assertIn("Reel Type", out)                  # recommends the exact field
        self.assertIn("can't split", out.lower())

    def test_duration_compared_when_present(self):
        cols = _COLS_BASE + ["Duration"]
        rows = [_row(3, "Great", hook="Curiosity Gap", fmt="Demo",
                     **{"Reel Type": "Trial", "Views": "20000", "Duration": "8"}),
                _row(4, "Ok", hook="Authority", fmt="Tutorial",
                     **{"Reel Type": "Standard", "Views": "6000", "Duration": "22"})]
        cmp = sa.compare_trial_vs_standard(rows, cols)
        self.assertIn("duration", cmp["comparisons"])
        self.assertEqual(cmp["comparisons"]["duration"]["trial"], 8.0)


# --------------------------------------------------------------------------- #
# Part C — duration
# --------------------------------------------------------------------------- #
class TestDuration(unittest.TestCase):
    def test_missing_duration_is_explicit_with_backfill(self):
        # inject empty buckets so no live Content audit read is attempted
        d = sa.analyze_winning_reel_duration(_ROWS_BASE, _COLS_BASE, audit_buckets={})
        self.assertFalse(d["duration_available"])
        self.assertEqual(d["source"], "none")
        self.assertEqual(d["backfill_field"], "DURATION_SECONDS")
        self.assertIn("yt-dlp", d["recommended_source"])
        out = sa._render_duration("how many seconds long are our highest-performing reels?", d)
        low = out.lower()
        self.assertIn("no duration field", low)
        self.assertIn("yt-dlp", low)
        self.assertNotRegex(low, r"median\s*\d")         # never invents a number

    def test_real_duration_used_when_present(self):
        cols = _COLS_BASE + ["Duration"]
        rows = [_row(3, "Great", **{"Reel Type": "Trial", "Views": "20000", "Duration": "0:08"}),
                _row(4, "Great", **{"Reel Type": "Trial", "Views": "18000", "Duration": "12"}),
                _row(5, "Good", **{"Reel Type": "Trial", "Views": "9000", "Duration": "7s"}),
                _row(6, "Ok", **{"Reel Type": "Standard", "Views": "6000", "Duration": "0:22"})]
        d = sa.analyze_winning_reel_duration(rows, cols)
        self.assertTrue(d["duration_available"])
        self.assertEqual(d["common_bucket"], "6–10 sec")
        self.assertGreater(d["count"], 0)
        out = sa._render_duration("how long are our best reels?", d)
        self.assertIn("6–10 sec", out)
        self.assertIn("*Sources:*", out)

    def test_duration_parsing(self):
        self.assertEqual(sa._parse_seconds("12"), 12)
        self.assertEqual(sa._parse_seconds("12s"), 12)
        self.assertEqual(sa._parse_seconds("0:08"), 8)
        self.assertEqual(sa._parse_seconds("00:01:03"), 63)
        self.assertIsNone(sa._parse_seconds(""))


# --------------------------------------------------------------------------- #
# Part D — creative test plan
# --------------------------------------------------------------------------- #
class TestCreativeTestPlan(unittest.TestCase):
    def test_plan_is_internal_anchored_external_reference_bridge(self):
        plan = sa.generate_creative_test_plan(count=20, brain=copy.deepcopy(_BRAIN))
        self.assertGreater(plan["returned"], 0)
        for t in plan["tests"]:
            self.assertTrue(t["internal_basis"], "every test needs an internal anchor")
            self.assertTrue(t["hypothesis"], "every test needs a learning hypothesis")
            self.assertTrue(t["structure"], "every test needs a creative bridge/structure")
            self.assertIn("proxy", t["kpi_proxy"].lower() + " proxy")
        # at least one test carries an external execution reference
        self.assertTrue(any(t["external_urls"] for t in plan["tests"]))

    def test_plan_reaches_requested_count_with_rich_brain(self):
        rich = {"profiles": [dict(_BRAIN["profiles"][0], PROFILE_ID=f"P{i}",
                                  PRODUCT=p, PROFILE_NAME=f"{p} profile")
                             for i, p in enumerate(["BodyShield GK Leggings", "Gloves",
                                                    "Sliders", "ExoShield Head Guard"])],
                "connections": _BRAIN["connections"], "ideas": _BRAIN["ideas"], "calendar": []}
        plan = sa.generate_creative_test_plan(count=20, brain=rich)
        self.assertEqual(plan["returned"], 20)
        self.assertFalse(plan["short"])

    def test_external_never_framed_as_proof(self):
        out = sa._render_test_plan("give me a list of 20 ideas we should test",
                                   sa.generate_creative_test_plan(20, brain=copy.deepcopy(_BRAIN)))
        self.assertIn(sa._NOT_PROOF, out)
        self.assertNotRegex(out.lower(),
                            r"(external|inspiration|reference)[^.]{0,40}prov(e|es|en|ing)")

    def test_kpi_labeled_proxy_and_no_hard_metrics(self):
        out = sa._render_test_plan("give me a list of 20 ideas we should test",
                                   sa.generate_creative_test_plan(20, brain=copy.deepcopy(_BRAIN)))
        self.assertIn("(proxy)", out)
        self.assertNotRegex(out.lower(), r"\b\d+\s*(comments|saves|shares|likes)\b")

    def test_plan_renders_numbered_and_sources_at_bottom(self):
        out = sa._render_test_plan("give me a list of 20 ideas we should test",
                                   sa.generate_creative_test_plan(20, brain=copy.deepcopy(_BRAIN)))
        body, src = st.split_sources(out)
        self.assertRegex(body, r"(?m)^1\. ")
        self.assertTrue(src.startswith("*Sources:*") or "*Sources:*" in out)
        self.assertLess(out.index("1."), out.index("*Sources:*"))

    def test_thin_brain_is_honest_not_padded(self):
        plan = sa.generate_creative_test_plan(20, brain={"profiles": [], "connections": [],
                                                         "ideas": [], "calendar": []})
        self.assertEqual(plan["returned"], 0)
        out = sa._render_test_plan("give me 20 ideas to test", plan)
        self.assertIn("enough internal evidence", out.lower())


# --------------------------------------------------------------------------- #
# read-only guarantees
# --------------------------------------------------------------------------- #
class TestReadOnly(unittest.TestCase):
    def test_functions_do_not_mutate_internal_rows(self):
        rows = copy.deepcopy(_ROWS_BASE)
        snapshot = copy.deepcopy(rows)
        sa.audit_metrics_schema(rows=rows, columns=_COLS_BASE)
        sa.compare_trial_vs_standard(rows, _COLS_BASE)
        sa.analyze_winning_reel_duration(rows, _COLS_BASE)
        sa.find_top_performing_posts(rows, _COLS_BASE)
        self.assertEqual(rows, snapshot, "analytics must never mutate internal rows")

    def test_plan_never_calls_a_write_method(self):
        class _ExplodingSheets:
            def read_profiles(self):
                return _BRAIN["profiles"]

            def read_semantic_connections(self):
                return _BRAIN["connections"]

            def read_ideas(self):
                return _BRAIN["ideas"]

            def read_calendar_ratings(self):
                return []

            def __getattr__(self, name):
                if name.startswith(("upsert_", "append_", "update_", "ensure_")):
                    raise AssertionError(f"test plan must not call write method {name}")
                raise AttributeError(name)

        import inspiration_sheets
        real = inspiration_sheets.InspirationSheets
        inspiration_sheets.InspirationSheets = _ExplodingSheets
        try:
            plan = sa.generate_creative_test_plan(count=10)      # uses _brain() -> _ExplodingSheets
            self.assertGreater(plan["returned"], 0)
        finally:
            inspiration_sheets.InspirationSheets = real


# --------------------------------------------------------------------------- #
# Hardening patch: aliases, content-audit fallback, demographics, schema, CLI
# --------------------------------------------------------------------------- #
class _GlobalPatchCase(unittest.TestCase):
    """Restores the module's two network indirections after tests that patch them."""

    def setUp(self):
        self._orig_internal = sa._internal_sheet
        self._orig_named_ws = sa._read_named_worksheet

    def tearDown(self):
        sa._internal_sheet = self._orig_internal
        sa._read_named_worksheet = self._orig_named_ws


class TestAliasPatch(unittest.TestCase):
    def test_recommended_column_names_are_recognized(self):
        cols = ["LINK", "PERFORMANCE", "DURATION_SECONDS", "FOLLOWERS_AT_POST", "AGE_SPLIT",
                "GENDER_SPLIT", "LOCATION_SPLIT", "FOLLOWER_NONFOLLOWER_SPLIT", "REACH",
                "IMPRESSIONS", "PROFILE_VISITS", "WEBSITE_CLICKS", "PRODUCT_CLICKS",
                "TRIAL_CLICKS", "QUALIFIED_DMS", "POST_DATE"]
        av = sa.detect_available_metrics(cols, [])
        expect = {"duration": "DURATION_SECONDS", "followers": "FOLLOWERS_AT_POST",
                  "demo_age": "AGE_SPLIT", "demo_gender": "GENDER_SPLIT",
                  "demo_location": "LOCATION_SPLIT",
                  "demo_follower_split": "FOLLOWER_NONFOLLOWER_SPLIT", "reach": "REACH",
                  "impressions": "IMPRESSIONS", "profile_visits": "PROFILE_VISITS",
                  "website_clicks": "WEBSITE_CLICKS", "product_clicks": "PRODUCT_CLICKS",
                  "trial_clicks": "TRIAL_CLICKS", "qualified_dms": "QUALIFIED_DMS",
                  "date": "POST_DATE"}
        for field, col in expect.items():
            self.assertTrue(av[field]["available"], f"{field} not recognized")
            self.assertEqual(av[field]["column"], col)

    def test_backward_compatible_short_aliases_still_work(self):
        av = sa.detect_available_metrics(
            ["duration", "followers", "age", "gender", "location", "follower split"], [])
        for field in ("duration", "followers", "demo_age", "demo_gender", "demo_location",
                      "demo_follower_split"):
            self.assertTrue(av[field]["available"], f"{field} short alias broke")


class TestAuditReadOnly(_GlobalPatchCase):
    def test_audit_reports_coverage_and_comparison_capability(self):
        a = sa.audit_metrics_schema(rows=_ROWS_BASE, columns=_COLS_BASE)
        self.assertIn("coverage", a)
        self.assertEqual(a["coverage"]["views"], 100)
        self.assertFalse(a["demographic_comparison_possible"])
        self.assertIn("AGE_SPLIT", a["missing_demographics"])
        self.assertIn("DURATION_SECONDS", a["recommended_missing"])

    def test_audit_report_is_read_only(self):
        rows = copy.deepcopy(_ROWS_BASE)
        snap = copy.deepcopy(rows)
        sa._internal_sheet = lambda: ([dict(r) for r in rows], list(_COLS_BASE), "")
        sa._read_named_worksheet = lambda title: None
        try:
            report = sa.audit_social_metrics_report()
        finally:
            pass
        self.assertIn("read-only", report.lower())
        self.assertIn("nothing written", report.lower())
        self.assertEqual(rows, snap, "audit must not mutate rows")


class TestContentAuditFallback(_GlobalPatchCase):
    def _rows(self):
        return [_row(3, "Great", hook="Curiosity Gap", fmt="Demo",
                     **{"Reel Type": "Trial", "Views": "20000"}),
                _row(5, "Good", hook="Curiosity Gap", fmt="Demo",
                     **{"Reel Type": "Trial", "Views": "9000"})]

    def test_exact_duration_preferred_over_buckets(self):
        cols = _COLS_BASE + ["Duration"]
        rows = [_row(3, "Great", **{"Reel Type": "Trial", "Views": "20000", "Duration": "8"})]
        d = sa.analyze_winning_reel_duration(
            rows, cols, audit_buckets={"https://www.instagram.com/storellisoccer/reel/3/": "> 60 sec"})
        self.assertEqual(d["source"], "exact")     # exact wins even when a bucket exists

    def test_content_audit_bucket_fallback(self):
        rows = self._rows()
        buckets = {"https://www.instagram.com/storellisoccer/reel/3/": "10-22 sec",
                   "https://www.instagram.com/storellisoccer/reel/5/": "10-22 sec"}
        d = sa.analyze_winning_reel_duration(rows, _COLS_BASE, audit_buckets=buckets)
        self.assertEqual(d["source"], "content_audit_bucket")
        self.assertEqual(d["dominant_bucket"], "10-22 sec")
        out = sa._render_duration("how long are our best reels?", d)
        low = out.lower()
        self.assertIn("exact duration seconds yet", low)
        self.assertTrue("bucket" in low)
        self.assertIn("not exact seconds", low)        # clearly labelled proxy

    def test_missing_when_no_buckets(self):
        d = sa.analyze_winning_reel_duration(self._rows(), _COLS_BASE, audit_buckets={})
        self.assertEqual(d["source"], "none")

    def test_content_audit_reader_parses_buckets(self):
        sa._read_named_worksheet = lambda title: [
            ["ID", "LINK", "overall_videoLength_< 10 sec", "overall_videoLength_10-22 sec"],
            ["1", "https://ig/a", "0", "1"], ["2", "https://ig/b", "1", "0"]]
        try:
            got = sa.content_audit_duration_buckets({"https://ig/a", "https://ig/b"})
        finally:
            pass
        self.assertEqual(got, {"https://ig/a": "10-22 sec", "https://ig/b": "< 10 sec"})


class TestDemographicParser(unittest.TestCase):
    def test_parse_split_variants(self):
        self.assertEqual(sa._parse_split("F 58% / M 42%"), {"F": 58.0, "M": 42.0})
        self.assertEqual(sa._parse_split("18-24 34% / 25-34 41%"), {"18-24": 34.0, "25-34": 41.0})
        self.assertEqual(sa._parse_split("US 60% / UK 12%"), {"US": 60.0, "UK": 12.0})
        self.assertEqual(sa._parse_split(""), {})
        self.assertEqual(sa._parse_split("no percentages here"), {})

    def test_demographic_comparison_when_columns_exist(self):
        cols = _COLS_BASE + ["GENDER_SPLIT"]
        rows = [_row(3, "Great", hook="Curiosity Gap", fmt="Demo",
                     **{"Reel Type": "Trial", "Views": "20000", "GENDER_SPLIT": "F 60% / M 40%"}),
                _row(6, "Ok", hook="Authority", fmt="Tutorial",
                     **{"Reel Type": "Standard", "Views": "6000", "GENDER_SPLIT": "F 45% / M 55%"})]
        cmp = sa.compare_trial_vs_standard(rows, cols)
        self.assertTrue(cmp["demographic_comparison_possible"])
        self.assertIn("demo_gender", cmp["demographics"])
        self.assertEqual(cmp["demographics"]["demo_gender"]["trial"], {"F": 60.0, "M": 40.0})
        out = sa._render_trial_vs_standard("trial vs standard demographic", cmp)
        self.assertNotIn("aren't in the current data", out)   # it DID compare demographics
        self.assertIn("Gender", out)

    def test_missing_demographics_still_honest(self):
        cmp = sa.compare_trial_vs_standard(_ROWS_BASE, _COLS_BASE)
        self.assertFalse(cmp["demographic_comparison_possible"])
        out = sa._render_trial_vs_standard("trial vs standard demographic", cmp)
        low = out.lower()
        self.assertIn("not demographic", low)
        self.assertIn("AGE_SPLIT", out)                       # names the exact missing columns


class TestSchemaPlan(unittest.TestCase):
    def test_schema_plan_all_has_insertion_location(self):
        out = sa.render_schema_plan("what fields do we need to add?")
        self.assertIn("between `Status` and the first taxonomy category `HOOK`", out)
        self.assertIn("row 1", out.lower())
        self.assertIn("DURATION_SECONDS", out)
        self.assertIn("AGE_SPLIT", out)

    def test_schema_plan_demographics_needs_ig_insights(self):
        out = sa.render_schema_plan("what do we need to track to answer demographics?")
        self.assertIn("AGE_SPLIT", out)
        self.assertIn("IG Insights", out)
        self.assertIn("between `Status` and the first taxonomy category `HOOK`", out)

    def test_schema_plan_duration_names_column_and_backfill(self):
        out = sa.render_schema_plan("what do we need to track to answer reel duration?")
        self.assertIn("DURATION_SECONDS", out)
        self.assertIn("yt-dlp", out)

    def test_schema_routing_precedence(self):
        # a schema ask that mentions "duration" must route to schema, not the data answer
        self.assertTrue(sa.is_schema_plan_query("what do we need to track to answer reel duration?"))
        self.assertEqual(sa._schema_focus("what do we need to track to answer reel duration?"),
                         "duration")
        self.assertEqual(sa._schema_focus("what fields do we need for demographics?"), "demographics")


class TestBackfillDryRun(_GlobalPatchCase):
    def test_dry_run_lists_candidates_and_never_writes(self):
        rows = copy.deepcopy(_ROWS_BASE)
        snap = copy.deepcopy(rows)
        sa._internal_sheet = lambda: ([dict(r) for r in rows], list(_COLS_BASE), "")
        out = sa.backfill_duration_dry_run(probe=False)
        self.assertIn("NO WRITES", out)
        self.assertIn("could receive DURATION_SECONDS", out)
        self.assertIn("nothing was written", out.lower())
        self.assertEqual(rows, snap, "dry-run must not mutate rows")

    def test_candidates_exclude_external_and_already_filled(self):
        cols = _COLS_BASE + ["DURATION_SECONDS", "Source Type"]
        rows = [_row(3, "Great", **{"Reel Type": "Trial", "Views": "1", "DURATION_SECONDS": "8"}),
                _row(4, "Good", **{"Reel Type": "Trial", "Views": "1", "DURATION_SECONDS": ""}),
                _row(9, "Great", **{"Reel Type": "Trial", "Views": "1", "Source Type": "External"})]
        sa._internal_sheet = lambda: ([dict(r) for r in rows], list(cols), "")
        info = sa.duration_backfill_candidates()
        links = [c["link"] for c in info["candidates"]]
        self.assertIn("https://www.instagram.com/storellisoccer/reel/4/", links)   # empty -> candidate
        self.assertNotIn("https://www.instagram.com/storellisoccer/reel/3/", links)  # already filled
        self.assertNotIn("https://www.instagram.com/storellisoccer/reel/9/", links)  # external


if __name__ == "__main__":
    unittest.main()
