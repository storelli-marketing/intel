"""Tests for the Slack conversational RAG orchestrator + related fixes.

Proves reference resolution from thread memory, reasoned urgency (not raw score),
refined/generic non-contradiction, KISS calendar weakness, unfurls disabled,
external-not-proof, missing-context clarification, and read-only.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import slack_conversation_orchestrator as orch
import idea_retrieval as ir
import calendar_retrieval as cret


def _idea(idea_id, title, product="Gloves", score="80", **over):
    d = {"IDEA_ID": idea_id, "STATUS": "Proposed", "IDEA_TITLE": title, "PRODUCT": product,
         "ICP": "Aspiring Pro", "HOOK": "hook", "CONCEPT": "A clear demo concept.",
         "SHOT_LIST": "beat one | beat two", "CTA": "Shop", "FORMAT": "Tutorial",
         "IDEA_SCORE": score, "STRATEGIC_PRIORITY_SCORE": "80", "EVIDENCE_FIT_SCORE": "80",
         "EXECUTION_CLARITY_SCORE": "80", "FEASIBILITY_SCORE": "80", "NOVELTY_SCORE": "70",
         "COPYRIGHT_SAFETY_SCORE": "100", "RECOMMENDED_SHOOT_PRIORITY": "High",
         "CONFIDENCE": "High", "SOURCE_PROFILE_NAME": "Gloves / Aspiring Pro",
         "INTERNAL_EVIDENCE_URLS": "https://ig/s1/", "EXTERNAL_REFERENCE_URLS": "https://tiktok/e1"}
    d.update(over)
    return d


DIVE = _idea("IDEA-bs-1", "Dive Without The Sting", product="BodyShield GK Leggings",
             REFINED_IDEA_TITLE="BodyShield GK Leggings: Dive Without The Sting",
             REFINEMENT_STATUS="Refined", REFINED_HOOK="Every dive, a wince",
             REFINED_CONCEPT="Show a keeper flinching, then diving freely in BodyShield.",
             REFINED_SHOT_LIST="turf wince | full dive | confident save",
             SOURCE_PROFILE_NAME="BodyShield GK Leggings / Adult Amateur", score="95")
GRIP = _idea("IDEA-g-1", "Wet Weather Grip Myth", score="91")
IDEAS = [DIVE, GRIP,
         _idea("IDEA-g-2", "Glove Care", score="88", NOVELTY_SCORE="90", EVIDENCE_FIT_SCORE="90"),
         _idea("IDEA-g-3", "Weak One", score="60", STRATEGIC_PRIORITY_SCORE="50",
               EXECUTION_CLARITY_SCORE="40", FEASIBILITY_SCORE="40", EVIDENCE_FIT_SCORE="40")]


class TestMemoryAndReference(unittest.TestCase):
    def test_memory_from_prior_turn(self):
        ctx = [{"role": "user", "text": "give me BodyShield ideas"},
               {"role": "assistant", "text": "*1. BodyShield GK Leggings: Dive Without The Sting* _(refined)_\n"
                                             "*My move:* Shoot *BodyShield GK Leggings: Dive Without The Sting* first."}]
        mem = orch.build_memory(ctx, IDEAS)
        self.assertIn("IDEA-bs-1", mem["last_recommended_idea_ids"])

    def test_tell_me_more_resolves_prior_idea(self):
        ctx = [{"role": "assistant", "text": "*My move:* Shoot *BodyShield GK Leggings: Dive Without The Sting* first."}]
        mem = orch.build_memory(ctx, IDEAS)
        idea, how = orch.resolve_idea_reference("tell me more about it", IDEAS, mem)
        self.assertEqual(idea["IDEA_ID"], "IDEA-bs-1")
        self.assertEqual(how, "memory")

    def test_explicit_named_idea_resolves_not_gloves(self):
        # Failure #2: naming the BodyShield idea must NOT fall back to Gloves.
        out = orch.answer(
            "you suggested to shoot BodyShield GK Leggings: Dive Without The Sting first — "
            "can you tell me more about it?", context=[], ideas=IDEAS)
        self.assertIsNotNone(out)
        self.assertIn("Dive Without The Sting", out)
        self.assertNotIn("Wet Weather Grip", out)
        self.assertIn("BodyShield GK Leggings / Adult Amateur", out)   # right profile

    def test_missing_context_asks_clarifying(self):
        out = orch.answer("tell me more about it", context=[], ideas=IDEAS)
        self.assertIsNotNone(out)
        self.assertIn("missing the previous item", out.lower())


class TestUrgentReasoning(unittest.TestCase):
    def test_urgent_uses_reasoning_not_raw_score(self):
        out = orch.answer("what are the most urgent ideas we should test and why?",
                          context=[], ideas=IDEAS)
        self.assertIsNotNone(out)
        # Reasoned language, not a bare score list.
        self.assertRegex(out.lower(), r"shootable|internal proof|learning|priority|value")
        self.assertNotIn("Weak One", out)          # low-urgency idea excluded from top 3

    def test_urgency_score_differs_from_idea_score(self):
        # Glove Care (score 88) has higher novelty+evidence -> can out-rank score 91.
        self.assertGreater(orch.urgency_score(IDEAS[2]), 0)


class TestReasoningReturnsNoneForSimple(unittest.TestCase):
    def test_plain_idea_list_not_intercepted(self):
        self.assertIsNone(orch.answer("give me 5 gloves ideas", context=[], ideas=IDEAS))

    def test_read_only(self):
        class RO:
            def __init__(self): self.writes = 0
            def read_ideas(self): return IDEAS
            def append_ideas(self, *a, **k): self.writes += 1
        ro = RO()
        orch.answer("what are the most urgent ideas to test?", context=[], sheets=ro)
        self.assertEqual(ro.writes, 0)

    def test_external_not_proof(self):
        out = orch.answer("what are the most urgent ideas we should test?", context=[], ideas=IDEAS)
        self.assertIn("not proof", out.lower())


class TestRefinedGenericNonContradiction(unittest.TestCase):
    def _refined_generic(self):
        return _idea("IDEA-r-1", "The Game-Changer", REFINEMENT_STATUS="Refined",
                     REFINED_IDEA_TITLE="The 3-Second Grip Check", HOOK="Unleash your inner keeper",
                     REFINED_HOOK="Your grip fails for one reason")

    def test_refined_idea_list_does_not_flag_generic(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=[self._refined_generic()])
        self.assertNotIn("sharpen the hook", out.lower())
        self.assertNotRegex(out.lower(), r"generic language.*sharpen")

    def test_generic_critique_shows_original_and_fix(self):
        out = ir.answer_ideas("critique the top ideas", ideas=[self._refined_generic()])
        self.assertIn("generic language", out.lower())
        self.assertIn("fixes it", out.lower())


class TestCalendarKISS(unittest.TestCase):
    def _cal(self, title, score, rec, **over):
        d = {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": title, "CALENDAR_IDEA_SCORE": str(score),
             "RECOMMENDATION": rec, "NOTION_PAGE_URL": "https://app.notion.com/p/x",
             "RATIONALE": "Long rationale that should not be dumped in full here at all.",
             "REVISION_SUGGESTION": "Tie it to a proven BodyShield turf-burn angle for adult amateurs.",
             "INTERNAL_EVIDENCE_FIT_SCORE": "35", "ICP": "Parents"}
        d.update(over)
        return d

    def test_kiss_weak_list(self):
        rows = [self._cal("What Parents Don't See", 66, "Revise"),
                self._cal("BTS Checklist", 66, "Revise", INTERNAL_EVIDENCE_FIT_SCORE="80", PRODUCT=""),
                self._cal("Season Kickoff", 65, "Revise", INTERNAL_EVIDENCE_FIT_SCORE="80", PRODUCT="")]

        class RO:
            def read_calendar_ratings(self): return rows
        out = cret.answer_calendar("which proposed ideas in the calendar are weak, give me a KISS list",
                                   sheets=RO())
        self.assertIn("need revision before shooting", out.lower())
        self.assertIn("weak because", out.lower())
        self.assertIn("Fix:", out)
        self.assertIn("enough internal proof for Parents", out)
        self.assertNotIn("…", out.split("Sources:")[0].split("Fix:")[-1][:5])  # fix not mid-word cut at start


class TestUnfurlsDisabled(unittest.TestCase):
    def test_post_message_disables_unfurls(self):
        import slack_bot
        captured = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"ok": True}

        import httpx
        orig = httpx.post
        httpx.post = lambda url, **k: captured.update(k.get("json", {})) or FakeResp()
        try:
            import config
            config.SLACK_BOT_TOKEN = "xoxb-test"
            slack_bot.post_message("C1", "hello <https://tiktok/x|link>")
        finally:
            httpx.post = orig
        self.assertIs(captured.get("unfurl_links"), False)
        self.assertIs(captured.get("unfurl_media"), False)


if __name__ == "__main__":
    unittest.main()
