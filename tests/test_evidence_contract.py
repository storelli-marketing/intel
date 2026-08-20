"""Unit tests for the Evidence & Answer Contract primitives (Phases 5–17).

These test the epistemic machinery directly — source authority, claim strength,
sufficiency, the specificity ladder, the metric contract, freshness, contradiction
handling, abstention, and claim validation — independently of any answer path.

Run: python -m unittest tests.test_evidence_contract
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import claim_validator as CV
import config
import evidence_contract as EC
import metric_registry as MR
import pattern_history as PH


# --------------------------------------------------------------------------- #
# Phase 5 — source authority
# --------------------------------------------------------------------------- #
class TestSourceAuthority(unittest.TestCase):
    def test_internal_classes_are_proof_external_is_not(self):
        for c in (EC.INTERNAL_STORELLI_METRIC, EC.INTERNAL_STORELLI_CONTENT,
                  EC.INTERNAL_DERIVED_PATTERN, EC.INTERNAL_WINNING_PROFILE):
            self.assertTrue(EC.is_internal_proof(c))
        for c in (EC.EXTERNAL_INSPIRATION, EC.SEMANTIC_CONNECTION, EC.STRATEGIC_INFERENCE):
            self.assertFalse(EC.is_internal_proof(c))

    def test_internal_stats_reject_external_rows_structurally(self):
        rows = [{"source_class": EC.INTERNAL_STORELLI_CONTENT},
                {"source_class": EC.EXTERNAL_INSPIRATION}]
        with self.assertRaises(EC.ExternalAsProofError):
            EC.assert_internal_only(rows, where="winning profile")
        # internal-only passes
        EC.assert_internal_only([rows[0]], where="correlation")

    def test_external_allowed_and_forbidden_uses(self):
        for use in ("execution_inspiration", "storytelling_reference", "visual_reference",
                    "hypothesis_generation"):
            self.assertTrue(EC.external_use_allowed(use))
        for use in EC.EXTERNAL_FORBIDDEN_USES:
            self.assertFalse(EC.external_use_allowed(use))


# --------------------------------------------------------------------------- #
# Phases 6/7 — claim strength + sufficiency
# --------------------------------------------------------------------------- #
class TestSufficiency(unittest.TestCase):
    _INTERNAL = {EC.INTERNAL_DERIVED_PATTERN}

    def test_thin_sample_never_becomes_proven(self):
        s = EC.evaluate_sufficiency(sample_size=2, positive_examples=2, comparison_examples=10,
                                    effect_signal=0.6, source_classes=self._INTERNAL)
        self.assertEqual(s["claim_strength"], EC.DIRECTIONAL)
        self.assertTrue(any("small sample" in l for l in s["limitations"]))

    def test_n_equals_one_is_not_a_claim(self):
        s = EC.evaluate_sufficiency(sample_size=1, positive_examples=1, comparison_examples=8,
                                    effect_signal=0.9, source_classes=self._INTERNAL)
        self.assertEqual(s["claim_strength"], EC.UNKNOWN)

    def test_strong_repeatable_evidence_is_proven(self):
        s = EC.evaluate_sufficiency(sample_size=10, positive_examples=7, comparison_examples=8,
                                    effect_signal=0.4, source_classes=self._INTERNAL)
        self.assertEqual(s["claim_strength"], EC.PROVEN)

    def test_no_comparison_caps_at_directional(self):
        s = EC.evaluate_sufficiency(sample_size=10, positive_examples=7, comparison_examples=0,
                                    effect_signal=0.4, source_classes=self._INTERNAL)
        self.assertEqual(s["claim_strength"], EC.DIRECTIONAL)

    def test_flat_effect_cannot_be_proven(self):
        s = EC.evaluate_sufficiency(sample_size=12, positive_examples=8, comparison_examples=6,
                                    effect_signal=0.01, source_classes=self._INTERNAL)
        self.assertEqual(s["claim_strength"], EC.DIRECTIONAL)

    def test_outlier_concentration_caps_strength(self):
        s = EC.evaluate_sufficiency(sample_size=10, positive_examples=6, comparison_examples=6,
                                    effect_signal=0.5, source_classes=self._INTERNAL,
                                    outlier_share=0.8)
        self.assertEqual(s["claim_strength"], EC.DIRECTIONAL)
        self.assertTrue(any("outlier" in l for l in s["limitations"]))

    def test_external_only_is_inference_never_proof(self):
        s = EC.evaluate_sufficiency(sample_size=25, positive_examples=20, comparison_examples=20,
                                    effect_signal=0.9,
                                    source_classes={EC.EXTERNAL_INSPIRATION})
        self.assertEqual(s["claim_strength"], EC.INFERRED)
        self.assertTrue(any("no internal" in l for l in s["limitations"]))

    def test_unavailable_metric_is_unknown(self):
        s = EC.evaluate_sufficiency(sample_size=20, positive_examples=15,
                                    source_classes=self._INTERNAL, metric_available=False)
        self.assertEqual(s["claim_strength"], EC.UNKNOWN)

    def test_contradictions_cap_and_surface(self):
        s = EC.evaluate_sufficiency(sample_size=12, positive_examples=8, comparison_examples=8,
                                    effect_signal=0.5, source_classes=self._INTERNAL,
                                    contradictions=1)
        self.assertEqual(s["claim_strength"], EC.SUPPORTED)
        self.assertTrue(any("other way" in l for l in s["limitations"]))

    def test_staleness_reduces_strength(self):
        stale = config.INTELLIGENCE_REFRESH_CADENCE_DAYS + \
            config.INTELLIGENCE_STALE_TOLERANCE_DAYS + 5
        s = EC.evaluate_sufficiency(sample_size=12, positive_examples=8, comparison_examples=8,
                                    effect_signal=0.5, source_classes=self._INTERNAL,
                                    freshness_days=stale)
        self.assertNotEqual(s["claim_strength"], EC.PROVEN)
        self.assertTrue(any("days old" in l for l in s["limitations"]))


# --------------------------------------------------------------------------- #
# Phase 8 — specificity ladder
# --------------------------------------------------------------------------- #
class TestSpecificity(unittest.TestCase):
    def test_exact_slice_used_when_sufficient(self):
        r = EC.resolve_scope({EC.SCOPE_EXACT: 6, EC.SCOPE_ICP: 20, EC.SCOPE_BROAD: 40})
        self.assertEqual(r["scope"], EC.SCOPE_EXACT)
        self.assertFalse(r["relaxed"])
        self.assertEqual(r["disclosure"], "")

    def test_relaxes_and_discloses(self):
        r = EC.resolve_scope({EC.SCOPE_EXACT: 1, EC.SCOPE_ICP: 9, EC.SCOPE_BROAD: 40})
        self.assertEqual(r["scope"], EC.SCOPE_ICP)
        self.assertTrue(r["relaxed"])
        self.assertIn("1 example", r["disclosure"])

    def test_no_usable_scope(self):
        r = EC.resolve_scope({EC.SCOPE_EXACT: 0, EC.SCOPE_BROAD: 1})
        self.assertIsNone(r["scope"])
        self.assertIn("don't have enough", r["disclosure"])


# --------------------------------------------------------------------------- #
# Phase 9/10 — metric contract
# --------------------------------------------------------------------------- #
class TestMetricContract(unittest.TestCase):
    def test_private_metrics_unavailable_without_meta(self):
        saved = config.INSTAGRAM_ACCESS_TOKEN, config.INSTAGRAM_BUSINESS_ACCOUNT_ID
        config.INSTAGRAM_ACCESS_TOKEN = config.INSTAGRAM_BUSINESS_ACCOUNT_ID = ""
        try:
            for m in ("SAVES", "REACH", "IMPRESSIONS", "AGE_SPLIT",
                      "FOLLOWER_NONFOLLOWER_SPLIT"):
                self.assertFalse(MR.is_available(m), m)
                self.assertIn("private", MR.metric_gap_note(m).lower())
        finally:
            config.INSTAGRAM_ACCESS_TOKEN, config.INSTAGRAM_BUSINESS_ACCOUNT_ID = saved

    def test_public_metrics_need_the_column(self):
        self.assertTrue(MR.is_available("VIEWS", columns=["VIEWS", "LIKES"]))
        self.assertFalse(MR.is_available("VIEWS", columns=["LIKES"]))

    def test_present_but_empty_column_is_not_data(self):
        self.assertFalse(MR.is_available("VIEWS", columns=["VIEWS"], populated={"VIEWS": 0}))
        self.assertTrue(MR.is_available("VIEWS", columns=["VIEWS"], populated={"VIEWS": 12}))

    def test_registry_declares_comparability_limits(self):
        er = MR.describe("ENGAGEMENT_RATE", columns=["ENGAGEMENT_RATE"])
        self.assertIn("not comparable", er["limitations"])
        self.assertEqual(MR.resolve("duration").name, "DURATION_SECONDS")
        self.assertEqual(MR.REGISTRY["POST_DATE"].mutability, "immutable")
        self.assertEqual(MR.REGISTRY["VIEWS"].mutability, "mutable")

    def test_follower_denominator_is_documented(self):
        self.assertIn("follower", MR.REGISTRY["PERFORMANCE"].denominator)
        self.assertIn("snapshot", MR.REGISTRY["FOLLOWERS_AT_MEASUREMENT"].limitations)


# --------------------------------------------------------------------------- #
# Phase 14/15/16 — contradictions, abstention, answer contract
# --------------------------------------------------------------------------- #
class TestContradictionsAbstention(unittest.TestCase):
    def test_detects_segment_disagreement(self):
        overall = [{"label": "Curiosity Gap", "lift": 0.4}, {"label": "Education", "lift": 0.1}]
        segment = [{"label": "Education", "lift": 0.5}, {"label": "Curiosity Gap", "lift": -0.2}]
        c = EC.find_contradictions(overall, segment)
        self.assertTrue(c)
        self.assertTrue(any("Education" in x["note"] for x in c))

    def test_no_contradiction_when_aligned(self):
        same = [{"label": "Curiosity Gap", "lift": 0.4}]
        self.assertEqual(EC.find_contradictions(same, same), [])

    def test_abstention_proposes_a_test(self):
        a = EC.abstention("Parents", missing="only 2 Parents examples")
        self.assertEqual(a["claim_strength"], EC.UNKNOWN)
        self.assertTrue(a["proposed_test"])
        self.assertIn("Parents", a["gap"])

    def test_answer_contract_shape(self):
        suff = EC.evaluate_sufficiency(sample_size=6, positive_examples=4, comparison_examples=5,
                                       effect_signal=0.3,
                                       source_classes={EC.INTERNAL_WINNING_PROFILE})
        ans = EC.build_answer("Shoot the turf-burn cut.", facts=["6 internal examples"],
                              inferences=["should lift saves"], recommendation="Shoot it",
                              sufficiency=suff, could_change_mind="two flat results")
        for key in ("direct_answer", "facts", "inferences", "recommendation",
                    "limitations", "claim_strength", "confidence", "evidence_refs",
                    "could_change_mind"):
            self.assertIn(key, ans)
        self.assertEqual(ans["claim_strength"], EC.SUPPORTED)


# --------------------------------------------------------------------------- #
# Phase 17 — claim validation
# --------------------------------------------------------------------------- #
class TestClaimValidation(unittest.TestCase):
    def test_honest_disclaimer_is_never_rewritten(self):
        d = "_External inspiration is reference only — not proof it works for Storelli._"
        r = CV.validate_response(d)
        self.assertFalse(r.rewritten)
        self.assertEqual(r.text, d)

    def test_affirmative_external_as_proof_is_neutralised(self):
        r = CV.validate_response("The external video proves this works for us.")
        self.assertTrue(r.rewritten)
        self.assertIn("execution inspiration only", r.text)
        self.assertNotIn("proves this works", r.text)

    def test_causal_language_softened(self):
        r = CV.validate_response("Curiosity hooks cause more saves.")
        self.assertIn("associated with", r.text)
        self.assertNotIn("cause more", r.text)

    def test_negated_hedge_survives(self):
        for t in ("It's a bet on a strong format, not a guarantee.",
                  "External inspiration never proves performance for Storelli."):
            self.assertFalse(CV.validate_response(t).rewritten, t)

    def test_fabricated_private_metric_number_removed(self):
        r = CV.validate_response("This got 1,200 saves and 40k reach.",
                                 available_metrics=["VIEWS", "LIKES"])
        self.assertTrue(r.issues)
        self.assertNotIn("1,200 saves", r.text)

    def test_proxy_labelled_mention_is_allowed(self):
        t = "KPI bet: saves proxy (not tracked yet)."
        r = CV.validate_response(t, available_metrics=["VIEWS"])
        self.assertIn("saves proxy", r.text)

    def test_thin_sample_disclosure_added(self):
        ans = {"claim_strength": EC.DIRECTIONAL, "confidence": 0.45, "sample_size": 3}
        r = CV.validate("Curiosity hooks are the way to go here.", answer=ans,
                        available_metrics=["VIEWS"])
        self.assertIn("n=3", r.text)

    def test_relaxed_scope_disclosure_added(self):
        scope = EC.resolve_scope({EC.SCOPE_EXACT: 1, EC.SCOPE_ICP: 9, EC.SCOPE_BROAD: 20})
        r = CV.validate("Education hooks win for this audience.",
                        answer={"claim_strength": EC.SUPPORTED, "confidence": 0.7,
                                "sample_size": 9},
                        available_metrics=["VIEWS"], scope=scope)
        self.assertIn("example", r.text.lower())

    def test_unknown_cannot_assert(self):
        r = CV.validate("Parents respond best to fear hooks.",
                        answer={"claim_strength": EC.UNKNOWN, "confidence": 0.0,
                                "sample_size": 1}, available_metrics=["VIEWS"])
        self.assertIn("don't have the data", r.text.lower())

    def test_line_structure_preserved(self):
        brief = "*Beats:*\n1. Open on the sting\n2. Protected replay\n3. CTA"
        r = CV.validate_response(brief, available_metrics=["VIEWS"])
        self.assertEqual(r.text.count("\n"), brief.count("\n"))

    def test_evidence_detail_only_when_asked(self):
        self.assertTrue(CV.wants_evidence_detail("show me the evidence"))
        self.assertTrue(CV.wants_evidence_detail("how confident are you?"))
        self.assertTrue(CV.wants_evidence_detail("what's the sample?"))
        self.assertFalse(CV.wants_evidence_detail("what should we shoot?"))

    def test_evidence_detail_render_is_human(self):
        suff = EC.evaluate_sufficiency(sample_size=6, positive_examples=4, comparison_examples=5,
                                       effect_signal=0.3,
                                       source_classes={EC.INTERNAL_DERIVED_PATTERN})
        out = CV.render_evidence_detail(EC.build_answer("x", sufficiency=suff))
        self.assertIn("6 internal example", out)
        self.assertNotIn("{", out)


# --------------------------------------------------------------------------- #
# Phase 13 — change history
# --------------------------------------------------------------------------- #
class TestPatternHistory(unittest.TestCase):
    def test_snapshot_from_correlations(self):
        rows = PH.snapshot_from_correlations(
            [{"layer": "hook", "label": "Curiosity Gap", "lift": 0.42,
              "confidence": "Medium", "videos_with_signal": 6}])
        self.assertEqual(rows[0]["pattern_id"], "hook::curiosity gap")
        self.assertEqual(rows[0]["sample_size"], 6)

    def test_diff_detects_strengthening(self):
        cur = [{"pattern_id": "hook::curiosity gap", "layer": "hook", "label": "Curiosity Gap",
                "strength": 0.5, "confidence": "Medium", "sample_size": 8}]
        prev = {"hook::curiosity gap": {"STRENGTH": "0.3", "SAMPLE_SIZE": "5",
                                        "CONFIDENCE": "Low"}}
        d = PH.diff(cur, prev)[0]
        self.assertEqual(d["direction"], PH.STRONGER)
        self.assertAlmostEqual(d["delta"], 0.2, places=3)
        self.assertIn("more supporting", d["reason"])

    def test_diff_detects_weakening_and_new(self):
        cur = [{"pattern_id": "hook::humor", "layer": "hook", "label": "Humor",
                "strength": -0.2, "confidence": "Low", "sample_size": 3}]
        weaker = PH.diff(cur, {"hook::humor": {"STRENGTH": "0.3", "SAMPLE_SIZE": "3"}})[0]
        self.assertEqual(weaker["direction"], PH.WEAKER)
        new = PH.diff(cur, {})[0]
        self.assertEqual(new["direction"], PH.NEW)

    def test_stable_when_unchanged(self):
        cur = [{"pattern_id": "hook::demo", "layer": "hook", "label": "Demo",
                "strength": 0.30, "confidence": "Medium", "sample_size": 6}]
        d = PH.diff(cur, {"hook::demo": {"STRENGTH": "0.30", "SAMPLE_SIZE": "6"}})[0]
        self.assertEqual(d["direction"], PH.STABLE)


if __name__ == "__main__":
    unittest.main()
