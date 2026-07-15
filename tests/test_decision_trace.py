"""Tests for the Slack Decision Trace / Provenance Layer.

Proves the trace schema (≤3-word labels, short values), KPI inference tied to
storytelling structure (always proxy/inferred, never a hard metric), validation
(no invented source ids, external never proof, no unsupported KPI claim), and
that the trace shows up in the real Slack renderers — idea deep-dive, ad-hoc
Notion evaluation, semantic inspiration, comment drivers, idea diagnosis — while
keeping CEO length caps and sources at the bottom, and never calling IDEA_SCORE
a "performance score".

Run: python -m unittest tests.test_decision_trace
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import decision_trace as dt
import slack_response_style as st
import slack_conversation_orchestrator as orch
import adhoc_idea_evaluator as ae
import social_strategy_skills as strat


def _why_bullets(out: str) -> list:
    body = st.split_sources(out)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip().startswith("•")]


def _label_of(bullet: str):
    m = re.search(r"\*([^:*]+):\*", bullet)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
class TestTraceSchema(unittest.TestCase):
    def test_label_max_3_words(self):
        s = dt.step("Internal performance proof point", "x")
        self.assertLessEqual(len(s["label"].split()), 3)
        self.assertTrue(dt.label_ok("Internal proof"))
        self.assertFalse(dt.label_ok("Internal proof of concept"))

    def test_value_is_short(self):
        s = dt.step("Risk", "one two three four five six seven eight nine ten eleven twelve thirteen fourteen")
        self.assertLessEqual(len(s["value"].split()), dt._MAX_VALUE_WORDS + 1)   # + ellipsis token
        self.assertNotIn("\n", s["value"])

    def test_kpi_tied_to_structure_and_always_proxy(self):
        cases = {
            "Fear/Risk → Pain Reveal → CTA": "comment-likelihood",
            "Curiosity Gap → Demo": "retention",
            "before and after gear test": "conversion",
            "Demo → Product Protection Reveal": "saves",
            "Education tutorial mistake correction": "shareability",
            "POV story-demo": "engagement",
        }
        for structure, expect in cases.items():
            v = dt.kpi_value(structure)
            self.assertIn(expect, v)
            self.assertFalse(dt.claims_hard_kpi(v))            # never a hard number
            self.assertTrue(any(t in v.lower() for t in dt._PROXY_TERMS))

    def test_kpi_step_and_bullets(self):
        steps = [dt.step("Internal proof", "BodyShield demos worked", ["S1", "S2"], "internal", "High"),
                 dt.kpi_step("Curiosity Gap → Demo → Pain Reveal")]
        b = dt.bullets(steps)
        self.assertIn("*Internal proof:*", b[0])
        self.assertIn("[S1][S2]", b[0])
        self.assertTrue(b[1].startswith("*KPI bet:*"))

    def test_validate_rejects_invented_and_external_proof(self):
        allowed = {"S1", "C1", "E1", "N1"}
        good = [dt.step("Internal proof", "profile matched", ["S1"], "internal", "High"),
                dt.kpi_step("demo")]
        self.assertTrue(dt.validate_trace(good, allowed)[0])
        bad_id = [dt.step("Internal proof", "x", ["S9"], "internal", "High")]
        self.assertFalse(dt.validate_trace(bad_id, allowed)[0])
        ext_backs_internal = [dt.step("Internal proof", "x", ["E1"], "internal", "High")]
        self.assertFalse(dt.validate_trace(ext_backs_internal, allowed)[0])
        hard = [dt.step("KPI bet", "gets 400 comments", [], "inference", "Thin")]
        self.assertFalse(dt.validate_trace(hard, allowed)[0])

    def test_external_as_proof_detector(self):
        self.assertTrue(dt.external_as_proof("The external inspiration [E1] proves it works"))
        self.assertFalse(dt.external_as_proof("External inspiration is a reference only"))


# ---------------------------------------------------------------------------
_DEEP_IDEA = {
    "IDEA_ID": "IDEA-bs-1", "REFINED_IDEA_TITLE": "BodyShield GK Leggings: Dive Without The Sting",
    "PRODUCT": "BodyShield GK Leggings", "ICP": "Adult Amateur", "CONFIDENCE": "High", "IDEA_SCORE": "94",
    "SOURCE_PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur: Curiosity Gap + Demo",
    "IDEA_TITLE": "BodyShield GK Leggings: Dive Without The Sting",
    "CONCEPT": "turf-burn wince then protected replay",
    "SHOT_LIST": "Open on the sting after a dive | protected replay | CTA",
    "REFINED_CONCEPT": "turf-burn wince then protected replay",
    "REFINED_SHOT_LIST": "Open on the sting after a dive | protected replay | CTA",
    "CTA": "Protect every dive", "STRATEGIC_PRIORITY_SCORE": "88", "EXECUTION_CLARITY_SCORE": "85",
    "FEASIBILITY_SCORE": "84", "EVIDENCE_FIT_SCORE": "90", "NOVELTY_SCORE": "78",
    "RECOMMENDED_SHOOT_PRIORITY": "High",
    "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/;"
                              "https://www.instagram.com/storellisoccer/reel/BBB/",
    "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1",
    "RISK_NOTES": "Keep the pain moment tasteful."}

_EV = {"_lead": "Shoot it — proven territory.", "IDEA_EVALUATION_SCORE": "91", "CONFIDENCE": "High",
       "CLOSEST_WINNING_PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur",
       "CLOSEST_SEMANTIC_CONNECTION_NAME": "BodyShield turf-burn protection",
       "SUGGESTED_STORY_STRUCTURE": "Curiosity Gap → Demo → Pain Reveal → Product Protection Reveal → CTA",
       "WHAT_TO_STEAL": "the wince moment", "RECOMMENDATION": "Shoot", "_my_move": "Film wince then replay.",
       "SOURCE_URL": "https://www.notion.so/x", "IDEA_TITLE": "BodyShield turf-burn wince",
       "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
       "_s_url": ["https://www.instagram.com/storellisoccer/reel/AAA/"],
       "CLOSEST_RATED_IDEA_TITLE": "Dive Without The Sting",
       "_videos": [{"url": "https://www.tiktok.com/@jasmines_main/video/1", "creator": "@jasmines_main",
                    "steal": "the wince moment", "not_copy": "their caption"}]}


def _profile():
    return {"PROFILE_ID": "WFP-bs", "ACTIVE": "TRUE", "CONFIDENCE": "High",
            "PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur: Curiosity Gap + Demo",
            "PRODUCT": "BodyShield GK Leggings", "ICP": "Adult Amateur",
            "HOOK_TAGS": "Curiosity Gap, Fear / Risk", "FORMAT_TAGS": "Demo",
            "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/"}


_CONN = {"CONNECTION_ID": "SC-1", "CONCEPT_NAME": "BodyShield turf-burn protection",
         "PRODUCT": "BodyShield GK Leggings", "CONNECTION_SCORE": "89",
         "STORYTELLING_STRUCTURE": "Curiosity Gap → Demo → Pain Reveal → Product Protection Reveal → CTA",
         "WHAT_TO_STEAL": "the wince moment", "WHAT_NOT_TO_COPY": "their caption",
         "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
         "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1;"
                                    "https://www.tiktok.com/@groundglorygk/video/2",
         "EXTERNAL_CREATORS": "@jasmines_main, @groundglorygk"}


class _Brain:
    def read_profiles(self):
        return [_profile()]

    def read_semantic_connections(self):
        return [dict(_CONN)]

    def read_ideas(self):
        return [dict(_DEEP_IDEA)]

    def read_calendar_ratings(self):
        return []

    def read_adhoc_evaluations(self):
        return []


def _assert_trace_quality(tc, text, allow_deep=False):
    body = st.split_sources(text)[0]
    tc.assertNotIn("performance score", text.lower())     # IDEA_SCORE language rule
    for b in _why_bullets(text):
        lab = _label_of(b)
        if lab:
            tc.assertLessEqual(len(lab.split()), 3, f"label too long: {lab!r}")
    tc.assertLessEqual(len(_why_bullets(text)), 5)
    if re.search(r"\[[SECNI]\d+\]", body):
        tc.assertIn("*Sources:*", text)                   # sources at bottom
    mode = st.MODE_DEEP if allow_deep else st.MODE_DEFAULT
    tc.assertLessEqual(len(body.split()), int(st.WORD_CAP[mode] * 1.25))


class TestTraceInAnswers(unittest.TestCase):
    def test_idea_deep_dive_trace(self):
        det, facts, srcmap, why, move = orch._pack_deep_dive(_DEEP_IDEA, st.MODE_DEFAULT)
        for label in ("Internal proof", "Story match", "Inspo cue", "KPI bet"):
            self.assertIn(label, det)
        self.assertIn("[S1]", det)                         # internal proof cited
        _assert_trace_quality(self, det)

    def test_evaluation_trace_has_n_s_c_e(self):
        out = ae.render_evaluation(_EV, "evaluate this idea")
        for tag in ("[N1]", "[S1]", "[C1]", "[E1]"):
            self.assertIn(tag, out)
        self.assertIn("KPI bet", out)
        self.assertIn("Idea score:", out)
        self.assertIn("execution reference", out.lower())
        _assert_trace_quality(self, out)

    def test_semantic_answer_has_steal_and_kpi(self):
        out = strat.answer("what videos should we watch before shooting the BodyShield idea?",
                           [{"role": "assistant", "text": "BodyShield turf-burn"}],
                           sheets=_Brain(), gemini=None)
        self.assertIn("steal", out.lower())
        self.assertIn("KPI bet", out)
        _assert_trace_quality(self, out)

    def test_comment_drivers_kpi_inferred(self):
        out = strat.answer("what content is most likely to get comments?", [],
                           sheets=_Brain(), gemini=None)
        self.assertIn("comment-likelihood inferred", out.lower())
        self.assertNotRegex(out.lower(), r"\d+\s*comments")
        _assert_trace_quality(self, out)

    def test_idea_diagnosis_weakness_fix_trace(self):
        out = strat.answer("why is this BodyShield idea weak?",
                           [{"role": "assistant", "text": "BodyShield turf-burn"}],
                           sheets=_Brain(), gemini=None)
        self.assertIn("Weakness", out)
        self.assertIn("Fix", out)
        self.assertIn("KPI bet", out)
        _assert_trace_quality(self, out)


if __name__ == "__main__":
    unittest.main()
