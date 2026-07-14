"""Tests for Milestone 4A — rated creative idea generation.

Proves ideas require internal profile evidence, external inspiration alone
cannot generate an idea, ineligible inspiration is ignored, scores are computed,
the self-critique gate rejects weak/unsafe ideas, [S#]/[E#] citations stay
separate, and nothing internal is used as proof.

Run: python -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import idea_generator as ig
from idea_generator import (build_ideas, compute_idea_scores, copyright_recheck,
                            eligible_inspiration, eligible_profiles,
                            self_critique_pass, top_refs_for_profile)
from inspiration_sheets import SOURCE_TYPE_EXTERNAL
import performance
import correlations as corr
from sheets_client import SheetsClient
import taxonomy

A_SIGNAL_COL = taxonomy.all_signal_columns()[0]


def _profile(pid="WFP-gloves-aspiring_pro", active="TRUE", conf="Medium", **over):
    p = {"PROFILE_ID": pid, "ACTIVE": active, "CONFIDENCE": conf,
         "PROFILE_NAME": "Gloves / Aspiring Pro: Education + Tutorial",
         "PRODUCT": "Gloves", "ICP": "Aspiring Pro",
         "HOOK_TAGS": "Education", "FORMAT_TAGS": "Tutorial", "VISUAL_STYLE_TAGS": "Action",
         "PROBLEM_TAGS": "Chronic Pain", "SOLUTION_TAGS": "Prevention",
         "FUNNEL_STAGE_TAGS": "Consideration", "INTERNAL_SAMPLE_SIZE": "5",
         "SUPPORTING_VIDEO_URLS": "https://ig/s1/;https://ig/s2/",
         "SUPPORTING_LEARNING_IDS": "signal_hook_education;signal_format_tutorial"}
    p.update(over)
    return p


def _insp(row=3, **over):
    r = {"_row": row, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL, "SAFETY_STATUS": "Safe",
         "ANALYSIS_STATUS": "Analyzed", "USE_FOR_IDEA_GEN": "TRUE",
         "INSPIRATION_QUALITY_SCORE": "88", "FAMOUS_PLAYER_RISK": "Low",
         "MATCH_FOOTAGE_RISK": "Low", "OFF_DOMAIN_RISK": "Low",
         "CREATIVE_MECHANISM": "Education | Tutorial | chronic pain -> prevention",
         "BEST_MATCHED_PROFILE_ID": "WFP-gloves-aspiring_pro",
         "POST_URL": "https://tiktok/e1", "SOURCE_ID": "tiktok:e1",
         "HOOK_TAGS": "Education", "FORMAT_TAGS": "Tutorial"}
    r.update(over)
    return r


_GOOD_IDEA = {
    "idea_title": "Keeper Confidence Drill: grip that holds under pressure",
    "hook": "The one glove mistake costing you clean sheets",
    "format": "Tutorial", "concept": "Show three grip checkpoints a keeper can self-test.",
    "storelli_adaptation": "Use Storelli Gloves to demo grip retention in wet conditions.",
    "shot_list": ["close-up grip", "wet-ball catch", "before/after confidence"],
    "cta": "Shop Storelli Gloves", "idea_rationale": "Maps to [S1][S2] internal winning tutorial pattern; adapts mechanism from [E1] without copying.",
    "self_critique": "Specific hook, shootable, clearly Storelli, external used as inspiration only.",
    "risk_notes": "No famous players or match footage.", "recommended_shoot_priority": "High",
    "verdict": "keep", "inspiration_fit": 85, "product_fit": 90, "icp_fit": 85,
    "execution_clarity": 88, "novelty": 70, "feasibility": 90, "strategic_priority": 88,
}


class FakeGemini:
    def __init__(self, ideas=None):
        self._payload = json.dumps({"ideas": ideas if ideas is not None else [_GOOD_IDEA]})
        self.calls = 0

    def summarize_findings(self, prompt):
        self.calls += 1
        return self._payload


class TestEligibility(unittest.TestCase):
    def test_inspiration_gate(self):
        self.assertTrue(eligible_inspiration(_insp()))
        self.assertFalse(eligible_inspiration(_insp(INSPIRATION_QUALITY_SCORE="70")))
        self.assertFalse(eligible_inspiration(_insp(USE_FOR_IDEA_GEN="FALSE")))
        self.assertFalse(eligible_inspiration(_insp(SAFETY_STATUS="Rejected")))
        self.assertFalse(eligible_inspiration(_insp(ANALYSIS_STATUS="Skipped")))
        self.assertFalse(eligible_inspiration(_insp(FAMOUS_PLAYER_RISK="High")))
        self.assertFalse(eligible_inspiration(_insp(MATCH_FOOTAGE_RISK="High")))
        self.assertFalse(eligible_inspiration(_insp(OFF_DOMAIN_RISK="High")))
        self.assertFalse(eligible_inspiration(_insp(CREATIVE_MECHANISM="")))

    def test_profile_gate(self):
        self.assertEqual(len(eligible_profiles([_profile()])), 1)
        self.assertEqual(eligible_profiles([_profile(active="FALSE")]), [])
        self.assertEqual(eligible_profiles([_profile(conf="Low")]), [])


class TestNoEvidenceNoIdea(unittest.TestCase):
    def test_no_active_profiles_no_ideas(self):
        # External inspiration alone cannot generate an idea.
        ideas = build_ideas([_profile(active="FALSE")], [_insp()], FakeGemini())
        self.assertEqual(ideas, [])

    def test_profile_without_eligible_refs_no_ideas(self):
        # Active profile but only low-quality inspiration -> no refs -> no idea.
        ideas = build_ideas([_profile()], [_insp(INSPIRATION_QUALITY_SCORE="50")], FakeGemini())
        self.assertEqual(ideas, [])

    def test_ineligible_rows_never_referenced(self):
        rows = [_insp(row=3, FAMOUS_PLAYER_RISK="High"),   # ignored
                _insp(row=4, SAFETY_STATUS="Rejected"),     # ignored
                _insp(row=5)]                               # the only eligible one
        refs = top_refs_for_profile(_profile(), rows, k=4)
        self.assertEqual([r["_row"] for r in refs], [5])


class TestScoring(unittest.TestCase):
    def test_scores_computed_and_formula(self):
        refs = [_insp()]
        scores = compute_idea_scores(_profile(conf="High"), refs, _GOOD_IDEA, copyright_ok=True)
        for k in ("IDEA_SCORE", "EVIDENCE_FIT_SCORE", "INSPIRATION_FIT_SCORE",
                  "PRODUCT_FIT_SCORE", "COPYRIGHT_SAFETY_SCORE", "STRATEGIC_PRIORITY_SCORE"):
            self.assertIn(k, scores)
        self.assertEqual(scores["COPYRIGHT_SAFETY_SCORE"], 100.0)
        # Evidence fit is anchored to the internal profile, not external views.
        self.assertGreaterEqual(scores["EVIDENCE_FIT_SCORE"], 90)

    def test_copyright_hit_tanks_safety(self):
        scores = compute_idea_scores(_profile(), [_insp()], _GOOD_IDEA, copyright_ok=False)
        self.assertLess(scores["COPYRIGHT_SAFETY_SCORE"], 30)


class TestSelfCritique(unittest.TestCase):
    def test_generic_hook_rejected(self):
        idea = dict(_GOOD_IDEA, hook="watch this")
        scores = compute_idea_scores(_profile(), [_insp()], idea, True)
        ok, why = self_critique_pass(idea, scores)
        self.assertFalse(ok)

    def test_copyright_idea_rejected(self):
        idea = dict(_GOOD_IDEA, concept="Recreate Messi's Champions League highlights")
        ok, why = self_critique_pass(idea, compute_idea_scores(_profile(), [_insp()], idea, False))
        self.assertFalse(ok)

    def test_model_drop_verdict_respected(self):
        idea = dict(_GOOD_IDEA, verdict="drop")
        ok, _ = self_critique_pass(idea, compute_idea_scores(_profile(), [_insp()], idea, True))
        self.assertFalse(ok)

    def test_good_idea_kept(self):
        ok, _ = self_critique_pass(_GOOD_IDEA, compute_idea_scores(_profile(), [_insp()], _GOOD_IDEA, True))
        self.assertTrue(ok)


class TestBuildIdeas(unittest.TestCase):
    def test_ideas_have_scores_and_separated_citations(self):
        ideas = build_ideas([_profile()], [_insp()], FakeGemini())
        self.assertTrue(ideas)
        idea = ideas[0]
        self.assertIn("IDEA_SCORE", idea)
        # Internal evidence + external references live in SEPARATE fields.
        self.assertIn("https://ig/s1/", idea["INTERNAL_EVIDENCE_URLS"])
        self.assertIn("https://tiktok/e1", idea["EXTERNAL_REFERENCE_URLS"])
        self.assertNotIn("tiktok", idea["INTERNAL_EVIDENCE_URLS"])   # no external in internal
        self.assertIn("[S1]", idea["IDEA_RATIONALE"])
        self.assertIn("[E1]", idea["IDEA_RATIONALE"])
        self.assertEqual(idea["SOURCE_PROFILE_ID"], "WFP-gloves-aspiring_pro")

    def test_weak_ideas_dropped(self):
        weak = [dict(_GOOD_IDEA, hook="wow", idea_title="x"),
                dict(_GOOD_IDEA, concept="full match highlights recreation")]
        ideas = build_ideas([_profile()], [_insp()], FakeGemini(ideas=weak))
        self.assertEqual(ideas, [])       # both fail self-critique


class TestIsolation(unittest.TestCase):
    def test_copyright_recheck(self):
        self.assertFalse(copyright_recheck("recreate Ronaldo free kick")[0])
        self.assertFalse(copyright_recheck("stab proof body armor demo")[0])
        self.assertFalse(copyright_recheck("use full match highlights from the broadcast")[0])
        self.assertTrue(copyright_recheck("goalkeeper grip tutorial in the rain")[0])
        # "highlights" as an ordinary verb must NOT be blocked in idea copy.
        self.assertTrue(copyright_recheck("this demo highlights the impact protection")[0])

    def test_external_reference_not_internal_evidence(self):
        # An idea's external URLs must never appear as internal evidence, so an
        # external row can never be counted as Storelli proof downstream.
        ideas = build_ideas([_profile()], [_insp()], FakeGemini())
        idea = ideas[0]
        ext = {"_row": 999, "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL,
               "PERFORMANCE": "Great", A_SIGNAL_COL: "1"}
        internal = {"_row": 1, "PERFORMANCE": "Great", A_SIGNAL_COL: "1"}
        buckets = performance.buckets_for_rows([internal, ext])
        self.assertIn(1, buckets)
        self.assertNotIn(999, buckets)
        analyzed = [r for r in [internal, ext]
                    if SheetsClient.is_analyzed(r) and r["_row"] in buckets]
        self.assertEqual(corr.compute(analyzed, buckets), corr.compute([internal], buckets))


if __name__ == "__main__":
    unittest.main()
