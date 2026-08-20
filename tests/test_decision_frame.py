"""Active decision frame — unit + multi-turn regression.

The golden case is the exact production exchange that failed: an analysis turn
established BodyShield, and the next turn ("what should we shoot next week based
on the latest data you just shared?") threw it away and re-ranked the global idea
pool, recommending Gloves. The third turn then answered a challenge with a
generic confidence line.

These tests assert the SHARED mechanism, not the wording: frame derivation,
inheritance, precedence over keyword handlers, objective inheritance, and the
epistemic challenge/falsification contract.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
sys.path.insert(0, _HERE)

import conversation_resolver as R
import decision_frame as DF
import evidence_contract as EC
import frame_reasoning as FR

# A realistic analysis answer of the shape the strategist path produces.
ANALYSIS = (
    "My read: Content for *BodyShield Leggings* performs best when it uses a "
    "*Curiosity Gap* or *Fear / Risk* hook, especially combined with *Reaction* "
    "formats, directly showcasing protection for the *Aspiring Pro* audience. "
    "Reaction is associated with a stronger Great rate in the current sample. "
    "Proof: [S1] [S2] [S3]")
GLOVES_ANALYSIS = (
    "For *Gloves*, *Education* + *Tutorial* is the pattern that works for the "
    "*Aspiring Pro* audience — grip care is the strongest territory we have. [S1] [S2]")
PARENTS_ANALYSIS = (
    "For *Parents* the evidence is thin — only 2 posts, so I wouldn't call any "
    "pattern proven. *Fear / Risk* is the nearest signal. [S1]")


def thread(*pairs):
    ctx = []
    for user, assistant in pairs:
        ctx.append({"role": "user", "text": user})
        if assistant is not None:
            ctx.append({"role": "assistant", "text": assistant})
    return ctx


def idea(title, product, hook, fmt, **scores):
    base = {"IDEA_ID": title[:6], "IDEA_TITLE": title, "PRODUCT": product,
            "HOOK": hook, "FORMAT": fmt, "ICP": "Aspiring Pro",
            "STATUS": "proposed", "IDEA_SCORE": 80,
            "STRATEGIC_PRIORITY_SCORE": 80, "FEASIBILITY_SCORE": 80,
            "EXECUTION_CLARITY_SCORE": 80, "NOVELTY_SCORE": 70,
            "EVIDENCE_FIT_SCORE": 80, "PRODUCT_FIT_SCORE": 90,
            "COPYRIGHT_SAFETY_SCORE": 95, "CONFIDENCE": "Medium",
            "RECOMMENDED_SHOOT_PRIORITY": "High",
            "SHOT_LIST": "OPEN: close-up on a scraped knee.",
            "SOURCE_PROFILE_NAME": f"{product} winning profile",
            "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/reel/AAA/",
            "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@x/video/1"}
    base.update(scores)
    return base


IDEAS = [
    idea("The Cost of Bare Knees", "BodyShield GK Leggings", "Fear / Risk", "Reaction",
         IDEA_SCORE=88, EVIDENCE_FIT_SCORE=92),
    idea("Dive Without The Sting", "BodyShield GK Leggings", "Curiosity Gap", "Demo",
         IDEA_SCORE=86, EVIDENCE_FIT_SCORE=84),
    idea("Extend Your Gloves' Life", "Gloves", "Education", "Tutorial",
         IDEA_SCORE=95, EVIDENCE_FIT_SCORE=95, NOVELTY_SCORE=90,
         RECOMMENDED_SHOOT_PRIORITY="High", FEASIBILITY_SCORE=99),
    idea("The Stone Hand Mistake", "Gloves", "Fear / Risk", "Story",
         IDEA_SCORE=93, EVIDENCE_FIT_SCORE=90, FEASIBILITY_SCORE=97),
]


class TestFrameDerivation(unittest.TestCase):
    def test_frame_matches_the_established_conclusion(self):
        f = DF.derive(thread(("What is working for BodyShield right now?", ANALYSIS)), "")
        self.assertEqual(f["scope"]["product"], ["BodyShield Leggings"])
        self.assertEqual(f["scope"]["icp"], ["Aspiring Pro"])
        self.assertIn("Curiosity Gap", f["scope"]["hook"])
        self.assertIn("Reaction", f["scope"]["format"])
        self.assertTrue(f["prior_findings"])
        self.assertEqual(f["evidence_refs"], ["S1", "S2", "S3"])
        self.assertTrue(DF.is_active(f))

    def test_analysis_turn_is_not_recorded_as_a_recommendation(self):
        """A bolded taxonomy option is the SUBJECT of an analysis. Recording it as
        `prior_recommendation` would make an analytical turn look like a decision
        and give the challenge path the wrong referent."""
        f = DF.derive(thread(("What is working for BodyShield?", ANALYSIS)), "")
        self.assertIsNone(f["prior_recommendation"])

    def test_no_frame_before_anything_is_established(self):
        self.assertFalse(DF.is_active(DF.derive([], "what should we shoot?")))

    def test_frame_does_not_drift_under_its_own_output(self):
        """An answer given inside the frame cites its own sources. Re-anchoring on
        the latest turn moved the ICP to whatever appeared in a citation label."""
        rec = ("Off the back of that, *Dive Without The Sting* is the one I'd shoot. "
               "Sources: BodyShield GK Leggings / *Adult Amateur*: Curiosity Gap + "
               "*Do / Don't*")
        f = DF.derive(thread(("What is working for BodyShield?", ANALYSIS),
                             ("What should we shoot based on that?", rec)),
                      "are you sure?")
        self.assertEqual(f["scope"]["icp"], ["Aspiring Pro"], "ICP drifted")
        self.assertIn("Reaction", f["scope"]["format"])
        self.assertNotIn("Do / Don't", f["scope"]["format"])
        # what was recommended IS the freshest fact and should update
        self.assertEqual(f["prior_recommendation"], "Dive Without The Sting")


class TestInheritance(unittest.TestCase):
    def setUp(self):
        self.frame = DF.derive(thread(("What is working for BodyShield?", ANALYSIS)), "")

    def test_the_exact_production_turn_inherits(self):
        self.assertTrue(DF.inherits(
            "What should we shoot next week based on the latest data you just shared?",
            self.frame))

    def test_bare_continuations_inherit_only_with_a_live_frame(self):
        self.assertTrue(DF.inherits("So what should we shoot?", self.frame))
        self.assertFalse(DF.inherits("So what should we shoot?", DF.new_frame()))

    def test_challenge_turns_inherit(self):
        for q in ("Are you sure? What would change your mind?", "why?",
                  "How sure are you?", "What's the argument against this?"):
            self.assertTrue(DF.inherits(q, self.frame), q)

    def test_explicit_broadening_does_not_inherit(self):
        for q in ("Actually, strongest idea across all products?",
                  "What's the best idea across the whole calendar?",
                  "Forget that — new question."):
            self.assertFalse(DF.inherits(q, self.frame), q)
            self.assertTrue(DF.wants_reset(q), q)

    def test_naming_a_different_product_is_a_new_scope(self):
        self.assertTrue(DF.names_new_scope("What is working for gloves?", self.frame))
        self.assertFalse(DF.names_new_scope("What should we shoot?", self.frame))


class TestObjective(unittest.TestCase):
    def setUp(self):
        self.frame = DF.derive(thread(("What is working for BodyShield?", ANALYSIS)), "")

    def test_default_inside_a_frame_is_to_exploit_the_finding(self):
        obj, explicit = DF.resolve_objective("So what should we shoot?", self.frame)
        self.assertEqual(obj, DF.EXPLOIT_LEARNING)
        self.assertFalse(explicit, "an inferred objective must not be reported as asked for")

    def test_explicit_objective_wins_and_is_marked_explicit(self):
        obj, explicit = DF.resolve_objective("Optimize for easiest production instead.",
                                            self.frame)
        self.assertEqual(obj, DF.PRACTICALITY)
        self.assertTrue(explicit)

    def test_practicality_is_never_introduced_silently(self):
        """The production answer ranked by 'production practicality' although the
        user never asked to optimise for it."""
        obj, _ = DF.resolve_objective(
            "What should we shoot next week based on the latest data you just shared?",
            self.frame)
        self.assertNotEqual(obj, DF.PRACTICALITY)

    def test_objective_is_inherited_on_later_turns(self):
        f = dict(self.frame)
        f["optimization_goal"] = DF.PRACTICALITY
        obj, explicit = DF.resolve_objective("and what next?", f)
        self.assertEqual(obj, DF.PRACTICALITY)
        self.assertFalse(explicit)


class TestConstrainedRetrieval(unittest.TestCase):
    def setUp(self):
        self.frame = DF.derive(thread(("What is working for BodyShield?", ANALYSIS)), "")

    def test_stays_inside_the_frame_even_when_outside_scores_higher(self):
        """Gloves outscores every BodyShield concept. Picking it is exactly the
        production bug."""
        sel = FR.constrained_ideas(IDEAS, self.frame, DF.EXPLOIT_LEARNING)
        self.assertFalse(sel["broadened"])
        self.assertIn("BodyShield", sel["picked"][0]["PRODUCT"])
        self.assertNotIn("Gloves", sel["picked"][0]["PRODUCT"])

    def test_broadens_only_when_the_frame_is_empty_and_says_so(self):
        empty = DF.new_frame()
        empty["scope"]["product"] = ["Nonexistent Product"]
        sel = FR.constrained_ideas(IDEAS, empty, DF.EXPLOIT_LEARNING)
        self.assertTrue(sel["broadened"])
        text = FR.render_constrained_recommendation(sel, empty, DF.EXPLOIT_LEARNING,
                                                    False, "normal")
        self.assertRegex(text.lower(), r"steps outside|nothing in the")

    def test_objective_changes_the_pick_not_the_scope(self):
        practical = FR.constrained_ideas(IDEAS, self.frame, DF.PRACTICALITY)
        exploit = FR.constrained_ideas(IDEAS, self.frame, DF.EXPLOIT_LEARNING)
        for sel in (practical, exploit):
            self.assertTrue(all("BodyShield" in i["PRODUCT"] for i in sel["picked"]),
                            "changing the objective must not widen the scope")

    def test_answer_names_the_scope_it_stayed_inside(self):
        sel = FR.constrained_ideas(IDEAS, self.frame, DF.EXPLOIT_LEARNING)
        text = FR.render_constrained_recommendation(sel, self.frame,
                                                    DF.EXPLOIT_LEARNING, False, "normal")
        self.assertIn("BodyShield", text)
        self.assertRegex(text, r"(?i)(staying with|off the back of|inside the)")

    def test_answer_flags_the_higher_scoring_option_it_passed_up(self):
        sel = FR.constrained_ideas(IDEAS, self.frame, DF.EXPLOIT_LEARNING)
        text = FR.render_constrained_recommendation(sel, self.frame,
                                                    DF.EXPLOIT_LEARNING, False, "normal")
        self.assertIn("Gloves' Life", text,
                      "should acknowledge the globally stronger idea it declined")

    def test_reasoning_is_comparative_not_a_bare_score(self):
        sel = FR.constrained_ideas(IDEAS, self.frame, DF.EXPLOIT_LEARNING)
        reason = FR.compare_reason(sel["picked"][0], sel["alternative"], self.frame,
                                   DF.EXPLOIT_LEARNING)
        self.assertRegex(reason, r"(?i)(because|closer|clearer|obvious|fresher|close)")
        self.assertNotRegex(reason, r"score \d")


class TestConfidenceSeparation(unittest.TestCase):
    def test_pattern_and_execution_confidence_are_separate(self):
        frame = DF.derive(thread(("What is working for BodyShield?", ANALYSIS)), "")
        conf = FR.confidence_split(IDEAS[0], frame)
        self.assertIn("pattern", conf)
        self.assertIn("execution", conf)
        self.assertNotEqual(conf["pattern"], conf["execution"],
                            "a strong territory does not make one script strong")

    def test_recommendation_is_never_stronger_than_its_weaker_leg(self):
        frame = DF.derive(thread(("What is working for BodyShield?", ANALYSIS)), "")
        conf = FR.confidence_split(IDEAS[0], frame, pattern_strength=EC.PROVEN)
        order = [EC.UNKNOWN, EC.INFERRED, EC.DIRECTIONAL, EC.SUPPORTED, EC.PROVEN]
        self.assertLessEqual(order.index(conf["recommendation"]),
                             order.index(conf["pattern"]))
        self.assertLessEqual(order.index(conf["recommendation"]),
                             order.index(conf["execution"]))

    def test_thin_frame_yields_a_weak_pattern_confidence(self):
        thin = DF.derive(thread(("What works for parents?", PARENTS_ANALYSIS)), "")
        conf = FR.confidence_split(IDEAS[0], thin)
        self.assertIn(conf["pattern"], (EC.INFERRED, EC.DIRECTIONAL))


class TestChallengeAndFalsification(unittest.TestCase):
    def setUp(self):
        self.frame = DF.derive(thread(("What is working for BodyShield?", ANALYSIS)), "")
        self.frame["prior_recommendation"] = "The Cost of Bare Knees"
        self.pack = FR.challenge_pack(self.frame, IDEAS[0], IDEAS[1],
                                      DF.EXPLOIT_LEARNING)

    def test_pack_carries_the_full_contract(self):
        for field in ("current_recommendation", "confidence", "strongest_support",
                      "strongest_counterargument", "alternative",
                      "falsification_conditions"):
            self.assertIn(field, self.pack)
        self.assertTrue(self.pack["falsification_conditions"])
        self.assertEqual(self.pack["current_recommendation"], "The Cost of Bare Knees")

    def test_falsification_conditions_are_concrete(self):
        conds = " ".join(self.pack["falsification_conditions"]).lower()
        self.assertRegex(conds, r"(thin|sample|stronger internal support|test landing "
                                r"flat|refresh|slice)")
        self.assertNotRegex(conds, r"^medium confidence$")

    def test_rendered_challenge_is_not_a_generic_confidence_line(self):
        text = FR.render_challenge(self.pack, "normal")
        low = text.lower()
        self.assertIn("the cost of bare knees", low)
        self.assertRegex(low, r"(more confident in|weaker leg|both halves sit)")
        self.assertRegex(low, r"(change my mind|move me off|reverse this)")
        self.assertRegex(low, r"argument against")
        # the specific failure mode: a bare confidence label and nothing else
        self.assertGreater(len(text.split()), 45)

    def test_challenge_names_the_alternative_once(self):
        text = FR.render_challenge(self.pack, "normal")
        self.assertEqual(text.count("Dive Without The Sting"), 1)

    def test_challenge_kinds_are_distinguished(self):
        self.assertEqual(R.challenge_kind("what would change your mind?"),
                         R.ASK_FALSIFICATION)
        self.assertEqual(R.challenge_kind("how sure are you?"), R.ASK_CONFIDENCE)
        self.assertEqual(R.challenge_kind("what's the argument against this?"),
                         R.ASK_COUNTERARGUMENT)
        self.assertEqual(R.challenge_kind("why did you recommend that?"),
                         R.ASK_WHY_RECOMMENDATION)


class TestClaimLanguage(unittest.TestCase):
    def test_unearned_significance_is_rewritten(self):
        import claim_validator as CV
        r = CV.validate("Reaction shows a significant +19% lift over baseline. [S1]")
        self.assertNotIn("significant", r.text.lower())
        self.assertIn("19%", r.text)
        self.assertIn("significance language without a significance test", r.issues)

    def test_ordinary_use_of_significant_is_left_alone(self):
        import claim_validator as CV
        r = CV.validate("That was a significant investment of shoot time.")
        self.assertIn("significant", r.text.lower())

    def test_aggregate_lift_is_separated_from_example_reels(self):
        import claim_validator as CV
        r = CV.validate("Reaction has a +19% higher Great rate. Proof: [S1]")
        self.assertRegex(r.text.lower(), r"example[s]? of the pattern")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Multi-turn regression through the REAL Slack entry point.
#
# The deterministic floor is used deliberately (no model): if the frame only
# survives because an LLM happened to read the transcript, the mechanism isn't
# fixed. These assert the shared machinery, not phrasing.
# ---------------------------------------------------------------------------
class MultiTurnBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import config
        import gemini_client
        import inspiration_sheets
        import sheets_client
        import test_conversation_stateful as fixtures
        from strategist_benchmark.internal_fixture import FakeSheetsClient

        class FrameIdeaSheets(fixtures.FakeSheets):
            """Seeded ideas that reproduce the production trap: the globally
            highest-scoring concepts are Gloves, while the frame is BodyShield."""

            def read_inspiration_ideas(self):
                return list(IDEAS)

        cls._patched = []
        cls._real = inspiration_sheets.InspirationSheets
        for _n, mod in list(sys.modules.items()):
            if mod is not None and getattr(mod, "InspirationSheets", None) is cls._real:
                setattr(mod, "InspirationSheets", FrameIdeaSheets)
                cls._patched.append(mod)
        inspiration_sheets.InspirationSheets = FrameIdeaSheets
        cls._real_sc = sheets_client.SheetsClient
        cls._patched_sc = []
        for _n, mod in list(sys.modules.items()):
            if mod is not None and getattr(mod, "SheetsClient", None) is cls._real_sc:
                setattr(mod, "SheetsClient", FakeSheetsClient)
                cls._patched_sc.append(mod)
        sheets_client.SheetsClient = FakeSheetsClient
        cls._gem = gemini_client.GeminiClient
        gemini_client.GeminiClient = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("model disabled — frame must survive without it"))
        cls._key = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""

    @classmethod
    def tearDownClass(cls):
        import config
        import gemini_client
        import inspiration_sheets
        import sheets_client
        for mod in cls._patched:
            setattr(mod, "InspirationSheets", cls._real)
        inspiration_sheets.InspirationSheets = cls._real
        for mod in cls._patched_sc:
            setattr(mod, "SheetsClient", cls._real_sc)
        sheets_client.SheetsClient = cls._real_sc
        gemini_client.GeminiClient = cls._gem
        config.GEMINI_API_KEY = cls._key

    def converse(self, *says):
        """Run a real multi-turn thread; return the list of answers."""
        import social_brain
        ctx, out = [], []
        for say in says:
            ans = social_brain.answer_conversation(say, list(ctx))
            out.append(ans)
            ctx += [{"role": "user", "text": say},
                    {"role": "assistant", "text": ans}]
        return out


class TestProductionRegression(MultiTurnBase):
    """The exact three-turn exchange from production."""

    T1 = "What is working for BodyShield right now, and what evidence supports that?"
    T2 = "What should we shoot next week based on the latest data you just shared?"
    T3 = "Are you sure? What would change your mind?"

    def setUp(self):
        self.a1, self.a2, self.a3 = self.converse(self.T1, self.T2, self.T3)

    def test_turn1_establishes_the_bodyshield_frame(self):
        low = self.a1.lower()
        self.assertIn("bodyshield", low)
        frame = DF.derive(thread((self.T1, self.a1)), "")
        self.assertTrue(DF.is_active(frame))
        self.assertTrue(any("bodyshield" in p.lower()
                            for p in frame["scope"]["product"]))

    def test_turn2_inherits_and_does_not_jump_to_gloves(self):
        """THE production bug: a global re-rank recommended two Gloves concepts."""
        low = self.a2.lower()
        self.assertIn("bodyshield", low, "lost the decision frame")
        self.assertNotRegex(
            low.split("*sources:*")[0],
            r"shoot\s+\*?extend your gloves|\*1\.\s*extend your gloves",
            "silently jumped to a global Gloves recommendation")

    def test_turn2_does_not_invent_an_optimization_objective(self):
        body = self.a2.lower().split("*sources:*")[0]
        self.assertNotIn("ranked by production practicality", body)

    def test_turn2_states_its_reading_of_best(self):
        self.assertRegex(self.a2.lower(),
                         r"(staying with|off the back of|inside the|reading \"best\")")

    def test_turn3_challenges_the_turn2_recommendation(self):
        low = self.a3.lower()
        # Assert the SPLIT, not one phrasing of it: which leg is weaker decides
        # the sentence, so three wordings are all correct.
        self.assertRegex(low, r"(more confident in|weaker leg|both halves sit)",
                         "no pattern/execution split")
        self.assertRegex(low, r"(territory|pattern)")
        self.assertRegex(low, r"(execution|expression of it)")
        self.assertRegex(low, r"(change my mind|move me off|reverse this)")
        self.assertRegex(low, r"argument against")
        self.assertIn("bodyshield", low, "lost context on the challenge turn")

    def test_turn3_is_not_a_generic_confidence_template(self):
        low = self.a3.lower()
        self.assertNotRegex(low, r"^confidence is currently (medium|high|low)")
        self.assertGreater(len(self.a3.split()), 45)

    def test_three_answers_are_structurally_different(self):
        self.assertEqual(len({self.a1, self.a2, self.a3}), 3)
        for a in (self.a1, self.a2, self.a3):
            self.assertNotIn("Storelli Marketing Brain. Ask me", a)


class TestFrameScenarios(MultiTurnBase):
    """Scenarios A-E: the frame must work for any subject, not just BodyShield."""

    def test_A_gloves_context_is_inherited(self):
        a1, a2 = self.converse("What is working for gloves?", "So what should we shoot?")
        self.assertIn("glove", a2.lower(), "gloves frame not inherited")

    def test_B_recommendation_follows_the_identified_change(self):
        a1, a2 = self.converse("What changed this week?",
                               "What should we shoot because of that?")
        self.assertTrue(a2.strip())
        self.assertNotIn("Storelli Marketing Brain. Ask me", a2)

    def test_C_parents_scope_and_thin_evidence_caveat_both_survive(self):
        a1, a2 = self.converse("What works for parents?", "What should we make next?")
        low = a2.lower()
        self.assertRegex(low, r"(thin|only \d+|not proven|judgement|test|few|small)",
                         "dropped the evidence-thin caveat when carrying the frame")

    def test_D_third_turn_explicitly_broadens(self):
        a1, a2, a3 = self.converse("What is working for BodyShield?",
                                   "What should we shoot?",
                                   "Actually, strongest idea across all products?")
        self.assertIn("bodyshield", a2.lower(), "first recommendation left the frame")
        self.assertNotEqual(a2, a3, "broadening produced the same constrained answer")

    def test_E_objective_changes_without_changing_the_evidence_universe(self):
        a1, a2, a3 = self.converse("What is working for BodyShield?",
                                   "What should we shoot?",
                                   "Optimize for easiest production instead.")
        self.assertIn("bodyshield", a3.lower(),
                      "changing the objective must not widen the scope")
        self.assertRegex(a3.lower(), r"(practical|easier|feasib|quick|as you asked)")

    def test_frame_works_without_the_model(self):
        """If these only pass with an LLM reading the transcript, the shared
        mechanism isn't fixed."""
        import config
        self.assertEqual(config.GEMINI_API_KEY, "")
