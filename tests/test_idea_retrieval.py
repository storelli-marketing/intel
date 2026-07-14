"""Tests for Milestone 4B — Slack rated-idea retrieval + critique (read-only).

Proves product/ICP retrieval, top-ideas ranking, critique + generic-language
flagging, shoot-first ranking, [S#]/[E#] source rendering (internal vs external
separated), no-ideas fallback, external-not-as-proof, and that retrieval performs
no write operations.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import idea_retrieval as ir


def _idea(**over):
    d = {
        "IDEA_ID": "IDEA-gloves-001", "STATUS": "Proposed",
        "IDEA_TITLE": "Wet Weather Grip Myth", "PRODUCT": "Gloves", "ICP": "Aspiring Pro",
        "HOOK": "Think wet weather kills your grip? Here's the fix.",
        "FORMAT": "Tutorial", "CONCEPT": "Debunk wet-grip myth with a controlled demo.",
        "SHOT_LIST": "keeper fumbles wet ball | grip technique | dry save",
        "CTA": "Shop Storelli Gloves",
        "IDEA_SCORE": "91", "STRATEGIC_PRIORITY_SCORE": "95",
        "EVIDENCE_FIT_SCORE": "82", "PRODUCT_FIT_SCORE": "100", "ICP_FIT_SCORE": "95",
        "EXECUTION_CLARITY_SCORE": "95", "NOVELTY_SCORE": "85", "FEASIBILITY_SCORE": "90",
        "COPYRIGHT_SAFETY_SCORE": "100", "RECOMMENDED_SHOOT_PRIORITY": "High",
        "SOURCE_PROFILE_NAME": "Gloves / Aspiring Pro: Education + Tutorial",
        "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/C9iLa3-Bw51/",
        "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@_jason_jamal/video/7086764976643329285",
        "IDEA_RATIONALE": "Maps to [S1] internal tutorial pattern; adapts [E1] mechanism.",
        "CONFIDENCE": "Medium",
    }
    d.update(over)
    return d


BODY = _idea(IDEA_ID="IDEA-bodyshield-001", PRODUCT="BodyShield GK Leggings",
             ICP="Adult Amateur", IDEA_TITLE="Turf Burn Before & After", IDEA_SCORE="94")
PARENT = _idea(IDEA_ID="IDEA-parents-001", PRODUCT="BodyShield GK Leggings",
               ICP="Parents", IDEA_TITLE="Keep Your Kid Diving Confidently", IDEA_SCORE="88")
GENERIC = _idea(IDEA_ID="IDEA-gloves-002", IDEA_TITLE="The Game-Changer",
                HOOK="Unleash your inner keeper and dominate", IDEA_SCORE="80")
HARD = _idea(IDEA_ID="IDEA-gloves-003", IDEA_TITLE="Hard Shoot",
             EXECUTION_CLARITY_SCORE="40", FEASIBILITY_SCORE="30",
             RECOMMENDED_SHOOT_PRIORITY="Low", IDEA_SCORE="85")

ALL = [_idea(), BODY, PARENT, GENERIC, HARD]


class FakeSheets:
    def __init__(self, ideas):
        self._ideas = ideas
        self.writes = 0

    def read_ideas(self):
        return list(self._ideas)

    # If retrieval ever tried to write, these would bump `writes` (they aren't used).
    def append_ideas(self, *a, **k):
        self.writes += 1

    def update_content_cells_bulk(self, *a, **k):
        self.writes += 1


class TestQueryParsing(unittest.TestCase):
    def test_is_idea_query(self):
        self.assertTrue(ir.is_idea_query("give me 5 ideas for BodyShield"))
        self.assertTrue(ir.is_idea_query("what should we shoot first?"))
        self.assertTrue(ir.is_idea_query("which ideas are too generic?"))
        self.assertFalse(ir.is_idea_query("turn this into a brief"))
        self.assertFalse(ir.is_idea_query("what is working for parents?"))

    def test_parse_filters_and_mode(self):
        self.assertEqual(ir.parse_query("give me 5 BodyShield ideas")["product"], "BodyShield")
        self.assertEqual(ir.parse_query("show me parent-facing ideas")["icp"], "Parents")
        self.assertEqual(ir.parse_query("critique the top ideas")["mode"], "critique")
        self.assertEqual(ir.parse_query("which ideas are too generic")["mode"], "generic")
        self.assertEqual(ir.parse_query("what should we shoot first")["mode"], "shoot_first")
        self.assertEqual(ir.parse_query("evidence behind idea #2")["mode"], "evidence")
        self.assertEqual(ir.parse_query("evidence behind idea #2")["target"], 2)


class TestRetrieval(unittest.TestCase):
    def test_bodyshield_retrieval(self):
        out = ir.answer_ideas("give me 5 BodyShield ideas", ideas=ALL)
        self.assertIn("Turf Burn Before & After", out)
        self.assertNotIn("Wet Weather Grip Myth", out)   # a Gloves idea, filtered out

    def test_parent_facing_retrieval(self):
        out = ir.answer_ideas("show me parent-facing ideas", ideas=ALL)
        self.assertIn("Keep Your Kid Diving Confidently", out)
        self.assertIn("Parents", out)

    def test_top_ideas_ranked_by_score(self):
        out = ir.answer_ideas("what are the best ideas we have?", ideas=ALL)
        # Highest score (BodyShield 94) should appear before the 91/88 ones.
        self.assertLess(out.index("Turf Burn Before & After"), out.index("Wet Weather Grip Myth"))

    def test_shoot_first_uses_production_priority(self):
        out = ir.answer_ideas("what should we shoot first?", ideas=ALL)
        self.assertIn("production practicality", out)
        # The Low-priority hard-to-shoot idea must not lead.
        self.assertNotIn("1. Hard Shoot", out)


class TestProductFamily(unittest.TestCase):
    def test_bodyshield_includes_related_pants_leggings(self):
        out = ir.answer_ideas("give me 5 BodyShield ideas", ideas=ALL)
        self.assertIn("Turf Burn Before & After", out)      # literal BodyShield
        # a Pants & Leggings idea is in the same family -> included
        pants = _idea(IDEA_ID="IDEA-pants-001", PRODUCT="Pants & Leggings",
                      ICP="Aspiring Pro", IDEA_TITLE="Slide Without Scars", IDEA_SCORE="90")
        out2 = ir.answer_ideas("give me 5 BodyShield ideas", ideas=ALL + [pants])
        self.assertIn("Slide Without Scars", out2)
        self.assertIn("map to the BodyShield family", out2)

    def test_labels_unchanged_in_output(self):
        pants = _idea(IDEA_ID="IDEA-pants-001", PRODUCT="Pants & Leggings",
                      ICP="Aspiring Pro", IDEA_TITLE="Slide Without Scars", IDEA_SCORE="90")
        out = ir.answer_ideas("give me BodyShield ideas", ideas=[BODY, pants])
        self.assertIn("Pants & Leggings", out)              # exact label preserved
        self.assertIn("BodyShield GK Leggings", out)        # not renamed

    def test_gloves_query_excludes_leggings_family(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=ALL)
        self.assertNotIn("Turf Burn Before & After", out)   # BodyShield excluded
        self.assertNotIn("Keep Your Kid Diving Confidently", out)
        self.assertNotIn("family", out)                     # no adjacency note

    def test_family_helper(self):
        self.assertEqual(ir._family_for("BodyShield GK Leggings"), "leggings")
        self.assertEqual(ir._family_for("Pants & Leggings"), "leggings")
        self.assertEqual(ir._family_for("Gloves"), "gloves")
        self.assertIsNone(ir._family_for("Random Widget"))


class TestCritique(unittest.TestCase):
    def test_generic_flagging(self):
        out = ir.answer_ideas("which ideas are too generic?", ideas=ALL)
        self.assertIn("The Game-Changer", out)
        self.assertRegex(out.lower(), r"game-changer|unleash|dominate|inner keeper")
        self.assertIn("haven't changed the sheet", out.lower().replace("’", "'")
                      if "haven't" in out.lower() else out)

    def test_critique_is_blunt(self):
        out = ir.answer_ideas("critique the top ideas", ideas=[HARD])
        self.assertIn("Hard to shoot", out)

    def test_generic_flags_helper(self):
        self.assertTrue(ir.generic_language_flags(GENERIC))
        self.assertFalse(ir.generic_language_flags(_idea()))


class TestSourceRendering(unittest.TestCase):
    def test_sources_have_s_and_e_separated(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=[_idea()])
        self.assertIn("Sources:", out)
        self.assertIn("[S1] <https://www.instagram.com/storellisoccer", out)
        self.assertIn("Storelli internal evidence", out)
        self.assertIn("[E1] <https://www.tiktok.com/@_jason_jamal", out)
        self.assertIn("External inspiration", out)
        # Internal proof and external inspiration are labeled separately.
        self.assertIn("Internal proof:", out)
        self.assertIn("External inspiration (reference only)", out)

    def test_evidence_mode(self):
        out = ir.answer_ideas("show me the evidence behind the top idea", ideas=[_idea()])
        self.assertIn("Evidence behind", out)
        self.assertIn("internal winning profile", out.lower())
        self.assertIn("not proof", out.lower())


class TestGuardrails(unittest.TestCase):
    def test_no_ideas_fallback(self):
        out = ir.answer_ideas("give me ideas", ideas=[])
        self.assertIn("don't have any rated ideas", out.lower().replace("’", "'"))

    def test_no_ideas_uses_fallback_callable(self):
        out = ir.answer_ideas("give me ideas", ideas=[], fallback=lambda: "FALLBACK-USED")
        self.assertEqual(out, "FALLBACK-USED")

    def test_external_never_presented_as_proof(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=[_idea()])
        low = out.lower()
        self.assertIn("not proof", low)
        # Never claims external views prove performance.
        self.assertNotRegex(low, r"views prove|proves it works|proven by views")

    def test_retrieval_is_read_only(self):
        sheets = FakeSheets(ALL)
        ir.answer_ideas("give me 5 BodyShield ideas", sheets=sheets)
        ir.answer_ideas("critique the top ideas", sheets=sheets)
        ir.answer_ideas("what should we shoot first?", sheets=sheets)
        self.assertEqual(sheets.writes, 0)   # zero write operations

    def test_ineligible_ideas_excluded(self):
        approved = _idea(STATUS="Published")
        no_internal = _idea(INTERNAL_EVIDENCE_URLS="", INTERNAL_EVIDENCE_IDS="")
        out = ir.answer_ideas("give me gloves ideas", ideas=[approved, no_internal])
        self.assertIn("don't have any eligible", out.lower().replace("’", "'"))


if __name__ == "__main__":
    unittest.main()
