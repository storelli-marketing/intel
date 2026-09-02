"""Tests for Milestone 4B — Slack rated-idea retrieval + critique (read-only).

Proves product/ICP retrieval, top-ideas ranking, critique + generic-language
flagging, shoot-first ranking, [S#]/[E#] source rendering (internal vs external
separated), no-ideas fallback, external-not-as-proof, and that retrieval performs
no write operations.

Run: python -m unittest discover -s tests
"""
import os
import sys
import re
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import idea_retrieval as ir
import slack_response_style as st


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
        self.assertIn("BodyShield family", out2)

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
        self.assertIn("too generic", out.lower())

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
        # Internal proof and external inspiration stay SEPARATE id spaces. They
        # are no longer restated as "proof [S1] · ref [E1]" on every idea line —
        # repeating that scaffold per item is what made a list read as one line
        # copy-pasted N times — so the property is asserted on the block that
        # actually carries the links.
        s_line = next(l for l in out.splitlines() if "[S1]" in l)
        e_line = next(l for l in out.splitlines() if "[E1]" in l)
        self.assertIn("Storelli internal evidence", s_line)
        self.assertIn("External inspiration", e_line)
        self.assertNotIn("[E", s_line)
        self.assertNotIn("[S", e_line)

    def test_evidence_mode(self):
        out = ir.answer_ideas("show me the evidence behind the top idea", ideas=[_idea()])
        self.assertIn("Evidence behind", out)
        self.assertIn("internal winning profile", out.lower())
        self.assertIn("not proof", out.lower())


class TestRequestedCount(unittest.TestCase):
    """The Slack failure this locks: "what are the top 10 ideas we should focus on
    to make videos?" answered with 3, gave no reason, and repeated the same
    scaffold on every line.

    Causes were (a) `\\b([1-9])\\b` matching a single digit only, so "10" parsed as
    no count at all, and (b) a hard ceiling of 5 that applied even to an explicit
    ask. Both are asserted here, plus the shortfall explanation.
    """

    HOOKS = ("Every dive, a wince", "That raw, stinging knee", "The split-second pull-back",
             "Same slide, two knees", "Why keepers stop sliding", "The check nobody does",
             "Wet weather grip myth", "The moment a parent hesitates",
             "The kit check that gets skipped", "The dive you pull out of",
             "A season of turf", "What lives in your gloves")

    def pool(self, n, **over):
        out = []
        for i in range(n):
            out.append(_idea(IDEA_ID=f"IDEA-{i:03d}", IDEA_TITLE=f"Idea Number {i + 1}",
                             HOOK=self.HOOKS[i % len(self.HOOKS)],
                             IDEA_SCORE=str(95 - i), **over))
        return out

    # --- the count is read at all ------------------------------------------
    def test_multi_digit_count_is_parsed(self):
        q = ir.parse_query("what are the top 10 ideas we should focus on to make videos?")
        self.assertEqual(q["count"], 10)
        self.assertTrue(q["count_explicit"])

    def test_single_digit_count_still_parsed(self):
        q = ir.parse_query("give me 5 BodyShield ideas")
        self.assertEqual(q["count"], 5)
        self.assertTrue(q["count_explicit"])

    def test_no_count_asked_keeps_the_small_default(self):
        q = ir.parse_query("give me ideas")
        self.assertFalse(q["count_explicit"])
        self.assertEqual(ir._cap(q, st.MODE_DEFAULT), 3)

    def test_an_ordinal_reference_is_not_a_count(self):
        q = ir.parse_query("critique idea 2")
        self.assertFalse(q["count_explicit"])
        self.assertEqual(q["target"], 2)

    def test_a_product_size_is_not_a_count(self):
        self.assertFalse(ir.parse_query("GK 3/4 leggings ideas")["count_explicit"])

    # --- the count is honoured ---------------------------------------------
    def test_ten_asked_and_ten_available_returns_ten(self):
        out = ir.answer_ideas("what are the top 10 ideas we should focus on to make videos?",
                              ideas=self.pool(12))
        for n in range(1, 11):
            self.assertIn(f"*{n}. ", out, f"idea {n} missing")
        self.assertNotIn("*11. ", out)
        self.assertNotIn("idea(s)", out)          # no robotic pluralisation

    def test_explicit_count_beats_the_old_ceiling_of_five(self):
        out = ir.answer_ideas("give me 8 ideas", ideas=self.pool(10))
        self.assertIn("*8. ", out)

    def test_long_list_is_not_silently_trimmed_by_the_word_cap(self):
        """A 10-item list must survive `enforce_length`, which trims on line
        boundaries — showing 10 and then cutting to 4 is the same bug."""
        out = ir.answer_ideas("top 10 ideas", ideas=self.pool(12))
        numbered = [l for l in out.splitlines() if re.match(r"^\*\d+\. ", l)]
        self.assertEqual(len(numbered), 10)

    def test_concise_does_not_override_an_explicit_count(self):
        out = ir.answer_ideas("briefly, give me 10 ideas", ideas=self.pool(12))
        numbered = [l for l in out.splitlines() if re.match(r"^\*\d+\. ", l)]
        self.assertEqual(len(numbered), 10)

    # --- a shortfall is explained ------------------------------------------
    def test_shortfall_says_how_many_and_why_nothing_more(self):
        out = ir.answer_ideas("top 10 ideas", ideas=self.pool(6))
        self.assertIn("You asked for 10", out)
        self.assertIn("6", out)
        self.assertIn("everything we've generated", out)

    def test_shortfall_names_the_eligibility_bar(self):
        pool = self.pool(4) + [_idea(IDEA_ID=f"BAD-{i}", IDEA_TITLE=f"Bad {i}",
                                     INTERNAL_EVIDENCE_URLS="")
                               for i in range(5)]
        out = ir.answer_ideas("top 10 ideas", ideas=pool)
        self.assertIn("You asked for 10", out)
        self.assertIn("don't clear the bar", out)
        self.assertIn("5", out)

    def test_shortfall_names_an_out_of_scope_filter(self):
        pool = (self.pool(2, PRODUCT="Gloves", ICP="Aspiring Pro")
                + self.pool(6, PRODUCT="ExoShield Head Guard", ICP="Parents"))
        out = ir.answer_ideas("give me 10 gloves ideas", ideas=pool)
        self.assertIn("You asked for 10", out)
        self.assertIn("other products/audiences", out)

    def test_readability_cap_is_explained_as_a_cap_not_a_shortage(self):
        out = ir.answer_ideas("give me 30 ideas", ideas=self.pool(40))
        self.assertIn(f"I'll give you {ir.MAX_IDEAS}", out)
        self.assertIn("in the pool", out)
        self.assertNotIn("everything we've generated", out)

    def test_met_ask_does_not_apologise(self):
        out = ir.answer_ideas("top 5 ideas", ideas=self.pool(9))
        self.assertNotIn("You asked for", out)
        self.assertIn("Here are 5", out)

    def test_shortfall_note_is_empty_when_the_ask_is_met(self):
        sup = {"asked": 5, "available": 9, "total": 9, "ineligible": 0, "out_of_scope": 0}
        self.assertEqual(ir.shortfall_note(sup, 5), "")

    def test_supply_counts_are_real(self):
        pool = (self.pool(3, PRODUCT="Gloves")
                + self.pool(2, PRODUCT="Sliders")
                + [_idea(IDEA_ID="X", INTERNAL_EVIDENCE_URLS="", PRODUCT="Gloves")])
        sup = ir.supply(pool, ir.parse_query("give me 10 gloves ideas"))
        self.assertEqual(sup["total"], 6)
        self.assertEqual(sup["ineligible"], 1)
        self.assertEqual(sup["out_of_scope"], 2)
        self.assertEqual(sup["available"], 3)


class TestConversationalShape(unittest.TestCase):
    """Not a template dump. The screenshot repeated
    "_· shoot High · shootable, no big weakness_ · proof [S1] · ref [E1]"
    verbatim under every idea."""

    def pool(self, n, **over):
        return [_idea(IDEA_ID=f"I-{i}", IDEA_TITLE=f"Title {i + 1}",
                      HOOK=f"Distinct hook number {i + 1}", IDEA_SCORE=str(95 - i), **over)
                for i in range(n)]

    def test_no_repeated_scaffold_per_line(self):
        out = ir.answer_ideas("top 6 ideas", ideas=self.pool(6))
        self.assertNotIn("proof [S", out.split("*Sources:*")[0])
        self.assertNotIn("ref [E", out.split("*Sources:*")[0])
        self.assertNotIn("shootable, no big weakness", out)

    def test_filler_risk_is_omitted_not_printed(self):
        self.assertEqual(ir._real_risk(_idea()), "")

    def test_priority_is_not_restated_on_every_item(self):
        out = ir.answer_ideas("top 5 ideas",
                              ideas=self.pool(5, RECOMMENDED_SHOOT_PRIORITY="High"))
        self.assertLessEqual(out.count("Worth doing early"), 1)

    def test_a_low_priority_item_still_says_so(self):
        pool = self.pool(2, RECOMMENDED_SHOOT_PRIORITY="High")
        pool += [_idea(IDEA_ID="L", IDEA_TITLE="Parked One", HOOK="A quieter angle",
                       IDEA_SCORE="70", RECOMMENDED_SHOOT_PRIORITY="Low")]
        out = ir.answer_ideas("top 3 ideas", ideas=pool)
        self.assertIn("park", out.lower())

    def test_identical_hooks_are_not_printed_twice(self):
        pool = [_idea(IDEA_ID=f"S-{i}", IDEA_TITLE=f"Title {i}",
                      HOOK="The exact same hook", IDEA_SCORE=str(90 - i))
                for i in range(4)]
        out = ir.answer_ideas("top 4 ideas", ideas=pool)
        self.assertEqual(out.lower().count("the exact same hook"), 1)

    def test_deduped_hook_falls_back_to_the_concept(self):
        """Dropping a repeated hook must not leave a bare title behind."""
        pool = [_idea(IDEA_ID=f"I{i}", IDEA_TITLE=f"Idea {i + 1}",
                      HOOK="One shared hook",
                      CONCEPT=f"Concept variant {i + 1} showing the protection moment",
                      IDEA_SCORE=str(95 - i)) for i in range(6)]
        out = ir.answer_ideas("top 6 ideas", ideas=pool)
        self.assertEqual(out.lower().count("one shared hook"), 1)
        for n in range(2, 7):
            self.assertIn(f"Concept variant {n}", out)

    def test_no_double_punctuation_from_a_question_hook(self):
        out = ir.answer_ideas("give me ideas",
                              ideas=[_idea(HOOK="Think wet weather kills your grip?")])
        self.assertNotIn("?.", out)
        self.assertIn("grip?", out)

    def test_sentence_helper_adds_one_terminator(self):
        self.assertEqual(ir._sentence("a statement"), "a statement.")
        self.assertEqual(ir._sentence("a question?"), "a question?")
        self.assertEqual(ir._sentence("done."), "done.")
        self.assertEqual(ir._sentence(""), "")

    def test_every_item_in_a_long_list_says_something(self):
        """No bare titles. The top few carry their reason on the following line;
        the tail carries it inline after an em dash."""
        body = ir.answer_ideas("top 10 ideas", ideas=self.pool(10))
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if not re.match(r"^\*\d+\. ", line):
                continue
            inline = "\u2014" in line
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            follows = bool(nxt.strip()) and not re.match(r"^\*\d+\. ", nxt) \
                and not nxt.startswith("*My move")
            self.assertTrue(inline or follows, f"bare title with no reason: {line}")

    def test_closing_move_is_actionable(self):
        out = ir.answer_ideas("top 10 ideas", ideas=self.pool(10))
        self.assertIn("*My move:*", out)
        self.assertIn("Start with", out)


def _refined_idea(**over):
    d = _idea(REFINEMENT_STATUS="Refined",
              REFINED_IDEA_TITLE="The 3-Second Grip Check Keepers Skip",
              REFINED_HOOK="Your grip fails on wet shots because of one setup mistake.",
              REFINED_CONCEPT="Three concrete grip checkpoints to self-test pre-match.",
              REFINED_SHOT_LIST="hand seam on ball | wet-ball catch | before/after hold",
              CREATIVE_DIRECTOR_NOTES="Cut the hype; led with a testable mechanic.",
              ORIGINAL_WEAKNESS="generic hype in title/hook (game changer); generic language (game changer)",
              IDEA_TITLE="The Game-Changer", HOOK="Unleash your inner keeper and dominate")
    d.update(over)
    return d


class TestRefinedPreference(unittest.TestCase):
    def test_uses_refined_title_and_hook(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=[_refined_idea()])
        self.assertIn("The 3-Second Grip Check Keepers Skip", out)   # refined title
        self.assertIn("one setup mistake", out)                       # refined hook
        self.assertNotIn("The Game-Changer", out)                     # original title hidden
        self.assertNotIn("Unleash your inner keeper", out)            # original hook hidden
        # The substantive guarantee is that the REFINED fields are the ones shown.
        # The old " _(refined)_" tag is deliberately gone: naming an internal
        # pipeline stage next to a title is the kind of process label that makes
        # a Slack answer read like a system dump rather than a colleague talking.
        self.assertNotIn("(refined)", out.lower())

    def test_fallback_to_original_when_unrefined(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=[_idea()])   # no refinement
        self.assertIn("Wet Weather Grip Myth", out)                   # original title used
        self.assertNotIn("Showing refined", out)

    def test_fallback_when_refined_field_empty(self):
        # Status Refined but REFINED_HOOK empty -> original hook.
        idea = _refined_idea(REFINED_HOOK="")
        out = ir.answer_ideas("give me gloves ideas", ideas=[idea])
        self.assertIn("Unleash your inner keeper", out)               # original hook fallback

    def test_critique_uses_stored_weakness_and_notes(self):
        out = ir.answer_ideas("critique the top ideas", ideas=[_refined_idea()])
        self.assertIn("Creative director:", out)                      # CREATIVE_DIRECTOR_NOTES
        self.assertIn("generic language", out.lower())                # still mentions original generic
        # De-duplicated weakness: "game changer" appears once in the weakness line.
        self.assertLessEqual(out.lower().count("generic hype in title/hook"), 1)

    def test_generic_mode_shows_refined_fix(self):
        out = ir.answer_ideas("which ideas are too generic?", ideas=[_refined_idea()])
        self.assertIn("already refined", out.lower())
        self.assertIn("The 3-Second Grip Check Keepers Skip", out)

    def test_evidence_sources_unchanged_with_refined(self):
        out = ir.answer_ideas("show me the evidence behind the top idea", ideas=[_refined_idea()])
        self.assertIn("[S1] <https://www.instagram.com/storellisoccer", out)   # source exact
        self.assertIn("[E1] <https://www.tiktok.com/@_jason_jamal", out)
        self.assertIn("not proof", out.lower())
        self.assertIn("The 3-Second Grip Check Keepers Skip", out)             # refined title


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
        self.assertIn("no eligible", out.lower())


if __name__ == "__main__":
    unittest.main()
