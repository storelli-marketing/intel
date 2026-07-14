"""Tests for Milestone 4C — creative director refinement layer.

Proves original + source fields are preserved (refinement writes only new
columns), generic language is reduced, refined hooks get more specific, source
fields are untouched, only INSPIRATION_IDEAS is written, and copyright safety is
kept. Slack remains read-only (idea_retrieval unchanged).

Run: python -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import idea_refiner as rf
from idea_refiner import has_generic, refine_ideas, refine_row, scrub_generic
from inspiration_sheets import IDEA_REFINE_COLUMNS


def _idea(**over):
    d = {
        "_row": 2, "IDEA_ID": "IDEA-gloves-001", "STATUS": "Proposed",
        "IDEA_TITLE": "The Game-Changer: Unleash Your Inner Keeper",
        "HOOK": "Dominate the box and become unbreakable",
        "PRODUCT": "Gloves", "ICP": "Aspiring Pro", "FORMAT": "Tutorial",
        "CONCEPT": "A keeper shows how the gloves game changer transforms grip.",
        "SHOT_LIST": "close-up grip | wet-ball catch | save",
        "STORELLI_ADAPTATION": "Use Storelli Gloves.",
        "IDEA_SCORE": "80", "PRODUCT_FIT_SCORE": "100", "EXECUTION_CLARITY_SCORE": "90",
        "NOVELTY_SCORE": "80", "EVIDENCE_FIT_SCORE": "82", "COPYRIGHT_SAFETY_SCORE": "100",
        "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@x/video/1",
        "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
        "SOURCE_PROFILE_NAME": "Gloves / Aspiring Pro: Education + Tutorial",
    }
    d.update(over)
    return d


_LLM_OUT = {
    "refined_title": "The 3-Second Grip Check Every Keeper Skips",
    "refined_hook": "Your grip fails on wet shots because of one setup mistake — here's the fix.",
    "refined_concept": "Show three concrete grip checkpoints a keeper can self-test before a match.",
    "refined_shot_list": ["close-up: hand seam on the ball", "wet-ball catch test", "before/after hold"],
    "creative_director_notes": "Cut the hype; led with a concrete, testable mechanic.",
}


class FakeGemini:
    def __init__(self, out=None):
        self._out = json.dumps(out if out is not None else _LLM_OUT)

    def summarize_findings(self, prompt):
        return self._out


class TestScrub(unittest.TestCase):
    def test_scrub_removes_generic(self):
        s = scrub_generic("The Game-Changer: Unleash Your Inner Keeper and dominate")
        for bad in ("game-changer", "unleash", "inner keeper", "dominate"):
            self.assertNotIn(bad, s.lower())

    def test_has_generic(self):
        self.assertTrue(has_generic("become unbreakable"))
        self.assertFalse(has_generic("the 3-second grip check"))


class TestRefineRow(unittest.TestCase):
    def test_only_refinement_columns_returned(self):
        cells = refine_row(_idea(), FakeGemini())
        self.assertEqual(set(cells), set(IDEA_REFINE_COLUMNS))
        # Never an original or source field.
        for protected in ("IDEA_TITLE", "HOOK", "CONCEPT", "SHOT_LIST",
                          "EXTERNAL_REFERENCE_URLS", "INTERNAL_EVIDENCE_URLS",
                          "SOURCE_PROFILE_NAME", "IDEA_SCORE"):
            self.assertNotIn(protected, cells)

    def test_generic_language_reduced(self):
        cells = refine_row(_idea(), FakeGemini())
        blob = (cells["REFINED_IDEA_TITLE"] + " " + cells["REFINED_HOOK"] + " "
                + cells["REFINED_CONCEPT"]).lower()
        for bad in ("game-changer", "game changer", "unleash", "inner keeper",
                    "unbreakable", "dominate"):
            self.assertNotIn(bad, blob)

    def test_refined_hook_more_specific(self):
        cells = refine_row(_idea(), FakeGemini())
        original_hook = _idea()["HOOK"]
        self.assertNotEqual(cells["REFINED_HOOK"], original_hook)
        self.assertGreater(len(cells["REFINED_HOOK"]), 20)
        self.assertFalse(has_generic(cells["REFINED_HOOK"]))

    def test_weakness_recorded(self):
        cells = refine_row(_idea(), FakeGemini())
        self.assertTrue(cells["ORIGINAL_WEAKNESS"])
        self.assertIn("generic", cells["ORIGINAL_WEAKNESS"].lower())

    def test_fallback_without_model_scrubs(self):
        cells = refine_row(_idea(), None)
        self.assertNotIn("game-changer", cells["REFINED_IDEA_TITLE"].lower())
        self.assertEqual(cells["REFINEMENT_STATUS"], "Auto-cleaned")

    def test_copyright_unsafe_refinement_rejected(self):
        bad = dict(_LLM_OUT, refined_hook="Recreate Messi's Champions League highlights")
        cells = refine_row(_idea(), FakeGemini(bad))
        self.assertEqual(cells["REFINEMENT_STATUS"], "Needs Review")
        self.assertNotIn("messi", cells["REFINED_HOOK"].lower())


class FakeIdeaSheets:
    def __init__(self, ideas):
        self._ideas = ideas
        self.idea_writes = []
        self.other_writes = 0
        self.runs = []

    def ensure_idea_columns(self, cols):
        return []

    def read_ideas(self):
        return list(self._ideas)

    def update_idea_cells_bulk(self, updates):
        self.idea_writes.extend(updates)

    # If refinement ever tried to write content/internal rows, these would fire.
    def update_content_cells_bulk(self, *a, **k):
        self.other_writes += 1

    def upsert_profiles(self, *a, **k):
        self.other_writes += 1

    def append_run(self, run):
        self.runs.append(run)


class TestOrchestrator(unittest.TestCase):
    def test_writes_only_refinement_cols_to_ideas_tab(self):
        sheets = FakeIdeaSheets([_idea(_row=2), _idea(_row=3, IDEA_ID="IDEA-gloves-002")])
        run = refine_ideas(sheets=sheets, gemini=FakeGemini())
        self.assertEqual(run["RUN_TYPE"], "Refine")
        self.assertEqual(len(sheets.idea_writes), 2)
        self.assertEqual(sheets.other_writes, 0)      # no content/profile writes
        for _row, cells in sheets.idea_writes:
            self.assertEqual(set(cells), set(IDEA_REFINE_COLUMNS))
        self.assertEqual(len(sheets.runs), 1)

    def test_source_and_original_fields_never_in_writeback(self):
        sheets = FakeIdeaSheets([_idea()])
        refine_ideas(sheets=sheets, gemini=FakeGemini())
        _row, cells = sheets.idea_writes[0]
        self.assertNotIn("EXTERNAL_REFERENCE_URLS", cells)
        self.assertNotIn("INTERNAL_EVIDENCE_URLS", cells)
        self.assertNotIn("IDEA_TITLE", cells)

    def test_idempotent(self):
        sheets = FakeIdeaSheets([_idea()])
        refine_ideas(sheets=sheets, gemini=FakeGemini())
        a = dict(sheets.idea_writes[-1][1])
        refine_ideas(sheets=sheets, gemini=FakeGemini())
        b = dict(sheets.idea_writes[-1][1])
        self.assertEqual(a, b)


class TestSlackStillReadOnly(unittest.TestCase):
    def test_idea_retrieval_has_no_write_methods_used(self):
        import idea_retrieval as ir

        class RO:
            def __init__(self): self.writes = 0
            def read_ideas(self): return [_idea()]
            def append_ideas(self, *a, **k): self.writes += 1
            def update_idea_cells_bulk(self, *a, **k): self.writes += 1
        ro = RO()
        ir.answer_ideas("give me gloves ideas", sheets=ro)
        self.assertEqual(ro.writes, 0)


if __name__ == "__main__":
    unittest.main()
