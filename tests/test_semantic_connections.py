"""Tests for the Semantic Connection Layer.

Proves idempotent writes, no connection without internal evidence, external
never proof, evidence-fit separated from inspiration-fit, Slack routing to the
inspiration layer (not idea retrieval), context resolution, BodyShield concept
returning relevant external videos, steal/not-copy + storytelling structure,
valid source ids, and read-only.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import semantic_connections as sc
import idea_retrieval as ir
import calendar_retrieval as cret


def _profile(product="BodyShield GK Leggings", conf="High"):
    return {"PROFILE_ID": "WFP-bs", "ACTIVE": "TRUE", "CONFIDENCE": conf,
            "PROFILE_NAME": f"{product} / Adult Amateur: Curiosity Gap + Demo",
            "PRODUCT": product, "ICP": "Adult Amateur", "HOOK_TAGS": "Curiosity Gap, Fear / Risk",
            "FORMAT_TAGS": "Demo", "PROBLEM_TAGS": "Acute Pain", "SOLUTION_TAGS": "Prevention",
            "FUNNEL_STAGE_TAGS": "Consideration", "INTERNAL_SAMPLE_SIZE": "6",
            "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/;"
                                     "https://www.instagram.com/storellisoccer/reel/BBB/",
            "SUPPORTING_LEARNING_IDS": "signal_hook_fear_risk"}


def _insp(handle, quality="90", caption="turf burn pain and protection demo", **over):
    d = {"SOURCE_ID": f"tiktok:{handle}", "SOURCE_TYPE": "EXTERNAL_INSPIRATION",
         "SAFETY_STATUS": "Safe", "ANALYSIS_STATUS": "Analyzed", "USE_FOR_IDEA_GEN": "TRUE",
         "INSPIRATION_QUALITY_SCORE": quality, "POST_URL": f"https://www.tiktok.com/@{handle}/video/1",
         "CAPTION": caption, "CREATIVE_MECHANISM": "Fear / Risk | Story | acute pain -> prevention",
         "BEST_MATCHED_PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur", "HANDLE": handle}
    d.update(over)
    return d


INSP = [_insp("jasmines_main", "94"), _insp("rezonwear", "92", caption="protection credibility demo"),
        _insp("lstu_kachow", "90", caption="training drill setup rep mistake correction"),
        _insp("gk_grip", "88", caption="wet weather grip glove tutorial",
              BEST_MATCHED_PROFILE_NAME="Gloves / Aspiring Pro")]
IDEAS = [{"IDEA_ID": "IDEA-bs-1", "IDEA_SCORE": "95", "SOURCE_PROFILE_ID": "WFP-bs",
          "REFINED_IDEA_TITLE": "BodyShield GK Leggings: Dive Without The Sting", "PRODUCT": "BodyShield GK Leggings"}]


class TestStructureAnchor(unittest.TestCase):
    def test_structure_for_hook(self):
        self.assertIn("→", sc.structure_for("Curiosity Gap"))
        self.assertIn("Pain Moment", sc.structure_for("Fear / Risk"))
        self.assertEqual(sc.structure_for(""), sc._DEFAULT_STRUCTURE)


class TestConnectionBuild(unittest.TestCase):
    def test_no_connection_without_internal_evidence(self):
        concept = sc._concept_from_profile(_profile(), "Fear / Risk")
        concept["INTERNAL_EVIDENCE_URLS"] = ""      # strip internal proof
        self.assertIsNone(sc.build_connection_row(concept, INSP[:2], "High", gemini=None))

    def test_connection_built_with_evidence_and_externals(self):
        concept = sc._concept_from_profile(_profile(), "Fear / Risk")
        row = sc.build_connection_row(concept, INSP[:2], "High", gemini=None)
        self.assertIsNotNone(row)
        self.assertTrue(row["INTERNAL_EVIDENCE_URLS"])
        self.assertTrue(row["EXTERNAL_REFERENCE_URLS"])
        self.assertIn("→", row["STORYTELLING_STRUCTURE"])
        self.assertTrue(row["WHAT_TO_STEAL"])
        self.assertTrue(row["WHAT_NOT_TO_COPY"])

    def test_evidence_fit_separated_from_inspiration_fit(self):
        concept = sc._concept_from_profile(_profile(), "Fear / Risk")
        hi = sc.build_connection_row(concept, [_insp("a", "100")], "High", gemini=None)
        lo = sc.build_connection_row(concept, [_insp("b", "80")], "High", gemini=None)
        self.assertEqual(hi["EVIDENCE_FIT_SCORE"], lo["EVIDENCE_FIT_SCORE"])   # internal only
        self.assertGreater(hi["INSPIRATION_FIT_SCORE"], lo["INSPIRATION_FIT_SCORE"])  # external drives this

    def test_score_formula(self):
        self.assertEqual(sc.connection_score(100, 100, 100, 100, 100), 100.0)


class FakeSheets:
    def __init__(self, connections=None):
        self._conns = connections or []
        self.upserts = []
        self.other_writes = 0

    def ensure_semantic_connections_tab(self):
        return False

    def read_semantic_connections(self):
        return list(self._conns)

    def read_profiles(self):
        return [_profile()]

    def read_content_rows(self):
        return list(INSP)

    def read_ideas(self):
        return list(IDEAS)

    def upsert_semantic_connections(self, rows):
        self.upserts.append(rows)
        return len(rows), 0

    def append_run(self, run):
        pass

    def update_content_cells_bulk(self, *a, **k):
        self.other_writes += 1

    def upsert_profiles(self, *a, **k):
        self.other_writes += 1


class TestBuildOrchestrator(unittest.TestCase):
    def test_idempotent_and_only_semantic_writes(self):
        sheets = FakeSheets()
        run = sc.build_semantic_connections(sheets=sheets, gemini=None,
                                            products=["BodyShield GK Leggings"], max_concepts=5)
        self.assertEqual(run["RUN_TYPE"], "SemanticConnections")
        self.assertEqual(sheets.other_writes, 0)          # no internal/inspiration writes
        rows = sheets.upserts[0]
        self.assertTrue(rows)
        id1 = rows[0]["CONNECTION_ID"]
        # rerun -> same connection id (idempotent upsert)
        sc.build_semantic_connections(sheets=sheets, gemini=None,
                                      products=["BodyShield GK Leggings"], max_concepts=5)
        self.assertEqual(sheets.upserts[1][0]["CONNECTION_ID"], id1)


class TestSlackRouting(unittest.TestCase):
    def test_inspiration_query_detection(self):
        self.assertTrue(sc.is_inspiration_query("what videos should we take inspiration from for BodyShield?"))
        self.assertTrue(sc.is_inspiration_query("show me inspo for the turf burn concept"))
        self.assertTrue(sc.is_inspiration_query("which videos map to the Dive Without The Sting idea?"))
        self.assertTrue(sc.is_inspiration_query("what storytelling structure should we use for this?"))
        # A plain idea/calendar ask must NOT route here.
        self.assertFalse(sc.is_inspiration_query("give me 5 BodyShield ideas"))
        self.assertFalse(ir.is_idea_query("what videos should we take inspiration from?") and
                         not sc.is_inspiration_query("what videos should we take inspiration from?"))

    def test_context_resolves_related_to_ideas(self):
        ctx = [{"role": "assistant", "text": "Shoot *BodyShield GK Leggings: Dive Without The Sting* first."}]
        self.assertEqual(sc._resolve_family("related to the ideas you proposed, what videos?", ctx), "leggings")


class TestInspirationAnswer(unittest.TestCase):
    def test_bodyshield_returns_external_videos_not_idea_list(self):
        out = sc.answer_inspiration("what videos should we take inspiration from for BodyShield leggings?",
                                    context=[], sheets=FakeSheets(), gemini=None)
        self.assertIsNotNone(out)
        self.assertIn("@jasmines_main", out)              # a specific external creator
        self.assertNotIn("Dive Without The Sting", out)   # NOT the idea list
        self.assertIn("execution reference", out.lower())

    def test_answer_has_steal_notcopy_structure_and_sources(self):
        out = sc.answer_inspiration("show me inspo videos for the turf burn concept",
                                    context=[], sheets=FakeSheets(), gemini=None)
        self.assertIn("Steal:", out)
        self.assertIn("Don't copy:", out)
        self.assertIn("→", out)                           # storytelling structure
        self.assertIn("[S1]", out)                        # internal proof id
        self.assertIn("[E1]", out)                        # external reference id
        self.assertIn("not proof", out.lower())
        self.assertNotRegex(out.lower(), r"views prove|proven by views")

    def test_read_only(self):
        sheets = FakeSheets()
        sc.answer_inspiration("inspiration videos for gloves", context=[], sheets=sheets, gemini=None)
        self.assertEqual(sheets.other_writes, 0)
        self.assertEqual(sheets.upserts, [])              # no writes from Slack

    def test_from_stored_connection(self):
        conn = {"CONNECTION_ID": "SC-1", "CONCEPT_KEY": "CP-x", "CONCEPT_NAME": "BodyShield turf",
                "PRODUCT": "BodyShield GK Leggings", "HOOK_ARCHETYPE": "Fear / Risk",
                "FORMAT_ARCHETYPE": "Demo", "CONNECTION_SCORE": "88",
                "STORYTELLING_STRUCTURE": "Fear/Risk → Story-Demo → Pain Moment → Protected Replay → CTA",
                "WHAT_TO_STEAL": "the wince moment", "WHAT_NOT_TO_COPY": "their caption",
                "STORELLI_ADAPTATION": "wince on dive -> turf mark -> BodyShield replay -> CTA",
                "WINNING_PROFILE_NAME": "BodyShield / Adult Amateur",
                "INTERNAL_EVIDENCE_URLS": "https://ig/AAA/",
                "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1",
                "EXTERNAL_CREATORS": "@jasmines_main"}
        out = sc.answer_inspiration("videos to take inspiration from for BodyShield",
                                    context=[], sheets=FakeSheets([conn]), gemini=None)
        self.assertIn("@jasmines_main", out)
        self.assertIn("the wince moment", out)


if __name__ == "__main__":
    unittest.main()
