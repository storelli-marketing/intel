"""Golden-prompt QA harness for the Slack answer path.

Runs the most important team questions end-to-end through the REAL Slack entry
point (social_brain.answer_conversation) with external APIs mocked offline:
Google Sheets -> an in-memory FakeSheets, Gemini -> disabled (deterministic),
Notion ingest -> a mock page. Locks in routing, context resolution, the write
policy, and CEO answer-quality so future changes can't silently regress them.

Run: python -m unittest tests.test_slack_golden_prompts
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import gemini_client
import inspiration_sheets
import notion_idea_ingest as ni
import slack_response_style as st
import social_brain
import adhoc_idea_evaluator as ae

MOCK_PAGE_ID = "1a2b3c4d5e6f7890abcdef1234567890"
MOCK_URL = f"https://www.notion.so/BodyShield-Turf-Burn-{MOCK_PAGE_ID}"

# --- seed brain -------------------------------------------------------------
_PROFILE = {"PROFILE_ID": "WFP-bs", "ACTIVE": "TRUE", "CONFIDENCE": "High",
            "PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur: Curiosity Gap + Demo",
            "PRODUCT": "BodyShield GK Leggings", "ICP": "Adult Amateur",
            "HOOK_TAGS": "Curiosity Gap, Fear / Risk", "FORMAT_TAGS": "Demo",
            "INTERNAL_SAMPLE_SIZE": "6", "PERFORMANCE_SIGNAL": "Great in 6/9 internal videos",
            "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/"}

_CONN = {"CONNECTION_ID": "SC-1", "CONCEPT_NAME": "BodyShield turf-burn protection",
         "PRODUCT": "BodyShield GK Leggings", "HOOK_ARCHETYPE": "Curiosity Gap",
         "FORMAT_ARCHETYPE": "Demo", "CONNECTION_SCORE": "89",
         "STORYTELLING_STRUCTURE": "Curiosity Gap → Demo → Pain Reveal → Product Protection Reveal → CTA",
         "WHAT_TO_STEAL": "the wince moment", "WHAT_NOT_TO_COPY": "their caption",
         "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
         "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1;"
                                    "https://www.tiktok.com/@groundglorygk/video/2",
         "EXTERNAL_CREATORS": "@jasmines_main, @groundglorygk"}


def _idea(idea_id, title, product, score="90", **over):
    d = {"IDEA_ID": idea_id, "STATUS": "", "IDEA_SCORE": score, "PRODUCT": product,
         "ICP": "Adult Amateur", "CONFIDENCE": "High", "REFINED_IDEA_TITLE": title,
         "IDEA_TITLE": title, "REFINED_CONCEPT": "turf-burn wince then protected replay demo",
         "CONCEPT": "turf-burn wince then protected replay demo",
         "REFINED_SHOT_LIST": "Open on the sting after a dive | protected replay | CTA",
         "CTA": "Protect every dive — shop BodyShield.", "SOURCE_PROFILE_NAME": _PROFILE["PROFILE_NAME"],
         "STRATEGIC_PRIORITY_SCORE": "88", "EXECUTION_CLARITY_SCORE": "85", "FEASIBILITY_SCORE": "84",
         "EVIDENCE_FIT_SCORE": "90", "NOVELTY_SCORE": "78", "RECOMMENDED_SHOOT_PRIORITY": "High",
         "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
         "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1",
         "RISK_NOTES": "Keep the pain moment tasteful.", "ORIGINAL_WEAKNESS": "hook was generic"}
    d.update(over)
    return d


_IDEAS = [_idea("IDEA-bs-1", "BodyShield GK Leggings: Dive Without The Sting", "BodyShield GK Leggings", "94"),
          _idea("IDEA-gk-1", "Gloves: Wet Weather Grip Myth", "Gloves", "88",
                REFINED_CONCEPT="wet grip myth", SOURCE_PROFILE_NAME="Gloves / Aspiring Pro")]

_CAL = [
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "Generic protection montage", "PRODUCT": "Gloves",
     "RECOMMENDATION": "Reject", "CALENDAR_IDEA_SCORE": "41", "NOTION_PAGE_URL": "https://notion.so/a"},
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "BodyShield turf-burn wince", "PRODUCT": "BodyShield GK Leggings",
     "RECOMMENDATION": "Keep", "CALENDAR_IDEA_SCORE": "84", "NOTION_PAGE_URL": "https://notion.so/b"},
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "Glove grip explainer", "PRODUCT": "Gloves",
     "RECOMMENDATION": "Revise", "CALENDAR_IDEA_SCORE": "58", "NOTION_PAGE_URL": "https://notion.so/c"},
    {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": "Slider slide test", "PRODUCT": "Sliders",
     "RECOMMENDATION": "Keep", "CALENDAR_IDEA_SCORE": "68", "NOTION_PAGE_URL": "https://notion.so/d"},
]

_MOCK_IDEA = {"source_type": "notion_page", "page_id": MOCK_PAGE_ID, "page_url": MOCK_URL,
              "title": "BodyShield GK Leggings: turf-burn wince after every dive", "status": "Idea",
              "platform": "TikTok", "product": "BodyShield GK Leggings", "icp": "Adult Amateur",
              "format": "Reel", "hook": "Curiosity Gap",
              "concept": "Pain moment of the turf sting after a dive, then a protected replay wearing "
                         "BodyShield leggings, then a clean CTA over three shootable beats.",
              "caption": "", "script": "", "notes": "", "tags": ["Demo"],
              "raw_text": "turf burn wince protected bodyshield demo"}


class FakeSheets:
    writes = 0                       # class-level: counts any canonical/artifact write

    def read_profiles(self):
        return [dict(_PROFILE)]

    def read_semantic_connections(self):
        return [dict(_CONN)]

    def read_ideas(self):
        return [dict(x) for x in _IDEAS]

    def read_calendar_ratings(self):
        return [dict(x) for x in _CAL]

    def read_content_rows(self):
        return []

    def read_adhoc_evaluations(self):
        return []

    def ensure_adhoc_evaluations_tab(self):
        return False

    def upsert_adhoc_evaluations(self, rows):
        FakeSheets.writes += 1
        return len(rows), 0

    def __getattr__(self, name):
        # Any other read_* -> [], ensure_* -> False, upsert_* -> counts + (0,0).
        if name.startswith("read_"):
            return lambda *a, **k: []
        if name.startswith("ensure_"):
            return lambda *a, **k: False
        if name.startswith("upsert_") or name.startswith("append_") or name.startswith("update_"):
            def _w(*a, **k):
                FakeSheets.writes += 1
                return (0, 0)
            return _w
        raise AttributeError(name)


def _boom(*a, **k):
    raise RuntimeError("Gemini disabled in golden tests")


class GoldenBase(unittest.TestCase):
    def setUp(self):
        FakeSheets.writes = 0
        ae._EVAL_CACHE.clear()
        # Repoint every live reference to the real InspirationSheets -> FakeSheets.
        self._patched = []
        self._real_sheets = inspiration_sheets.InspirationSheets
        for _name, mod in list(sys.modules.items()):
            if mod is not None and getattr(mod, "InspirationSheets", None) is self._real_sheets:
                setattr(mod, "InspirationSheets", FakeSheets)
                self._patched.append((mod, "InspirationSheets", self._real_sheets))
        inspiration_sheets.InspirationSheets = FakeSheets
        # Disable Gemini everywhere -> deterministic paths.
        self._real_gem = gemini_client.GeminiClient
        gemini_client.GeminiClient = _boom
        # Mock Notion ingest (no network).
        self._real_ingest = ni.ingest
        ni.ingest = lambda *a, **k: (dict(_MOCK_IDEA), None)
        # Skip the LLM strategist fallback branch.
        self._real_key = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""

    def tearDown(self):
        for mod, attr, val in self._patched:
            setattr(mod, attr, val)
        inspiration_sheets.InspirationSheets = self._real_sheets
        gemini_client.GeminiClient = self._real_gem
        ni.ingest = self._real_ingest
        config.GEMINI_API_KEY = self._real_key
        ae._EVAL_CACHE.clear()

    # ---- helpers ----
    def run_prompt(self, text, context=None):
        return social_brain.answer_conversation(text, context or [])

    def assertQuality(self, text, out, allow_deep=False):
        body, _src = st.split_sources(out)
        low = out.lower()
        # no raw long URLs in the body (URLs live only in the Sources block)
        self.assertNotIn("http", body, "raw URL leaked into the answer body")
        # sources at the bottom when source tags are cited
        if re.search(r"\[[SECNI]\d+\]", body):
            self.assertIn("*Sources:*", out)
            self.assertLess(out.index("*Why:*") if "*Why:*" in out else 0, out.index("*Sources:*"))
        # external inspiration is never framed as proof
        self.assertNotRegex(low, r"(external|inspiration|reference)[^.]{0,40}prov(e|es|en|ing)")
        # <= 5 main bullets by default
        bullets = [ln for ln in body.splitlines() if ln.strip().startswith("•")]
        self.assertLessEqual(len(bullets), 5, "more than 5 main bullets")
        # no canned endings
        self.assertNotRegex(low, r"want me to|shall i\b|let me know if|would you like me")
        # no giant markdown tables unless explicitly asked
        self.assertNotIn("---|", out)
        self.assertNotIn("|---", out)
        # length within the CEO cap for the detected mode
        mode = st.detect_response_mode(text)
        cap = st.WORD_CAP[st.MODE_DEEP] if allow_deep else st.WORD_CAP.get(mode, st.WORD_CAP[st.MODE_DEFAULT])
        self.assertLessEqual(len(body.split()), int(cap * 1.2), "answer exceeds CEO length cap")


_BODYSHIELD_CTX = [
    {"role": "user", "text": "what should we test?"},
    {"role": "assistant", "text": "Shoot *BodyShield GK Leggings: Dive Without The Sting* first — "
                                  "the turf-burn wince maps to our winning profile."},
]


class TestGoldenPrompts(GoldenBase):
    def test_01_urgent_tests_reasons_not_just_scores(self):
        text = "what are the most urgent ideas we should test and why?"
        out = self.run_prompt(text)
        self.assertIn("My move", out)
        self.assertNotRegex(out, r"---\|")            # not a score dump table
        self.assertTrue(any(w in out for w in ("shoot", "test", "priorit")))
        self.assertQuality(text, out)

    def test_02_followup_resolves_bodyshield_not_gloves(self):
        text = ("you suggested to shoot BodyShield GK Leggings: Dive Without The Sting first — "
                "can u tell me more about it?")
        out = self.run_prompt(text, _BODYSHIELD_CTX)
        self.assertIn("Dive Without The Sting", out)
        self.assertNotIn("Wet Weather Grip", out)     # did NOT switch to Gloves
        self.assertTrue(any(w in out.lower() for w in ("film", "why", "evidence", "proof")))
        self.assertQuality(text, out)

    def test_03_related_videos_routes_to_semantic(self):
        text = "Related to the ideas you proposed, what videos should we take inspiration from?"
        out = self.run_prompt(text, _BODYSHIELD_CTX)
        self.assertIn("@", out)                        # external creators
        self.assertNotIn("Dive Without The Sting", st.split_sources(out)[0])   # not the idea list
        self.assertIn("Steal", out)
        self.assertIn("execution reference", out.lower())
        self.assertQuality(text, out)

    def test_04_evaluate_notion_writes_artifact(self):
        text = f"evaluate this idea: {MOCK_URL}"
        out = self.run_prompt(text)
        self.assertIn("Idea score:", out)
        self.assertIn("/100", out)
        self.assertEqual(FakeSheets.writes, 1)         # exactly the artifact
        self.assertQuality(text, out)

    def test_05_dry_run_does_not_write(self):
        text = f"dry run this idea: {MOCK_URL}"
        out = self.run_prompt(text)
        self.assertIn("Not saved — dry run.", out)
        self.assertEqual(FakeSheets.writes, 0)
        self.assertQuality(text, out)

    def test_06_comment_drivers_is_inference(self):
        text = "what content is most likely to get comments?"
        out = self.run_prompt(text)
        low = out.lower()
        self.assertTrue("don't have hard comment" in low or "no hard comment" in low or "inference" in low)
        self.assertIn("Try", out)
        self.assertNotRegex(low, r"\d+\s*comments")
        self.assertQuality(text, out)

    def test_07_test_hypothesis_win_lose(self):
        text = "what are we trying to learn with the BodyShield turf-burn idea?"
        out = self.run_prompt(text, _BODYSHIELD_CTX)
        self.assertIn("If it wins", out)
        self.assertIn("If it loses", out)
        self.assertQuality(text, out)

    def test_08_turn_that_into_shoot_brief(self):
        text = "turn that into a shoot brief"
        out = self.run_prompt(text, _BODYSHIELD_CTX)
        self.assertIn("Hook", out)
        self.assertIn("CTA", out)
        beats = len([ln for ln in out.splitlines() if re.match(r"\d+\. ", ln.strip())])
        self.assertGreaterEqual(beats, 4)
        self.assertLessEqual(beats, 6)
        self.assertQuality(text, out, allow_deep=True)

    def test_09_calendar_doctor_max_three(self):
        text = "what should we revise in the calendar?"
        out = self.run_prompt(text)
        self.assertTrue(any(w in out for w in ("Revise", "Kill", "Move up")))
        self.assertLessEqual(out.count("•"), 3)
        self.assertIn("Fix", out)
        self.assertQuality(text, out)

    def test_10_content_gap_parents(self):
        text = "where is the evidence thin?"
        out = self.run_prompt(text)
        self.assertIn("Parent", out)
        self.assertQuality(text, out)

    def test_11_help_menu_no_data_dump(self):
        out = self.run_prompt("what can you do?")
        self.assertIn("Strategy / CEO", out)
        self.assertIn("Notion idea evaluator", out)
        self.assertIn("ask one concrete content decision at a time", out)
        self.assertNotIn("http", out)                  # no data dump / URLs
        self.assertEqual(FakeSheets.writes, 0)

    def test_12_route_debug_developer_view(self):
        out = self.run_prompt("route_debug what should we revise in the calendar?")
        self.assertIn("route:", out)
        self.assertIn("social_strategy_skill", out)
        self.assertIn("calendar_doctor", out)

    def test_13_write_policy_only_evaluate_writes(self):
        # A whole session of non-evaluate prompts must not write anything.
        for t in ("what are the most urgent tests and why?",
                  "what content is most likely to get comments?",
                  "what should we revise in the calendar?",
                  "where is the evidence thin?", "what can you do?"):
            self.run_prompt(t, _BODYSHIELD_CTX)
        self.assertEqual(FakeSheets.writes, 0)


if __name__ == "__main__":
    unittest.main()
