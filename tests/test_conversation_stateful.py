"""Multi-turn conversation tests for the stateful Slack strategist UX.

Reproduces the production failure (Part N) and the multi-turn sequences (Part O/P):
context is carried across turns, "these ideas" resolves to the exact prior three,
"why" explains instead of re-listing, and response structure varies by intent.
Gemini is disabled so the deterministic path is exercised.

Run: python -m unittest tests.test_conversation_stateful
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import gemini_client
import inspiration_sheets
import conversation_resolver as R
import conversation_agent
import social_brain

_PROFILE = {"PROFILE_ID": "WFP-bs", "ACTIVE": "TRUE", "CONFIDENCE": "High",
            "PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur: Curiosity Gap + Demo",
            "PRODUCT": "BodyShield GK Leggings", "ICP": "Adult Amateur",
            "HOOK_TAGS": "Curiosity Gap, Fear / Risk", "FORMAT_TAGS": "Demo",
            "INTERNAL_SAMPLE_SIZE": "6",
            "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/"}
_CONN = {"CONNECTION_ID": "SC-1", "CONCEPT_NAME": "BodyShield turf-burn protection",
         "PRODUCT": "BodyShield GK Leggings", "CONNECTION_SCORE": "89",
         "STORYTELLING_STRUCTURE": "Curiosity Gap → Demo → Pain Reveal → Protection Reveal → CTA",
         "WHAT_TO_STEAL": "the wince moment", "WHAT_NOT_TO_COPY": "their caption",
         "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
         "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1;"
                                    "https://www.tiktok.com/@groundglorygk/video/2",
         "EXTERNAL_CREATORS": "@jasmines_main, @groundglorygk"}
_GLOVE = {"PROFILE_ID": "WFP-gk", "ACTIVE": "TRUE", "CONFIDENCE": "Medium",
          "PROFILE_NAME": "Gloves / Aspiring Pro: Authority + Tutorial", "PRODUCT": "Gloves",
          "ICP": "Aspiring Pro", "HOOK_TAGS": "Authority", "FORMAT_TAGS": "Tutorial",
          "INTERNAL_SAMPLE_SIZE": "4",
          "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/BBB/"}


def _idea(iid, title, score, concept):
    return {"IDEA_ID": iid, "STATUS": "", "IDEA_SCORE": score, "PRODUCT": "BodyShield GK Leggings",
            "ICP": "Adult Amateur", "CONFIDENCE": "High", "REFINED_IDEA_TITLE": title,
            "IDEA_TITLE": title, "REFINED_CONCEPT": concept, "CONCEPT": concept,
            "REFINED_HOOK": concept, "REFINED_SHOT_LIST": "Open on the sting | protected replay | CTA",
            "CTA": "Protect every dive — shop BodyShield.", "SOURCE_PROFILE_NAME": _PROFILE["PROFILE_NAME"],
            "STRATEGIC_PRIORITY_SCORE": "88", "EXECUTION_CLARITY_SCORE": "85",
            "FEASIBILITY_SCORE": "84", "EVIDENCE_FIT_SCORE": "90", "NOVELTY_SCORE": "78",
            "RECOMMENDED_SHOOT_PRIORITY": "High", "HOOK_TAGS": "Curiosity Gap",
            "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
            "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1",
            "RISK_NOTES": "Keep the pain moment tasteful.", "ORIGINAL_WEAKNESS": ""}


_IDEAS = [_idea("IDEA-1", "Dive Without The Sting", "94",
                "the wince before the dive then a protected replay demo"),
          _idea("IDEA-2", "Cost of Bare Knees", "88", "bare knees on turf protection demo"),
          _idea("IDEA-3", "The Full Dive", "82", "confidence aspiration full dive no hesitation"),
          dict(_idea("IDEA-gk", "Wet Weather Grip Myth", "80", "wet grip myth tutorial"),
               PRODUCT="Gloves", ICP="Aspiring Pro",
               SOURCE_PROFILE_NAME=_GLOVE["PROFILE_NAME"],
               INTERNAL_EVIDENCE_URLS="https://www.instagram.com/storellisoccer/reel/BBB/")]


class FakeSheets:
    def read_profiles(self):
        return [dict(_PROFILE), dict(_GLOVE)]

    def read_semantic_connections(self):
        return [dict(_CONN)]

    def read_ideas(self):
        return [dict(x) for x in _IDEAS]

    def read_calendar_ratings(self):
        return []

    def read_adhoc_evaluations(self):
        return []

    def __getattr__(self, name):
        if name.startswith("read_"):
            return lambda *a, **k: []
        if name.startswith("ensure_"):
            return lambda *a, **k: False
        raise AttributeError(name)


def _boom(*a, **k):
    raise RuntimeError("Gemini disabled")


class Base(unittest.TestCase):
    def setUp(self):
        self._patched = []
        self._real = inspiration_sheets.InspirationSheets
        for _n, mod in list(sys.modules.items()):
            if mod is not None and getattr(mod, "InspirationSheets", None) is self._real:
                setattr(mod, "InspirationSheets", FakeSheets)
                self._patched.append(mod)
        inspiration_sheets.InspirationSheets = FakeSheets
        self._gem = gemini_client.GeminiClient
        gemini_client.GeminiClient = _boom
        self._key = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""

    def tearDown(self):
        for mod in self._patched:
            setattr(mod, "InspirationSheets", self._real)
        inspiration_sheets.InspirationSheets = self._real
        gemini_client.GeminiClient = self._gem
        config.GEMINI_API_KEY = self._key

    def turn(self, text, ctx):
        return social_brain.answer_conversation(text, ctx)

    def recommend_three(self):
        t1 = self.turn("what are the strongest ideas to shoot?", [])
        ctx = [{"role": "user", "text": "what are the strongest ideas to shoot?"},
               {"role": "assistant", "text": t1}]
        return t1, ctx


# --------------------------------------------------------------------------- #
# Part D/C unit-level
# --------------------------------------------------------------------------- #
class TestClassifyResolve(Base):
    def test_dialogue_acts(self):
        self.assertEqual(R.classify_dialogue_act("why you proposed me these ideas?"), R.EXPLAIN)
        self.assertEqual(R.classify_dialogue_act("show me proof"), R.EVIDENCE)
        self.assertEqual(R.classify_dialogue_act("make it shorter"), R.SHORTER)
        self.assertEqual(R.classify_dialogue_act("turn the first one into a shoot brief"),
                         R.OPERATIONALIZE)
        self.assertEqual(R.classify_dialogue_act("are you sure?"), R.CHALLENGE)
        self.assertEqual(R.classify_dialogue_act("what if we make it for parents?"), R.MODIFY)
        self.assertEqual(R.classify_dialogue_act("why #1 over #2?"), R.COMPARE)
        self.assertEqual(R.classify_dialogue_act("forget those. what about gloves?"), R.RESET)
        self.assertEqual(R.classify_dialogue_act("what should we shoot?"), R.ASK_NEW)

    def test_resolve_these_ideas_to_prior_set(self):
        _t1, ctx = self.recommend_three()
        from slack_conversation_orchestrator import build_memory
        mem = build_memory(ctx, FakeSheets().read_ideas())
        res = R.resolve_referents("why you proposed me these ideas?", mem, FakeSheets().read_ideas())
        self.assertEqual(res["referent_type"], "idea_set")
        self.assertEqual([r["IDEA_ID"] for r in res["idea_records"]], ["IDEA-1", "IDEA-2", "IDEA-3"])


# --------------------------------------------------------------------------- #
# Part N — the exact production failure
# --------------------------------------------------------------------------- #
class TestProductionExample(Base):
    def test_why_these_explains_not_relists(self):
        t1, ctx = self.recommend_three()
        self.assertRegex(t1, r"(?m)^\*?1\.")               # turn 1 IS a list
        out = self.turn("why you proposed me these ideas?", ctx)
        body = out
        # does NOT repeat the ranked numbered list
        self.assertNotRegex(body, r"(?m)^\*?1\. .*score")
        # explains the common evidence pattern
        self.assertTrue("pattern" in out.lower() or "territory" in out.lower())
        # references the ideas / the first as strongest
        self.assertIn("Dive Without The Sting", out)
        self.assertTrue("strongest" in out.lower())
        # cites relevant evidence, external stays reference-only
        self.assertIn("*Sources:*", out)
        self.assertNotRegex(out.lower(), r"(external|inspiration|reference)[^.]{0,40}prov(e|es|en|ing)")
        # conversational length
        self.assertLess(len(re.sub(r"<[^>]+>", "", body).split()), 170)


# --------------------------------------------------------------------------- #
# Part O — multi-turn sequences
# --------------------------------------------------------------------------- #
class TestMultiTurn(Base):
    def test_why_then_explanation(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("why?", ctx)
        self.assertNotRegex(out, r"(?m)^\*?1\. .*score")
        self.assertTrue("pattern" in out.lower() or "territory" in out.lower())

    def test_why_first_over_second_compares(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("why #1 over #2?", ctx)
        self.assertIn("Dive Without The Sting", out)
        self.assertIn("Cost of Bare Knees", out)
        self.assertNotIn("The Full Dive", out)             # only the two compared

    def test_show_proof_expands_evidence(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("show me the evidence", ctx)
        self.assertIn("Evidence behind", out)
        self.assertIn("internal", out.lower())
        self.assertIn("*Sources:*", out)

    def test_make_it_shorter_compresses(self):
        t1, ctx = self.recommend_three()
        out = self.turn("make it shorter", ctx)
        self.assertLessEqual(len(out.split()), len(t1.split()))

    def test_shoot_brief_resolves_first(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("turn the first one into a shoot brief", ctx)
        self.assertIn("Hook", out)
        self.assertIn("CTA", out)

    def test_what_videos_routes_to_inspiration(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("what videos should we use for that?", ctx)
        self.assertIn("@", out)                            # external creators
        self.assertIn("execution reference", out.lower())

    def test_make_it_for_parents_shifts_icp_thin_proof(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("what if we make it for parents?", ctx)
        self.assertIn("Parents", out)
        self.assertIn("thin", out.lower())                 # honest about thin Parents proof

    def test_are_you_sure_challenges(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("are you sure?", ctx)
        self.assertTrue("confidence" in out.lower() or "bet" in out.lower())
        self.assertNotRegex(out.lower(), r"(external|inspiration)[^.]{0,40}prov(e|es|en|ing)")

    def test_forget_those_resets_topic(self):
        _t1, ctx = self.recommend_three()
        out = self.turn("forget those. what about gloves?", ctx)
        self.assertIn("Wet Weather Grip Myth", out)        # switched to gloves, fresh
        self.assertNotIn("Dive Without The Sting", social_brain.__dict__.get("_x", out))

    def test_reconstruct_from_thread_after_restart(self):
        # simulate a fresh process (no cache) — context comes only from Slack thread
        _t1, ctx = self.recommend_three()
        conversation_agent.CS._CACHE.clear()               # wipe any cache
        out = self.turn("why you proposed me these ideas?", ctx)
        self.assertTrue("pattern" in out.lower() or "territory" in out.lower())
        self.assertNotRegex(out, r"(?m)^\*?1\. .*score")


# --------------------------------------------------------------------------- #
# Part P — response variation
# --------------------------------------------------------------------------- #
class TestResponseVariation(Base):
    def test_shapes_differ_by_intent(self):
        t1, ctx = self.recommend_three()
        why = self.turn("why?", ctx)
        compare = self.turn("why #1 over #2?", ctx)
        # list-shaped answer has a numbered list; explanation does not
        self.assertRegex(t1, r"(?m)^\*?1\.")
        self.assertNotRegex(why, r"(?m)^\*?1\. .*score")
        # comparison names two, explanation reasons about the set
        self.assertIn("Cost of Bare Knees", compare)
        self.assertNotEqual(why, compare)                  # not the same template


if __name__ == "__main__":
    unittest.main()
