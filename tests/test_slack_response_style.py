"""Tests for the global Slack CEO-conversation style layer.

Proves default/concise length caps, source preservation, [S#]/[E#]/[N#] label
survival, external-not-proof, no idea/calendar dumps, no canned endings, Slack
read-only, and that non-idea/non-calendar routing is unchanged.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import slack_response_style as st
import idea_retrieval as ir
import calendar_retrieval as cret


def _wc(text):
    body, _ = st.split_sources(text)
    return len(body.split())


# ---- style module ------------------------------------------------------------
class TestStyleModule(unittest.TestCase):
    def test_mode_detection(self):
        self.assertEqual(st.detect_response_mode("give me ideas"), st.MODE_DEFAULT)
        self.assertEqual(st.detect_response_mode("... be concise"), st.MODE_CONCISE)
        self.assertEqual(st.detect_response_mode("briefly, what works?"), st.MODE_CONCISE)
        self.assertEqual(st.detect_response_mode("go deep on this"), st.MODE_DEEP)

    def test_remove_canned_endings(self):
        t = "Answer line.\nWant me to dig deeper?\n\n*Sources:*\n  [S1] <u|x>"
        out = st.remove_canned_endings(t)
        self.assertNotIn("Want me to", out)
        self.assertIn("[S1]", out)          # sources preserved

    def test_enforce_length_preserves_sources(self):
        body = " ".join(f"word{i}" for i in range(300))
        text = body + "\n\n*Sources:*\n  [S1] <u|x>"
        out = st.enforce_length(text, st.MODE_CONCISE)
        self.assertLessEqual(_wc(out), st.WORD_CAP[st.MODE_CONCISE])
        self.assertIn("[S1]", out)

    def test_compact_sources_labels(self):
        block = st.compact_sources([("S1", "iu", "Storelli internal proof"),
                                    ("E1", "eu", "External inspiration — @x"),
                                    ("N1", "nu", "Notion calendar")])
        self.assertIn("[S1] <iu|", block)
        self.assertIn("[E1] <eu|", block)
        self.assertIn("[N1] <nu|", block)

    def test_no_fake_sources_when_none(self):
        self.assertEqual(st.compact_sources([]), "")


# ---- idea retrieval ----------------------------------------------------------
def _idea(**over):
    d = {"IDEA_ID": "IDEA-gloves-001", "STATUS": "Proposed", "PRODUCT": "Gloves",
         "ICP": "Aspiring Pro", "IDEA_TITLE": "Wet Weather Grip Myth",
         "HOOK": "Think wet weather kills your grip? Here's the fix.",
         "CONCEPT": "Debunk the wet-grip myth with a controlled demo of three checkpoints.",
         "SHOT_LIST": "fumble | grip | save", "CTA": "Shop", "FORMAT": "Tutorial",
         "IDEA_SCORE": "91", "STRATEGIC_PRIORITY_SCORE": "95", "PRODUCT_FIT_SCORE": "100",
         "EXECUTION_CLARITY_SCORE": "95", "NOVELTY_SCORE": "85", "FEASIBILITY_SCORE": "90",
         "COPYRIGHT_SAFETY_SCORE": "100", "EVIDENCE_FIT_SCORE": "82",
         "RECOMMENDED_SHOOT_PRIORITY": "High", "CONFIDENCE": "Medium",
         "SOURCE_PROFILE_NAME": "Gloves / Aspiring Pro",
         "INTERNAL_EVIDENCE_URLS": "https://ig/s1/", "EXTERNAL_REFERENCE_URLS": "https://tiktok/e1"}
    d.update(over)
    return d


MANY = [_idea(IDEA_ID=f"IDEA-g-{i}", IDEA_TITLE=f"Gloves idea {i}", IDEA_SCORE=str(95 - i))
        for i in range(10)]


class TestIdeaStyle(unittest.TestCase):
    def test_default_under_length_and_max_5(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=MANY)
        self.assertLessEqual(_wc(out), st.WORD_CAP[st.MODE_DEFAULT])
        # No more than 5 numbered ideas by default.
        self.assertLessEqual(sum(out.count(f"*{n}. ") for n in range(1, 9)), 5)

    def test_concise_is_shorter(self):
        default = ir.answer_ideas("give me 5 gloves ideas", ideas=MANY)
        concise = ir.answer_ideas("give me 5 gloves ideas, be concise", ideas=MANY)
        self.assertLessEqual(_wc(concise), st.WORD_CAP[st.MODE_CONCISE])
        self.assertLessEqual(_wc(concise), _wc(default))

    def test_no_canned_ending(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=MANY)
        self.assertNotIn("want me to", out.lower())

    def test_sources_and_labels_and_not_proof(self):
        out = ir.answer_ideas("give me gloves ideas", ideas=[_idea()])
        body, src = st.split_sources(out)
        self.assertTrue(src.startswith("*Sources:*"))     # sources at the bottom
        self.assertIn("[S1]", out)
        self.assertIn("[E1]", out)
        self.assertIn("not proof", out.lower())
        self.assertNotRegex(out.lower(), r"views prove|proven by views")

    def test_read_only(self):
        class RO:
            def __init__(self): self.writes = 0
            def read_ideas(self): return MANY
            def append_ideas(self, *a, **k): self.writes += 1
        ro = RO()
        ir.answer_ideas("give me gloves ideas", sheets=ro)
        self.assertEqual(ro.writes, 0)


# ---- calendar retrieval ------------------------------------------------------
def _rating(title, score, rec):
    return {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": title, "CALENDAR_IDEA_SCORE": str(score),
            "RECOMMENDATION": rec, "NOTION_PAGE_URL": "https://app.notion.com/p/x",
            "RATIONALE": "Solid tie to the winning profile and a clear demo beat.",
            "REVISION_SUGGESTION": "sharpen the hook", "INTERNAL_EVIDENCE_FIT_SCORE": "80",
            "INTERNAL_EVIDENCE_URLS": "https://ig/s1/", "EXTERNAL_REFERENCE_URLS": "https://tiktok/e1"}


CAL = ([_rating(f"Keep {i}", 85 - i, "Keep") for i in range(4)]
       + [_rating(f"Revise {i}", 65 - i, "Revise") for i in range(5)]
       + [_rating(f"Reject {i}", 45 - i, "Reject") for i in range(4)])


class ROCalSheets:
    def __init__(self, rows):
        self._r = rows
        self.writes = 0

    def read_calendar_ratings(self):
        return list(self._r)

    def upsert_calendar_ratings(self, *a, **k):
        self.writes += 1


class TestCalendarStyle(unittest.TestCase):
    def test_default_does_not_dump_all(self):
        out = cret.answer_calendar("rate the content calendar ideas", sheets=ROCalSheets(CAL))
        # 13 rated rows exist; default shortlist shows far fewer.
        shown = sum(out.count(t) for t in [r["CALENDAR_TITLE"] for r in CAL])
        self.assertLessEqual(shown, 9)
        self.assertNotIn("Reject 3", out)   # not every row dumped

    def test_worth_shooting_has_sources_and_not_proof(self):
        out = cret.answer_calendar("which calendar ideas are worth shooting?", sheets=ROCalSheets(CAL))
        self.assertIn("Keep 0", out)
        self.assertIn("not proof", out.lower())
        self.assertIn("[S1]", out)

    def test_notion_source_label(self):
        out = cret.answer_calendar("which calendar ideas are worth shooting?", sheets=ROCalSheets(CAL))
        self.assertRegex(out, r"\[N1\] <https://app.notion.com")

    def test_no_ratings_fallback(self):
        self.assertIn("run the calendar rating workflow first",
                      cret.answer_calendar("rate the calendar", sheets=ROCalSheets([])).lower())

    def test_read_only(self):
        ro = ROCalSheets(CAL)
        cret.answer_calendar("which calendar ideas are worth shooting?", sheets=ro)
        cret.answer_calendar("which proposed ideas are weak?", sheets=ro)
        self.assertEqual(ro.writes, 0)


class TestRoutingUnchanged(unittest.TestCase):
    def test_non_idea_non_calendar_not_intercepted(self):
        # These must NOT be captured by the idea/calendar retrieval intercepts.
        self.assertFalse(ir.is_idea_query("what is working for parents?"))
        self.assertFalse(cret.is_calendar_query("what is working for parents?"))
        self.assertFalse(ir.is_idea_query("summarize the brain"))
        self.assertFalse(cret.is_calendar_query("give me 5 gloves ideas"))


if __name__ == "__main__":
    unittest.main()
