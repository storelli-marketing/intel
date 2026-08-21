"""Explicit-analytics routing, temporal/duration analytics, and citation integrity.

These lock in the two production failures this work fixes:

A. A clearly specified analytics question mid-thread was answered with a creative
   recommendation, because "highest performing" reads as an optimisation objective
   to the decision frame and the frame ran before the analytics router.

B. A "we don't have enough evidence" answer cited three unrelated Great reels,
   because the strategist attached the strongest pack sources whenever the model
   cited none.

The assertions are on the SHARED MECHANISM (which route owns the turn, which
metric is answered, whether a source is bound to a claim), not on wording.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

import analytics_query as AQ
import performance
import social_analytics as sa
import source_binding as SB
import taxonomy

_META = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product",
         "Status", "VIEWS", "COMMENTS", "DURATION_SECONDS", "POST_DATE",
         "POST_TIMESTAMP", "FOLLOWERS_AT_MEASUREMENT", "PERFORMANCE_SOURCE"]

# (row, perf, product, icp, hook, format, views, comments, seconds, timestamp)
# Great reels are deliberately SHORT and posted at 18:00 Monday; the rest are
# longer and posted in the morning, so every analytic below has a real signal to
# find and a real "not enough per window" case to refuse.
_SPEC = [
    (3,  "Great",    "BodyShield Leggings", "Adult Amateur", "Curiosity Gap", "Demo",       24000, 210, 11, "2025-01-06T18:30:00+00:00"),
    (4,  "Great",    "BodyShield Leggings", "Adult Amateur", "Curiosity Gap", "Demo",       21000, 180, 9,  "2025-01-13T18:10:00+00:00"),
    (5,  "Great",    "BodyShield Leggings", "Adult Amateur", "Fear / Risk",   "Demo",       19500, 160, 14, "2025-01-20T18:45:00+00:00"),
    (6,  "Good",     "BodyShield Leggings", "Adult Amateur", "Curiosity Gap", "Story",      12000, 60,  27, "2025-01-07T09:15:00+00:00"),
    (7,  "Great",    "BodyShield Leggings", "Aspiring Pro",  "Curiosity Gap", "Demo",       26000, 240, 8,  "2025-01-27T18:05:00+00:00"),
    (8,  "Underdog", "BodyShield Leggings", "Adult Amateur", "Humor",         "Reaction",   2600,  8,   41, "2025-02-03T09:40:00+00:00"),
    (9,  "Underdog", "BodyShield Leggings", "General",       "Aspiration",    "Story",      3100,  11,  38, "2025-02-04T10:05:00+00:00"),
    (10, "Ok",       "BodyShield Leggings", "Adult Amateur", "Social Proof",  "Comparison", 7400,  25,  33, "2025-02-05T09:30:00+00:00"),
    (11, "Great",    "GK Gloves",           "Aspiring Pro",  "Education",     "Tutorial",   22000, 190, 13, "2025-02-10T18:20:00+00:00"),
    (12, "Great",    "GK Gloves",           "Aspiring Pro",  "Authority",     "Tutorial",   20500, 175, 12, "2025-02-17T18:35:00+00:00"),
    (13, "Good",     "GK Gloves",           "Aspiring Pro",  "Education",     "Do / Don't", 11800, 55,  25, "2025-02-11T09:50:00+00:00"),
    (14, "Underdog", "GK Gloves",           "Aspiring Pro",  "Curiosity Gap", "Demo",       2900,  9,   44, "2025-02-18T09:20:00+00:00"),
    (15, "Ok",       "GK Gloves",           "Adult Amateur", "Education",     "Tutorial",   6900,  22,  30, "2025-02-24T10:10:00+00:00"),
    (16, "Good",     "ExoShield Head Guard", "Parents",      "Fear / Risk",   "Story",      9800,  40,  22, "2025-02-25T09:05:00+00:00"),
    (17, "Ok",       "ExoShield Head Guard", "Parents",      "Education",     "Tutorial",   5200,  18,  29, "2025-03-03T10:25:00+00:00"),
    (18, "Great",    "Sliders",             "Adult Amateur", "Curiosity Gap", "Demo",       18000, 150, 10, "2025-03-10T18:15:00+00:00"),
    (19, "Underdog", "Sliders",             "General",       "Humor",         "Reaction",   2200,  6,   47, "2025-03-11T09:35:00+00:00"),
    (20, "Ok",       "Sliders",             "Adult Amateur", "Education",     "Demo",       6100,  20,  31, "2025-03-17T10:00:00+00:00"),
]


def _rows(with_duration=True, with_timestamp=True):
    out = []
    for n, perf, product, icp, hook, fmt, views, comments, secs, ts in _SPEC:
        r = {"_row": n, "ID": str(n - 2),
             "LINK": f"https://www.instagram.com/storellisoccer/reel/BM{n:03d}/",
             "PERFORMANCE": perf, "Storytelling structure": "Demo", "ICP": icp,
             "Product": product, "Status": "completed", "VIEWS": str(views),
             "COMMENTS": str(comments), "POST_DATE": ts[:10],
             "FOLLOWERS_AT_MEASUREMENT": "171204", "PERFORMANCE_SOURCE": "HUMAN",
             "DURATION_SECONDS": str(secs) if with_duration else "",
             "POST_TIMESTAMP": ts if with_timestamp else ""}
        for c in taxonomy.all_signal_columns():
            r[c] = "0"
        r[taxonomy.column_for("hook", hook)] = "1"
        r[taxonomy.column_for("format", fmt)] = "1"
        out.append(r)
    return out


class _AnalyticsFixture(unittest.TestCase):
    """Points social_analytics' single data seam at the fixture above."""

    with_duration = True
    with_timestamp = True

    def setUp(self):
        self._real_sheet = sa._internal_sheet
        self._real_audit = sa.content_audit_duration_buckets
        rows = _rows(self.with_duration, self.with_timestamp)
        sa._internal_sheet = lambda: (rows, list(_META), "")
        sa.content_audit_duration_buckets = lambda links=None: {}
        self.rows = rows

    def tearDown(self):
        sa._internal_sheet = self._real_sheet
        sa.content_audit_duration_buckets = self._real_audit

    def ask(self, q):
        aq = AQ.parse(q)
        self.assertIsNotNone(aq, f"not recognized as an analytics question: {q}")
        out = sa.answer_analytics_query(aq, q, [])
        self.assertTrue(out, f"analytics returned nothing for: {q}")
        return out


# ---------------------------------------------------------------------------
# precedence: an explicit analytics question is never a frame continuation
# ---------------------------------------------------------------------------
class TestExplicitPrecedence(unittest.TestCase):
    def test_duration_question_is_explicit_analytics(self):
        aq = AQ.parse("ok sounds good... can u tell me however how many seconds "
                      "long are our highest performing reels?")
        self.assertIsNotNone(aq)
        self.assertEqual(aq["metric"], AQ.M_DURATION)
        self.assertEqual(aq["cohort"]["performance"], "Great")
        self.assertEqual(aq["scope_source"], "global")

    def test_every_spec_analytics_question_routes(self):
        for q in ("how many seconds long are our highest-performing reels?",
                  "which reels have the most comments?",
                  "what is our average reel length?",
                  "how many Great reels are under 10 seconds?",
                  "what day do our best reels get posted?",
                  "what is the median views count?",
                  "how many BodyShield reels do we have?",
                  "trial vs standard reels?",
                  "what performs better: POV or tutorial?"):
            self.assertTrue(AQ.is_explicit_analytics_question(q), q)

    def test_prescriptive_and_ambiguous_stay_with_existing_routes(self):
        """The frame must keep every turn it should still own."""
        for q in ("How long should the BodyShield concept we just discussed be?",
                  "what should we shoot this week?",
                  "what content is most likely to get comments?",
                  "tell me more about the second one",
                  "expand #2", "why?", "shorter",
                  "turn this into a shoot brief",
                  "are you sure? what would change your mind?"):
            self.assertIsNone(AQ.parse(q), f"wrongly captured: {q}")

    def test_frame_does_not_claim_an_explicit_analytics_turn(self):
        """conversation_agent enforces the same rule locally (defence in depth)."""
        import conversation_agent as CA
        ctx = [
            {"role": "user", "text": "what is working for BodyShield?"},
            {"role": "assistant",
             "text": "My read: *BodyShield Leggings* works with a *Curiosity Gap* "
                     "hook and *Demo* format for *Adult Amateur*. [S1] [S2]"},
        ]
        out = CA.answer("how many seconds long are our highest performing reels?",
                        ctx, key="")
        self.assertIsNone(out)
        self.assertEqual(CA.LAST_DEBUG.get("context_frame_ignored_reason"),
                         "explicit_analytics_question")

    def test_frame_still_owns_a_genuine_continuation(self):
        """The decision frame is untouched for what it is actually for."""
        import decision_frame as DF
        ctx = [
            {"role": "user", "text": "what is working for BodyShield?"},
            {"role": "assistant",
             "text": "My read: *BodyShield Leggings* works with a *Curiosity Gap* "
                     "hook and *Demo* format for the *Adult Amateur* audience. [S1]"},
        ]
        frame = DF.derive(ctx, "what should we shoot next week based on that?")
        self.assertTrue(DF.is_active(frame))
        self.assertTrue(DF.inherits("what should we shoot next week based on that?", frame))


# ---------------------------------------------------------------------------
# TEST A — the exact production conversation
# ---------------------------------------------------------------------------
class TestProductionConversationA(_AnalyticsFixture):
    T1 = ("Can you show the difference between posts on our trial reels vs "
          "standard reels / in terms of demographic views?")
    T2 = ("ok sounds good... can u tell me however how many seconds long are "
          "our highest performing reels?")

    def test_turn1_is_honest_about_unavailable_split(self):
        out = self.ask(self.T1)
        low = out.lower()
        self.assertTrue("trial" in low and "standard" in low)
        self.assertIn("can't", low)
        # No fabricated demographic split.
        self.assertNotRegex(low, r"\d+\s*%\s*(male|female|men|women|aged)")

    def test_turn2_answers_duration_not_ideas(self):
        out = self.ask(self.T2)
        low = out.lower()
        # answers the metric asked
        self.assertIn("median", low)
        self.assertRegex(low, r"\b\d+(\.\d+)?s\b")
        # reports the cohort and the coverage
        self.assertIn("great", low)
        self.assertRegex(low, r"\b7 reels?\b")
        # does NOT recommend creative work
        self.assertNotIn("dive without the sting", low)
        for banned in ("i'd shoot", "block the shoot day", "shoot day for"):
            self.assertNotIn(banned, low)

    def test_turn2_does_not_inherit_trial_standard_constraint(self):
        out = self.ask(self.T2)
        low = out.lower()
        self.assertNotIn("trial", low)
        self.assertNotIn("standard", low)

    def test_turn2_uses_exact_duration_and_states_the_definition(self):
        out = self.ask(self.T2)
        # exact seconds, and the "highest performing" definition is stated (§4)
        self.assertIn("reels currently classified Great", out)
        self.assertIn("11s", out)          # real median of the Great cohort

    def test_full_two_turn_conversation_through_the_real_entrypoint(self):
        """End-to-end through social_brain, with T1's answer in the thread."""
        import social_brain
        t1 = self.ask(self.T1)
        ctx = [{"role": "user", "text": self.T1},
               {"role": "assistant", "text": t1}]
        out = social_brain.answer_conversation(
            self.T2, ctx, channel_context={"thread_ts": "t-a", "channel": "C",
                                           "user": "U"})
        low = out.lower()
        self.assertIn("median", low)
        self.assertNotIn("dive without the sting", low)
        self.assertNotIn("shoot day", low)


# ---------------------------------------------------------------------------
# TEST B — missing-data answers cite nothing irrelevant
# ---------------------------------------------------------------------------
class TestPostingTimeNoIrrelevantSources(_AnalyticsFixture):
    with_timestamp = False       # only POST_DATE -> no hour of day

    def test_best_time_says_so_without_citing_reels(self):
        out = self.ask("What is the best time to post?")
        low = out.lower()
        self.assertIn("can't", low)
        self.assertIn("time", low)
        # No Sources block, and no reel URL anywhere in the answer (§13/§14).
        self.assertNotIn("Sources:", out)
        self.assertNotIn("instagram.com", out)
        self.assertNotIn("[S1]", out)

    def test_names_what_would_make_it_answerable(self):
        out = self.ask("What is the best time to post?")
        self.assertIn("POST_TIMESTAMP", out)


class TestStrategistDropsOrphanSources(unittest.TestCase):
    """The direct fix for failure B at the LLM-composition layer."""

    ABSTAIN = ("My read: we can't call a best posting time yet — we don't track "
               "posting time in a way that supports that comparison.\n\n"
               "Next action: start logging post timestamps.")
    CLAIM = ("My read: our strongest reels skew short — the median across 7 Great "
             "reels is 11s.\n\nNext action: keep cutting to under 15s.")
    SOURCES = {
        "S1": "[sheet_row] row 12 — Great — https://www.instagram.com/storellisoccer/reel/BM012/",
        "S2": "[sheet_row] row 3 — Great — https://www.instagram.com/storellisoccer/reel/BM003/",
        "S3": "[sheet_row] row 5 — Great — https://www.instagram.com/storellisoccer/reel/BM005/",
    }

    def _compose(self, model_answer):
        import config
        import gemini_client
        import social_strategist as ss

        class Fake:
            def summarize_findings(self, prompt):
                return model_answer

        real_gem, real_key, real_mode = (gemini_client.GeminiClient,
                                         config.GEMINI_API_KEY,
                                         config.SLACK_STRATEGIST_MODE_ENABLED)
        gemini_client.GeminiClient = Fake
        config.GEMINI_API_KEY = "fake"
        config.SLACK_STRATEGIST_MODE_ENABLED = True
        try:
            return ss.compose_strategic_answer(
                "What is the best time to post?", [],
                {"evidence": "some retrieved evidence text", "sources": self.SOURCES})
        finally:
            gemini_client.GeminiClient = real_gem
            config.GEMINI_API_KEY = real_key
            config.SLACK_STRATEGIST_MODE_ENABLED = real_mode

    def test_abstention_gets_no_sources_block(self):
        out = self._compose(self.ABSTAIN)
        self.assertIsNotNone(out)
        self.assertNotIn("Sources:", out)
        self.assertNotIn("instagram.com", out)

    def test_real_data_claim_still_gets_its_sources(self):
        """The fix must not strip citations from answers that DO claim numbers."""
        out = self._compose(self.CLAIM)
        self.assertIn("Sources:", out)
        self.assertIn("instagram.com", out)


# ---------------------------------------------------------------------------
# TEST C — comments are a real metric dimension
# ---------------------------------------------------------------------------
class TestCommentAnalytics(_AnalyticsFixture):
    def test_most_comments_uses_the_comment_metric(self):
        out = self.ask("What gets the most comments?")
        self.assertIn("240", out)                 # the real maximum
        self.assertIn("comments", out.lower())

    def test_individual_reels_are_labelled_as_examples_not_proof(self):
        """§16 — a single reel illustrates; it never establishes the aggregate."""
        out = self.ask("which reels have the most comments?")
        self.assertIn("example, not the aggregate", out)
        self.assertRegex(out, r"\[S1\] \d+ internal reels with a COMMENTS value")

    def test_absent_comment_column_is_never_invented(self):
        rows = [{**r, "COMMENTS": ""} for r in self.rows]
        sa._internal_sheet = lambda: (rows, [c for c in _META if c != "COMMENTS"], "")
        out = self.ask("What gets the most comments?")
        low = out.lower()
        self.assertIn("can't", low)
        self.assertNotRegex(out, r"\b\d{2,}\s*comments\b")


# ---------------------------------------------------------------------------
# TEST D — explicit scope is honoured
# ---------------------------------------------------------------------------
class TestScopedAnalytics(_AnalyticsFixture):
    def test_within_bodyshield_scopes_the_cohort(self):
        aq = AQ.parse("Within BodyShield, how long are our best reels?")
        self.assertEqual(aq["scope_source"], "explicit")
        self.assertIn("bodyshield", [p.lower() for p in aq["filters"]["product"]])
        out = self.ask("Within BodyShield, how long are our best reels?")
        self.assertIn("bodyshield", out.lower())
        # 4 BodyShield Great reels (9, 11, 14, 8 seconds) -> median 10s
        self.assertIn("10s", out)
        self.assertRegex(out, r"\b4 reels\b")

    def test_global_question_is_not_silently_scoped_by_the_frame(self):
        frame = {"scope": {"product": ["GK Gloves"], "icp": [], "format": [],
                           "hook": [], "concept_ids": []}, "topic": "gloves"}
        aq = AQ.parse("how long are our highest-performing reels?", frame=frame)
        self.assertEqual(aq["scope_source"], "global")
        self.assertFalse(aq["filters"].get("product"))

    def test_explicit_backreference_may_inherit_frame_scope(self):
        frame = {"scope": {"product": ["BodyShield Leggings"], "icp": [],
                           "format": [], "hook": [], "concept_ids": []},
                 "topic": "bodyshield"}
        aq = AQ.parse("within the BodyShield stuff we just discussed, how long "
                      "are the best reels?", frame=frame)
        self.assertIn(aq["scope_source"], ("explicit", "inherited"))
        self.assertTrue(aq["filters"].get("product"))


# ---------------------------------------------------------------------------
# TEST E — a recommendation informed by analytics is NOT an analytics lookup
# ---------------------------------------------------------------------------
class TestRecommendationStaysWithTheFrame(unittest.TestCase):
    def test_how_long_should_this_be_is_not_an_analytics_query(self):
        for q in ("How long should the BodyShield concept we just discussed be?",
                  "how long should this one be?",
                  "how many seconds should it be?"):
            self.assertIsNone(AQ.parse(q), q)

    def test_the_factual_twin_is_an_analytics_query(self):
        self.assertIsNotNone(AQ.parse("how long are our best BodyShield reels?"))

    def test_it_is_recognized_as_an_analytics_informed_recommendation(self):
        reco = AQ.parse_recommendation(
            "How long should the BodyShield concept we just discussed be?")
        self.assertIsNotNone(reco)
        self.assertEqual(reco["question_type"], "recommendation")
        self.assertEqual(reco["metric"], AQ.M_DURATION)
        self.assertEqual(reco["scope_source"], "explicit")

    def test_scope_comes_from_the_frame_when_not_named(self):
        """The frame resolves 'this one' — that is what it is for (§22)."""
        frame = {"scope": {"product": ["BodyShield Leggings"], "icp": [],
                           "format": [], "hook": [], "concept_ids": []},
                 "topic": "bodyshield", "prior_recommendation": "Dive Without The Sting"}
        reco = AQ.parse_recommendation("how long should this one be?", frame=frame)
        self.assertEqual(reco["scope_source"], "inherited")
        self.assertEqual(reco["filters"]["product"], ["BodyShield Leggings"])
        self.assertEqual(reco["referent"], "Dive Without The Sting")

    def test_a_factual_duration_question_is_never_a_recommendation(self):
        self.assertIsNone(AQ.parse_recommendation(
            "how many seconds long are our highest performing reels?"))


class TestDurationInformedRecommendation(_AnalyticsFixture):
    Q = "and how long should the BodyShield concept we just discussed be?"

    def test_commits_to_a_range_built_on_the_scoped_cohort(self):
        reco = AQ.parse_recommendation(self.Q)
        out = sa.answer_duration_recommendation(reco, self.Q, [])
        self.assertTrue(out)
        # a target range, not a bare statistic
        self.assertRegex(out, r"\b\d+–\d+ seconds\b")
        # grounded in the BodyShield Great cohort (median 10s across 4)
        self.assertIn("BodyShield", out)
        self.assertIn("10s", out)
        self.assertRegex(out, r"\b4\b")

    def test_is_not_answered_with_an_idea_list(self):
        import social_brain
        ctx = [{"role": "user", "text": "what should we shoot for BodyShield?"},
               {"role": "assistant",
                "text": "I'd shoot *Dive Without The Sting* for *BodyShield "
                        "Leggings* with the *Adult Amateur* audience. [S1]"}]
        out = social_brain.answer_conversation(
            self.Q, ctx, channel_context={"thread_ts": "t-e", "channel": "C",
                                          "user": "U"})
        low = out.lower()
        self.assertIn("seconds", low)
        self.assertNotRegex(out, r"(?m)^\*?\s*2\.\s")   # not a ranked idea list
        self.assertNotIn("score 9", low)

    def test_is_not_answered_with_a_global_median(self):
        """The whole point of the distinction: scope must survive."""
        reco = AQ.parse_recommendation(self.Q)
        out = sa.answer_duration_recommendation(reco, self.Q, [])
        # global Great median is 11s across 7; the BodyShield answer must not be it
        self.assertNotIn("across 7", out)

    def test_thin_cohort_is_flagged_rather_than_asserted(self):
        rows = [r for r in self.rows if r["Product"] == "Sliders"]
        sa._internal_sheet = lambda: (rows, list(_META), "")
        reco = AQ.parse_recommendation("how long should the Sliders concept be?")
        out = sa.answer_duration_recommendation(reco, "", [])
        self.assertIn("Caveat", out)

    def test_no_measured_duration_declines_to_put_a_number_on_it(self):
        rows = _rows(with_duration=False)
        sa._internal_sheet = lambda: (rows, [c for c in _META
                                             if c != "DURATION_SECONDS"], "")
        reco = AQ.parse_recommendation(self.Q)
        out = sa.answer_duration_recommendation(reco, "", [])
        self.assertNotRegex(out, r"\b\d+–\d+ seconds\b")
        self.assertIn("DURATION_SECONDS", out)


# ---------------------------------------------------------------------------
# duration: exact / bucket / absent, never conflated (§5/§6)
# ---------------------------------------------------------------------------
class TestDurationSourceHierarchy(_AnalyticsFixture):
    def test_exact_seconds_reported_with_coverage(self):
        aq = AQ.parse("how many seconds long are our highest performing reels?")
        prof = sa.duration_profile(aq)
        self.assertEqual(prof["source"], "exact")
        self.assertEqual(prof["stats"]["n"], 7)
        self.assertEqual(prof["stats"]["median"], 11)
        self.assertEqual(prof["coverage_pct"], 100)
        self.assertEqual(prof["comparison"]["n"], 11)

    def test_percentage_under_a_threshold_is_computed_not_guessed(self):
        aq = AQ.parse("what percentage of Great reels are under 10 seconds?")
        prof = sa.duration_profile(aq)
        self.assertEqual(prof["threshold"], 10.0)
        self.assertEqual(prof["pct_under"], 29)     # 2 of 7 (9s, 8s)

    def test_bucket_fallback_is_labelled_approximate(self):
        rows = _rows(with_duration=False)
        cols = [c for c in _META if c != "DURATION_SECONDS"]
        sa._internal_sheet = lambda: (rows, cols, "")
        great_links = [r["LINK"] for r in rows if r["PERFORMANCE"] == "Great"]
        sa.content_audit_duration_buckets = lambda links=None: {
            lk: "< 10 sec" for lk in great_links[:4]}
        out = self.ask("how many seconds long are our highest performing reels?")
        low = out.lower()
        self.assertIn("approximate", low)
        self.assertIn("< 10 sec", out)
        # A bucket is NEVER presented as an exact figure. (Saying "approximate
        # rather than a measured median" is the point, so only a median WITH A
        # NUMBER is a failure.)
        self.assertNotRegex(low, r"median (?:is )?\d")
        self.assertNotRegex(low, r"\b\d+(?:\.\d+)?s\b")
        self.assertIn("duration_seconds", low)

    def test_absent_duration_never_estimates_seconds(self):
        rows = _rows(with_duration=False)
        cols = [c for c in _META if c != "DURATION_SECONDS"]
        sa._internal_sheet = lambda: (rows, cols, "")
        out = self.ask("how many seconds long are our highest performing reels?")
        self.assertNotRegex(out.lower(), r"median\s*\d")
        self.assertIn("no duration", out.lower())


# ---------------------------------------------------------------------------
# temporal dimensions (§7/§8)
# ---------------------------------------------------------------------------
class TestTemporalDimensions(_AnalyticsFixture):
    def test_temporal_audit_reports_real_coverage(self):
        tf = sa.temporal_fields()
        self.assertEqual(tf["timestamp_column"], "POST_TIMESTAMP")
        self.assertEqual(tf["with_date"], 18)
        self.assertEqual(tf["with_time"], 18)
        self.assertTrue(tf["hour_derivable"])
        self.assertTrue(tf["day_of_week_derivable"])

    def test_hours_are_utc_and_said_to_be(self):
        out = self.ask("What time are our strongest reels posted?")
        self.assertIn("UTC", out)

    def test_day_of_week_is_derived_from_the_timestamp(self):
        aq = AQ.parse("what day do our best reels get posted?")
        prof = sa.posting_time_profile(aq)
        self.assertEqual(prof["dimension"], "day")
        self.assertEqual(prof["best_window"], "Monday")
        self.assertTrue(prof["sufficient"])

    def test_posting_time_never_claims_causality(self):
        out = self.ask("What is the best time to post?")
        low = out.lower()
        for causal in ("causes", "leads to", "results in", "because of the time"):
            self.assertNotIn(causal, low)

    def test_post_age_uses_the_maturity_window(self):
        prof = sa.post_age_profile(AQ.parse("How old is the latest reel?"))
        self.assertEqual(prof["n"], 18)
        self.assertIsNotNone(prof["newest_days"])


class TestThinTimeWindows(_AnalyticsFixture):
    """Sparse per-window samples must refuse to name a best time (§8)."""

    def setUp(self):
        super().setUp()
        # One reel per distinct hour -> no window clears the minimum.
        rows = []
        for i, r in enumerate(_rows()):
            ts = f"2025-04-{i + 1:02d}T{(i % 24):02d}:15:00+00:00"
            rows.append({**r, "POST_TIMESTAMP": ts, "POST_DATE": ts[:10]})
        sa._internal_sheet = lambda: (rows, list(_META), "")

    def test_refuses_to_name_a_best_hour(self):
        aq = AQ.parse("What is the best time to post?")
        prof = sa.posting_time_profile(aq)
        self.assertFalse(prof["sufficient"])
        out = self.ask("What is the best time to post?")
        low = out.lower()
        self.assertIn("not enough", low)
        self.assertNotIn("Sources:", out)
        self.assertNotIn("instagram.com", out)

    def test_says_the_timestamps_exist_even_when_the_sample_does_not(self):
        out = self.ask("Do we have enough data to know the best posting time?")
        self.assertIn("No", out)
        self.assertIn("timestamps exist", out.lower())


# ---------------------------------------------------------------------------
# availability ladder (§10)
# ---------------------------------------------------------------------------
class TestAvailabilityLadder(_AnalyticsFixture):
    def test_four_states_are_distinguished(self):
        rows, cols = self.rows, list(_META)
        self.assertEqual(sa.availability("DURATION_SECONDS", rows, cols)["state"],
                         sa.ENOUGH_DATA)
        # column absent entirely
        self.assertEqual(
            sa.availability("SAVES", rows, cols)["state"], sa.COLUMN_MISSING)
        # column present but every cell empty
        empty = [{**r, "COMMENTS": ""} for r in rows]
        self.assertEqual(sa.availability("COMMENTS", empty, cols)["state"],
                         sa.COLUMN_EXISTS)
        # present, populated, but below the pattern threshold
        thin = [{**r, "COMMENTS": ""} for r in rows]
        thin[0]["COMMENTS"] = "12"
        self.assertEqual(sa.availability("COMMENTS", thin, cols)["state"],
                         sa.DATA_EXISTS)

    def test_comparison_needs_both_sides(self):
        prof = sa.performance_slice_profile(
            AQ.parse("what performs better: Demo or Tutorial?"))
        self.assertTrue(prof["comparable"])
        self.assertEqual(prof["sides"][0]["label"], "demo")


# ---------------------------------------------------------------------------
# trial vs standard stays honest (§9)
# ---------------------------------------------------------------------------
class TestTrialVsStandard(_AnalyticsFixture):
    def test_never_inferred_from_public_data(self):
        out = self.ask("trial vs standard reels?")
        low = out.lower()
        self.assertIn("can't", low)
        self.assertIn("trial", low)

    def test_classify_reel_type_returns_unknown_without_a_field(self):
        for r in self.rows:
            self.assertEqual(sa.classify_reel_type(r), "unknown")


# ---------------------------------------------------------------------------
# claim -> evidence binding primitives (§11/§12/§15)
# ---------------------------------------------------------------------------
class TestSourceBinding(unittest.TestCase):
    def test_orphan_sources_are_detected(self):
        led = SB.ClaimLedger()
        led.add("median duration is 11s", ["S1"], SB.AGGREGATE_EVIDENCE)
        self.assertEqual(led.orphans(["S1", "S2", "S3"]), {"S2", "S3"})

    def test_example_alone_cannot_support_a_claim(self):
        led = SB.ClaimLedger()
        led.add("this reel did well", ["S9"], SB.EXAMPLE_CONTENT)
        self.assertEqual(led.supporting_ids(), set())

    def test_example_rides_along_with_an_aggregate(self):
        led = SB.ClaimLedger()
        led.add("Demo has a higher Great rate", ["S1"], SB.AGGREGATE_EVIDENCE)
        led.add("for example", ["S2"], SB.EXAMPLE_CONTENT)
        self.assertEqual(led.supporting_ids(), {"S1", "S2"})

    def test_missing_data_answer_recognized(self):
        for t in ("I can't split trial vs standard yet.",
                  "We don't track posting time.",
                  "Not enough to call yet.",
                  "There's no duration column in the sheet."):
            self.assertTrue(SB.is_missing_data_answer(t), t)
        self.assertFalse(SB.is_missing_data_answer(
            "The median is 11s across 7 Great reels."))

    def test_data_claim_recognized(self):
        self.assertTrue(SB.makes_data_claim("median is 11s across 7 reels"))
        self.assertTrue(SB.makes_data_claim("61% are under 15s"))
        self.assertFalse(SB.makes_data_claim("I'd shoot the turf-burn concept."))

    def test_inline_citation_wins_over_the_pack(self):
        v = SB.relevant_source_ids("The median is 11s [S1].", ["S1", "S2", "S3"])
        self.assertEqual(v["keep"], {"S1"})
        self.assertEqual(v["dropped"], {"S2", "S3"})

    def test_empty_block_is_never_rendered(self):
        self.assertEqual(SB.render_sources([]), "")
        self.assertEqual(SB.render_sources([("S1", "")]), "")


class TestNoForcedSources(_AnalyticsFixture):
    def test_every_analytics_answer_either_binds_its_sources_or_shows_none(self):
        for q in ("how many seconds long are our highest performing reels?",
                  "What gets the most comments?",
                  "What is the best time to post?",
                  "how many BodyShield reels do we have?",
                  "trial vs standard reels?",
                  "How old is the latest reel?"):
            out = self.ask(q)
            audit = sa.LAST_SOURCE_AUDIT
            if "Sources:" in out:
                self.assertGreater(audit.get("after", 0), 0, q)
            # a rendered source id must appear in the block, never dangling
            for sid in SB.cited_ids(out):
                self.assertIn(f"[{sid}]", out, q)


class TestEmptyScopeIsNotASchemaProblem(_AnalyticsFixture):
    """A filter that matches nothing must not be reported as a missing column."""

    def test_duration_reports_the_scope_not_the_schema(self):
        aq = AQ.parse("how long are our best CoolCore reels?")
        self.assertIsNotNone(aq)
        out = sa.answer_analytics_query(aq, "", [])
        low = out.lower()
        self.assertNotIn("no duration column", low)
        self.assertIn("matching that", low)

    def test_posting_time_reports_the_scope_not_the_schema(self):
        aq = AQ.parse("what day do our best CoolCore reels get posted?")
        prof = sa.posting_time_profile(aq)
        self.assertEqual(prof["placed"], 0)
        out = sa.answer_analytics_query(aq, "", [])
        self.assertIn("matching that", out.lower())


class TestCoverageAudit(_AnalyticsFixture):
    """`audit-analytics-coverage` — the honest inventory behind every answer."""

    def test_reports_real_coverage_per_dimension(self):
        c = sa.analytics_coverage()
        self.assertTrue(c["ok"])
        self.assertEqual(c["internal_rows"], 18)
        self.assertEqual(c["great_rows"], 7)
        self.assertEqual(c["duration"]["great_exact_coverage"], 7)
        self.assertTrue(c["duration"]["supports_exact_analysis"])
        self.assertEqual(c["temporal"]["with_time"], 18)
        self.assertFalse(c["reel_type"]["supports_trial_split"])

    def test_reports_unsupported_dimensions_as_unsupported(self):
        rows = _rows(with_duration=False, with_timestamp=False)
        cols = [c for c in _META if c not in ("DURATION_SECONDS", "POST_TIMESTAMP")]
        sa._internal_sheet = lambda: (rows, cols, "")
        c = sa.analytics_coverage()
        self.assertIsNone(c["duration"]["column"])
        self.assertFalse(c["duration"]["supports_exact_analysis"])
        self.assertIsNone(c["temporal"]["timestamp_column"])
        self.assertFalse(c["temporal"]["hour_derivable"])
        self.assertFalse(c["temporal"]["supports_hour_analysis"])

    def test_render_never_crashes_on_an_unreachable_sheet(self):
        sa._internal_sheet = lambda: ([], [], "RuntimeError: no sheet")
        out = sa.render_analytics_coverage(sa.analytics_coverage())
        self.assertIn("Nothing written", out)


if __name__ == "__main__":
    unittest.main()
