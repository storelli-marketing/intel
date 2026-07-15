"""Tests for the Ad-Hoc Notion Idea Evaluation Layer.

Proves Notion URL detection/extraction, clear errors for inaccessible / thin
pages, normalization, RAG retrieval (semantic connections + internal evidence
separated from external inspiration), external-never-proof scoring, guarded
recommendations (vague -> revise / needs more info; strong BodyShield idea
scores higher), idempotent writes to ADHOC_IDEA_EVALUATIONS, Slack routing +
follow-up context resolution, source-id validation, external-as-proof rejection,
and read-only behavior (no Notion writes, no internal-row writes).

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import adhoc_idea_evaluator as ev
import notion_idea_ingest as ni


PAGE_ID = "1a2b3c4d5e6f7890abcdef1234567890"
PAGE_URL = f"https://www.notion.so/BodyShield-Idea-{PAGE_ID}"


def _profile():
    return {"PROFILE_ID": "WFP-bs", "ACTIVE": "TRUE", "CONFIDENCE": "High",
            "PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur: Curiosity Gap + Demo",
            "PRODUCT": "BodyShield GK Leggings", "ICP": "Adult Amateur",
            "HOOK_TAGS": "Curiosity Gap, Fear / Risk", "FORMAT_TAGS": "Demo",
            "PROBLEM_TAGS": "Acute Pain", "SOLUTION_TAGS": "Prevention",
            "INTERNAL_SAMPLE_SIZE": "6",
            "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/;"
                                     "https://www.instagram.com/storellisoccer/reel/BBB/"}


CONN = {"CONNECTION_ID": "SC-1", "CONCEPT_NAME": "BodyShield turf-burn protection",
        "PRODUCT": "BodyShield GK Leggings", "HOOK_ARCHETYPE": "Curiosity Gap",
        "FORMAT_ARCHETYPE": "Demo", "PROBLEM_TYPE": "Acute Pain", "CONNECTION_SCORE": "89",
        "STORYTELLING_STRUCTURE": "Curiosity Gap → Demo → Pain Reveal → Product Protection Reveal → CTA",
        "WHAT_TO_STEAL": "the wince moment", "WHAT_NOT_TO_COPY": "their caption",
        "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
        "EXTERNAL_CONTENT_IDS": "tiktok:jasmines_main;tiktok:groundglorygk",
        "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@jasmines_main/video/1;"
                                   "https://www.tiktok.com/@groundglorygk/video/2"}


def _insp(handle, sid, quality="92"):
    return {"SOURCE_ID": sid, "SAFETY_STATUS": "Safe", "ANALYSIS_STATUS": "Analyzed",
            "USE_FOR_IDEA_GEN": "TRUE", "INSPIRATION_QUALITY_SCORE": quality,
            "POST_URL": f"https://www.tiktok.com/@{handle}/video/1",
            "CAPTION": "turf burn pain and protection demo",
            "CREATIVE_MECHANISM": "Curiosity Gap | Demo | acute pain -> prevention",
            "BEST_MATCHED_PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur"}


INSP = [_insp("jasmines_main", "tiktok:jasmines_main", "92"),
        _insp("groundglorygk", "tiktok:groundglorygk", "90")]
IDEAS = [{"IDEA_ID": "IDEA-bs-1", "IDEA_SCORE": "95", "PRODUCT": "BodyShield GK Leggings",
          "REFINED_IDEA_TITLE": "BodyShield GK Leggings: Dive Without The Sting",
          "CONCEPT": "turf burn wince then protected replay"}]

STRONG_IDEA = {
    "source_type": "notion_page", "page_id": PAGE_ID, "page_url": PAGE_URL,
    "title": "BodyShield GK Leggings: the turf-burn wince after every dive",
    "status": "Idea", "platform": "TikTok", "product": "BodyShield GK Leggings",
    "icp": "Adult Amateur", "format": "Reel", "hook": "Curiosity Gap",
    "concept": "Open on the turf-burn sting after a diving save, then show a protected "
               "replay wearing BodyShield leggings, ending on a clean CTA. Pain moment "
               "first, protection reveal second, over three shootable beats.",
    "caption": "", "script": "", "notes": "", "tags": ["Demo"],
    "raw_text": "turf burn wince protected replay bodyshield demo pain moment"}


def _mock_page(title, body, product_prop=None, status="Idea"):
    props = {"Name": {"type": "title", "title": [{"plain_text": title}]},
             "Status": {"type": "status", "status": {"name": status}}}
    if product_prop:
        props["Product"] = {"type": "select", "select": {"name": product_prop}}
    page = {"id": PAGE_ID, "url": PAGE_URL, "properties": props}
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": body}]}}]
    return page, blocks


class FakeSheets:
    def __init__(self):
        self.adhoc_upserts = []
        self.other_writes = 0

    def ensure_adhoc_evaluations_tab(self):
        return False

    def read_profiles(self):
        return [_profile()]

    def read_semantic_connections(self):
        return [dict(CONN)]

    def read_content_rows(self):
        return [dict(r) for r in INSP]

    def read_ideas(self):
        return [dict(i) for i in IDEAS]

    def read_calendar_ratings(self):
        return []

    def upsert_adhoc_evaluations(self, rows):
        self.adhoc_upserts.append(rows)
        return len(rows), 0

    def upsert_calendar_ratings(self, *a, **k):
        self.other_writes += 1

    def upsert_profiles(self, *a, **k):
        self.other_writes += 1

    def upsert_semantic_connections(self, *a, **k):
        self.other_writes += 1


class FakeGemini:
    def __init__(self, payload):
        self._payload = payload

    def summarize_findings(self, prompt):
        return self._payload


# ---------------------------------------------------------------------------
class TestNotionIngest(unittest.TestCase):
    def test_url_detection_and_extraction(self):
        text = f"evaluate this idea: {PAGE_URL} please"
        self.assertEqual(ni.find_notion_url(text), PAGE_URL)
        self.assertEqual(ni.extract_page_id(PAGE_URL), PAGE_ID)
        dashed = "https://www.notion.so/x-1a2b3c4d-5e6f-7890-abcd-ef1234567890"
        self.assertEqual(ni.extract_page_id(dashed), PAGE_ID)
        self.assertEqual(ni.find_notion_url("no link here"), "")

    def test_inaccessible_page_returns_clear_error(self):
        def boom(_pid):
            raise ni.NotionAccessError("403")
        idea, err = ni.ingest(PAGE_URL, fetcher=boom)
        self.assertIsNone(idea)
        self.assertEqual(err, ni.ACCESS_ERROR)

    def test_thin_page_returns_insufficient_error(self):
        idea, err = ni.ingest(PAGE_URL, fetcher=lambda _p: _mock_page("Hi", ""))
        self.assertIsNone(idea)
        self.assertEqual(err, ni.INSUFFICIENT_ERROR)

    def test_content_normalized_correctly(self):
        page, blocks = _mock_page(
            "BodyShield GK Leggings: turf-burn wince after a dive",
            "Pain moment first, protected replay second, CTA. Three shootable beats.",
            product_prop="BodyShield GK Leggings")
        idea, err = ni.ingest(PAGE_URL, fetcher=lambda _p: (page, blocks))
        self.assertIsNone(err)
        self.assertEqual(idea["source_type"], "notion_page")
        self.assertEqual(idea["page_id"], PAGE_ID)
        self.assertIn("turf-burn", idea["title"])
        self.assertEqual(idea["status"], "Idea")
        self.assertEqual(ev._family(idea["product"]), "leggings")
        self.assertIn("protected replay", idea["raw_text"])

    def test_ingest_is_read_only(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "src",
                               "notion_idea_ingest.py")) as fh:
            src = fh.read()
        for writer in ("httpx.post", "httpx.patch", "httpx.put", "httpx.delete",
                       "pages.update", "pages.create", ".update("):
            self.assertNotIn(writer, src, f"ingest must not write to Notion ({writer})")


class TestEvaluation(unittest.TestCase):
    def _eval(self, idea, gemini=None):
        return ev.evaluate_idea(idea, [_profile()], [dict(CONN)], [dict(r) for r in INSP],
                                [dict(i) for i in IDEAS], [], gemini=gemini)

    def test_retrieves_semantic_connection(self):
        r = self._eval(STRONG_IDEA)
        self.assertEqual(r["CLOSEST_SEMANTIC_CONNECTION_ID"], "SC-1")
        self.assertEqual(r["CLOSEST_WINNING_PROFILE_ID"], "WFP-bs")

    def test_internal_evidence_separate_from_external(self):
        r = self._eval(STRONG_IDEA)
        self.assertTrue(r["INTERNAL_EVIDENCE_URLS"])          # from the winning profile
        self.assertTrue(r["EXTERNAL_REFERENCE_URLS"])         # from inspiration rows
        internal = set(r["INTERNAL_EVIDENCE_URLS"].split(";"))
        external = set(r["EXTERNAL_REFERENCE_URLS"].split(";"))
        self.assertFalse(internal & external)                 # disjoint sources

    def test_external_cannot_create_high_internal_evidence(self):
        # Internal-evidence fit is driven ONLY by internal proof, so adding piles
        # of high-quality external inspiration must not move it — while it clearly
        # raises the (separate) inspiration-alignment score.
        with_ext = self._eval(STRONG_IDEA)
        without_ext = ev.evaluate_idea(STRONG_IDEA, [_profile()], [dict(CONN)], [],
                                       [dict(i) for i in IDEAS], [])
        self.assertEqual(with_ext["INTERNAL_EVIDENCE_FIT_SCORE"],
                         without_ext["INTERNAL_EVIDENCE_FIT_SCORE"])       # external can't lift it
        self.assertGreater(with_ext["INSPIRATION_ALIGNMENT_SCORE"],
                           without_ext["INSPIRATION_ALIGNMENT_SCORE"])     # external drives this only

    def test_strong_idea_scores_higher_than_weak(self):
        strong = self._eval(STRONG_IDEA)
        weak = self._eval(dict(STRONG_IDEA, product="Gloves",
                               title="cool gloves clip", concept="some gloves", hook="",
                               raw_text="gloves", format=""))
        self.assertGreater(strong["IDEA_EVALUATION_SCORE"], weak["IDEA_EVALUATION_SCORE"])
        self.assertEqual(strong["RECOMMENDATION"], "Shoot")

    def test_vague_idea_revises(self):
        vague = dict(STRONG_IDEA, title="BodyShield game changer",
                     concept="dominate", raw_text="dominate", hook="")
        self.assertEqual(self._eval(vague)["RECOMMENDATION"], "Revise then shoot")

    def test_unclear_product_needs_more_info(self):
        noproduct = dict(STRONG_IDEA, product="", title="a fun clip idea",
                         concept="something visual", raw_text="something")
        self.assertEqual(self._eval(noproduct)["RECOMMENDATION"], "Needs more info")

    def test_idempotent_ids(self):
        id1 = self._eval(STRONG_IDEA)["EVALUATION_ID"]
        id2 = self._eval(STRONG_IDEA)["EVALUATION_ID"]
        self.assertEqual(id1, id2)                            # same page + content
        changed = self._eval(dict(STRONG_IDEA, concept="a completely different concept now"))
        self.assertNotEqual(id1, changed["EVALUATION_ID"])   # changed page -> new hash version


class TestLLMSynthesis(unittest.TestCase):
    def _facts_allowed(self):
        facts, allowed, _ = ev._facts(STRONG_IDEA, _profile(), dict(CONN),
                                      [dict(r) for r in INSP], IDEAS[0], None, 88, "Shoot")
        return facts, allowed

    def test_source_ids_validate(self):
        _, allowed = self._facts_allowed()
        good = {"lead": "ok", "source_ids_used": ["N1", "S1", "E1"]}
        bad = {"lead": "ok", "source_ids_used": ["N1", "E9", "Z3"]}
        self.assertTrue(ev._validate_synth(good, allowed)[0])
        self.assertFalse(ev._validate_synth(bad, allowed)[0])

    def test_external_as_proof_rejected(self):
        _, allowed = self._facts_allowed()
        proofy = {"lead": "The external inspiration proves this works for us.",
                  "source_ids_used": ["E1"]}
        ok, reason = ev._validate_synth(proofy, allowed)
        self.assertFalse(ok)
        self.assertEqual(reason, "external as proof")

    def test_llm_output_used_when_valid(self):
        import json
        payload = json.dumps({
            "recommendation": "Shoot", "confidence": "High",
            "lead": "Shoot it — clean map to proven territory.",
            "why": ["Matches the BodyShield pain/protection proof."],
            "what_works": ["Visual hook."], "what_is_weak": ["Slightly broad."],
            "how_to_improve": ["Lead with one wince."],
            "suggested_story_structure": "Curiosity Gap → Demo → Pain Reveal → CTA",
            "videos_to_take_inspo_from": [
                {"source_id": "E1", "why": "same pain beat",
                 "what_to_steal": "the wince", "what_not_to_copy": "the caption"}],
            "my_move": "Rewrite around one wince.", "source_ids_used": ["N1", "S1", "C1", "E1"]})
        r = ev.evaluate_idea(STRONG_IDEA, [_profile()], [dict(CONN)], [dict(x) for x in INSP],
                             [dict(i) for i in IDEAS], [], gemini=FakeGemini(payload))
        self.assertEqual(r["_lead"], "Shoot it — clean map to proven territory.")
        self.assertIn("wince", r["_videos"][0]["steal"])

    def test_invalid_llm_falls_back_deterministic(self):
        import json
        payload = json.dumps({"lead": "external inspiration proves it works",
                              "source_ids_used": ["E1"]})
        r = ev.evaluate_idea(STRONG_IDEA, [_profile()], [dict(CONN)], [dict(x) for x in INSP],
                             [dict(i) for i in IDEAS], [], gemini=FakeGemini(payload))
        self.assertNotIn("proves it works", r["_lead"])       # deterministic lead used


class TestSlackRoutingAndRender(unittest.TestCase):
    def test_evaluate_with_url_routes_to_evaluator(self):
        self.assertTrue(ev.is_evaluation_query(f"evaluate this idea: {PAGE_URL}"))
        self.assertTrue(ev.is_evaluation_query(f"is this worth shooting? {PAGE_URL}"))
        self.assertTrue(ev.is_evaluation_query(PAGE_URL))     # bare paste
        # A plain inspiration ask with NO url must NOT route here.
        self.assertFalse(ev.is_evaluation_query("what videos should we take inspiration from?"))

    def test_followup_why_resolves_last_notion_idea(self):
        ctx = [{"role": "user", "text": f"evaluate this idea {PAGE_URL}"},
               {"role": "assistant", "text": f"Worth testing. Sources: [N1] {PAGE_URL}"}]
        self.assertTrue(ev.is_evaluation_query("why?", ctx))
        self.assertTrue(ev.is_evaluation_query("how do we improve it?", ctx))
        self.assertEqual(ev._prior_eval_url(ctx), PAGE_URL)
        # No prior evaluation in context -> "why?" is not an evaluation query.
        self.assertFalse(ev.is_evaluation_query("why?", []))

    def test_render_has_recommendation_score_sources(self):
        r = ev.evaluate_idea(STRONG_IDEA, [_profile()], [dict(CONN)], [dict(x) for x in INSP],
                             [dict(i) for i in IDEAS], [])
        out = ev.render_evaluation(r, "evaluate this idea")
        self.assertIn("Score:", out)
        self.assertIn("/100", out)
        self.assertIn("[N1]", out)                            # notion source
        self.assertIn("[S1]", out)                            # internal proof
        self.assertIn("execution reference", out.lower())     # external-not-proof disclaimer


class TestReadOnly(unittest.TestCase):
    def test_persist_only_writes_adhoc_tab(self):
        sheets = FakeSheets()
        r = ev.evaluate_idea(STRONG_IDEA, sheets.read_profiles(), sheets.read_semantic_connections(),
                             sheets.read_content_rows(), sheets.read_ideas(), [])
        persist = {k: v for k, v in r.items() if not k.startswith("_")}
        sheets.upsert_adhoc_evaluations([persist])
        self.assertEqual(sheets.other_writes, 0)              # no internal-row writes
        self.assertEqual(len(sheets.adhoc_upserts), 1)
        # transient render-only keys are never persisted
        self.assertFalse(any(k.startswith("_") for k in sheets.adhoc_upserts[0][0]))


if __name__ == "__main__":
    unittest.main()
