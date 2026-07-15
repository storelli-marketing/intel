"""Tests for the Slack Social Strategist Skill Pack.

Proves routing for the 8 skills, the hard-evidence vs external-reference vs
strategic-inference separation (comments answered as inference, never proof),
test-hypothesis win/lose learning, concept references returning videos (not the
idea list), idea diagnosis (weakness + fix), calendar doctor (revise/kill/move-up,
max 3), learning->action, content-gap (Parents gap), shot briefs (hook + 4-6
beats + CTA), follow-up context resolution, source-id + external-as-proof
validation, and read-only behavior (no Sheet/Notion writes).

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import social_strategy_skills as strat


def _profile(product="BodyShield GK Leggings", icp="Adult Amateur", conf="High"):
    return {"PROFILE_ID": "WFP-bs", "ACTIVE": "TRUE", "CONFIDENCE": conf,
            "PROFILE_NAME": f"{product} / {icp}: Curiosity Gap + Demo", "PRODUCT": product,
            "ICP": icp, "HOOK_TAGS": "Curiosity Gap, Fear / Risk", "FORMAT_TAGS": "Demo",
            "INTERNAL_SAMPLE_SIZE": "6", "PERFORMANCE_SIGNAL": "Great in 6/9 internal videos",
            "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/"}


CONN = {"CONNECTION_ID": "SC-1", "CONCEPT_NAME": "BodyShield turf-burn protection",
        "PRODUCT": "BodyShield GK Leggings", "HOOK_ARCHETYPE": "Curiosity Gap",
        "CONNECTION_SCORE": "89",
        "STORYTELLING_STRUCTURE": "Curiosity Gap → Demo → Pain Reveal → Product Protection Reveal → CTA",
        "WHAT_TO_STEAL": "the wince moment", "WHAT_NOT_TO_COPY": "their caption",
        "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
        "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1;"
                                   "https://www.tiktok.com/@groundglorygk/video/2",
        "EXTERNAL_CREATORS": "@jasmines_main, @groundglorygk"}

IDEAS = [{"IDEA_ID": "IDEA-bs-1", "IDEA_SCORE": "92", "PRODUCT": "BodyShield GK Leggings",
          "ICP": "Adult Amateur", "REFINED_IDEA_TITLE": "BodyShield: Dive Without The Sting",
          "REFINED_CONCEPT": "turf burn wince then protected replay", "ORIGINAL_WEAKNESS": "hook was generic"}]

CAL = [
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "Generic protection montage", "PRODUCT": "Gloves",
     "RECOMMENDATION": "Reject", "CALENDAR_IDEA_SCORE": "41", "NOTION_PAGE_URL": "https://notion.so/a"},
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "BodyShield turf-burn wince", "PRODUCT": "BodyShield GK Leggings",
     "RECOMMENDATION": "Keep", "CALENDAR_IDEA_SCORE": "84", "NOTION_PAGE_URL": "https://notion.so/b"},
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "Glove grip explainer", "PRODUCT": "Gloves",
     "RECOMMENDATION": "Revise", "CALENDAR_IDEA_SCORE": "58", "NOTION_PAGE_URL": "https://notion.so/c"},
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "Slider slide test", "PRODUCT": "Sliders",
     "RECOMMENDATION": "Keep", "CALENDAR_IDEA_SCORE": "68", "NOTION_PAGE_URL": "https://notion.so/d"},
]


class FakeSheets:
    def __init__(self, profiles=None, connections=None, ideas=None, calendar=None, adhoc=None):
        self._p = profiles if profiles is not None else [_profile()]
        self._c = connections if connections is not None else [dict(CONN)]
        self._i = ideas if ideas is not None else [dict(x) for x in IDEAS]
        self._cal = calendar if calendar is not None else [dict(x) for x in CAL]
        self._a = adhoc or []
        self.writes = 0

    def read_profiles(self):
        return [dict(x) for x in self._p]

    def read_semantic_connections(self):
        return [dict(x) for x in self._c]

    def read_ideas(self):
        return [dict(x) for x in self._i]

    def read_calendar_ratings(self):
        return [dict(x) for x in self._cal]

    def read_adhoc_evaluations(self):
        return [dict(x) for x in self._a]

    # any write method bumps the counter (must stay 0)
    def upsert_adhoc_evaluations(self, *a, **k):
        self.writes += 1

    def upsert_calendar_ratings(self, *a, **k):
        self.writes += 1

    def upsert_profiles(self, *a, **k):
        self.writes += 1

    def upsert_semantic_connections(self, *a, **k):
        self.writes += 1


CTX = [{"role": "user", "text": "give me BodyShield ideas"},
       {"role": "assistant", "text": "Shoot *BodyShield: Dive Without The Sting* — turf-burn wince."}]


def ans(q, ctx=None, sheets=None):
    return strat.answer(q, ctx or [], sheets=sheets or FakeSheets(), gemini=None)


class TestRouting(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(strat.detect_skill("what content gets the most comments?"), "comment_drivers")
        self.assertEqual(strat.detect_skill("which hooks invite replies?"), "comment_drivers")
        self.assertEqual(strat.detect_skill("what is the hypothesis behind this idea?"), "test_hypothesis")
        self.assertEqual(strat.detect_skill("which references map to this concept?"), "concept_references")
        self.assertEqual(strat.detect_skill("is the product role clear enough?"), "idea_diagnosis")
        self.assertEqual(strat.detect_skill("what should we kill in the calendar?"), "calendar_doctor")
        self.assertEqual(strat.detect_skill("what should we make more of? our learnings"),
                         "learning_to_action")
        self.assertEqual(strat.detect_skill("do we have enough parent content?"), "content_gap")
        self.assertEqual(strat.detect_skill("turn this into a shoot brief"), "shot_brief")
        # non-strategy asks stay out
        self.assertEqual(strat.detect_skill("give me 5 BodyShield ideas"), "")
        self.assertEqual(strat.detect_skill("what videos should we take inspiration from for gloves?"), "")


class TestCommentDrivers(unittest.TestCase):
    def test_comment_answer_is_inference_not_proof(self):
        out = ans("what content is most likely to get comments?")
        low = out.lower()
        self.assertTrue("don't have hard comment" in low or "no hard comment" in low
                        or "inference" in low)                 # honest about missing metric
        self.assertIn("Try", out)                              # a prompt/comment CTA
        self.assertNotRegex(low, r"\d+\s*comments")            # never a fabricated hard metric


class TestTestHypothesis(unittest.TestCase):
    def test_returns_success_and_failure_learning(self):
        out = ans("what are we trying to learn with the BodyShield turf-burn idea?", CTX)
        self.assertIn("If it wins", out)
        self.assertIn("If it loses", out)
        self.assertIn("Compare against", out)


class TestConceptReferences(unittest.TestCase):
    def test_returns_videos_not_idea_list(self):
        out = ans("what videos should we watch before shooting the BodyShield idea?", CTX)
        self.assertIn("@jasmines_main", out)                   # a specific external creator
        self.assertIn("[E1]", out)
        self.assertNotIn("Dive Without The Sting", out)        # NOT the idea list
        self.assertIn("reference", out.lower())


class TestIdeaDiagnosis(unittest.TestCase):
    def test_returns_weakness_and_fix(self):
        out = ans("why is this BodyShield idea weak?", CTX)
        self.assertIn("Weakness", out)
        self.assertIn("Fix", out)


class TestCalendarDoctor(unittest.TestCase):
    def test_revise_kill_moveup_max_three(self):
        out = ans("what should we revise in the calendar?")
        self.assertTrue(any(w in out for w in ("Revise", "Kill", "Move up")))
        self.assertLessEqual(out.count("• "), 3)               # max 3 by default

    def test_no_ratings_message(self):
        out = ans("what should we kill in the calendar?", sheets=FakeSheets(calendar=[]))
        self.assertIn("rate-calendar-ideas", out)


class TestLearningToAction(unittest.TestCase):
    def test_turns_learning_into_action(self):
        out = ans("what should we do because of our latest learnings?")
        self.assertIn("Learning", out)
        self.assertTrue("Do next" in out or "make more" in out.lower())


class TestContentGap(unittest.TestCase):
    def test_identifies_parents_gap(self):
        # brain has only Adult Amateur profiles -> Parents is a gap
        out = ans("where is the evidence thin?")
        self.assertIn("Parent", out)


class TestShotBrief(unittest.TestCase):
    def test_hook_beats_cta(self):
        out = ans("turn the BodyShield idea into a shoot brief", CTX)
        self.assertIn("Hook", out)
        self.assertIn("CTA", out)
        beats = len([ln for ln in out.splitlines() if __import__("re").match(r"\d+\. ", ln.strip())])
        self.assertGreaterEqual(beats, 4)
        self.assertLessEqual(beats, 6)

    def test_followup_turn_that_into_shoot_brief_resolves_context(self):
        out = ans("turn that into a shoot brief", CTX)
        self.assertIsNotNone(out)
        self.assertIn("Hook", out)                             # resolved subject from context

    def test_missing_context_asks_clarifying(self):
        out = strat.answer("turn that into a shoot brief", [], sheets=FakeSheets(
            profiles=[], connections=[], ideas=[], calendar=[]), gemini=None)
        self.assertIn("Which idea or product", out)


class TestValidationAndReadOnly(unittest.TestCase):
    def test_source_ids_validate(self):
        allowed = {"S1", "C1", "E1"}
        good = {"lead": "x", "recommendation": "y", "sources_used": ["S1", "E1"]}
        bad = {"lead": "x", "recommendation": "y", "sources_used": ["S1", "E9"]}
        self.assertTrue(strat._validate_strategy(good, allowed)[0])
        self.assertFalse(strat._validate_strategy(bad, allowed)[0])

    def test_external_as_proof_rejected(self):
        allowed = {"E1"}
        proofy = {"lead": "the external inspiration proves this works", "recommendation": "ship",
                  "sources_used": ["E1"]}
        ok, reason = strat._validate_strategy(proofy, allowed)
        self.assertFalse(ok)
        self.assertEqual(reason, "external as proof")

    def test_comment_metric_claim_rejected_when_unavailable(self):
        allowed = {"S1"}
        claim = {"lead": "this gets 400 comments", "recommendation": "ship", "sources_used": []}
        ok, reason = strat._validate_strategy(claim, allowed, require_metric_caveat=True)
        self.assertFalse(ok)

    def test_no_writes_across_all_skills(self):
        for q in ("what gets the most comments?",
                  "what are we trying to learn with the BodyShield idea?", CTX and
                  "what videos should we watch before shooting the BodyShield idea?",
                  "why is this BodyShield idea weak?", "what should we revise in the calendar?",
                  "what should we do because of our learnings?", "where is the evidence thin?",
                  "turn the BodyShield idea into a shoot brief"):
            sheets = FakeSheets()
            strat.answer(q, CTX, sheets=sheets, gemini=None)
            self.assertEqual(sheets.writes, 0, f"skill wrote to sheets for: {q}")


if __name__ == "__main__":
    unittest.main()
