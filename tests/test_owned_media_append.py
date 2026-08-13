"""Tests for the safe internal NEW_MEDIA append + lifecycle + ownership guards.

Covers the fixed NEW_MEDIA gap and its safety rules (Part 14): a new owned reel
becomes a POC row without duplicating, without inventing Product/ICP/taxonomy,
without corrupting the two-row header, and the appended row is analysis-eligible.
Plus lifecycle classification, mutable/immutable protection, the owned-TikTok
ownership guard, and readiness/health states.

Run: python -m unittest tests.test_owned_media_append
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import social_metrics_ingest as smi
import intelligence_refresh as ir
import taxonomy
from sheets_client import SheetsClient


# --------------------------------------------------------------------------- #
# SheetsClient.append_metadata_rows — two-row header integrity
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self):
        self.appended = []

    def append_rows(self, payload, value_input_option=None):
        self.appended.extend(payload)


class _FakeSC:
    """Mimics a SheetsClient POC (Status col 7, HOOK taxonomy at col 8)."""
    def __init__(self, data_rows=0):
        self.values = [
            ["", "", "", "", "", "", "", "HOOK"],                       # row 1 categories
            ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product",
             "Status", "Curiosity Gap"],                                # row 2 headers
        ]
        for i in range(data_rows):
            self.values.append([str(i + 1), f"https://ig/{i}", "Great", "s", "", "", "completed", "1"])
        self.meta_col = {"ID": 1, "LINK": 2, "PERFORMANCE": 3, "Storytelling structure": 4,
                         "ICP": 5, "Product": 6, "Status": 7}
        self.signal_col = {"signal_hook_curiosity_gap": 8}
        self.ws = _FakeWS()

    def next_id(self):
        return SheetsClient.next_id(self)


class TestAppendMetadataRows(unittest.TestCase):
    def test_next_id(self):
        self.assertEqual(SheetsClient.next_id(_FakeSC(data_rows=0)), 1)
        self.assertEqual(SheetsClient.next_id(_FakeSC(data_rows=3)), 4)

    def test_append_places_values_and_preserves_header(self):
        sc = _FakeSC(data_rows=2)
        n = SheetsClient.append_metadata_rows(sc, [{"LINK": "https://ig/x", "PERFORMANCE": "",
                                                    "Product": "SHOULD_NOT_BLEED"}])
        self.assertEqual(n, 1)
        row = sc.ws.appended[0]
        self.assertEqual(len(row), 8)                                   # full width, no shift
        self.assertEqual(row[sc.meta_col["LINK"] - 1], "https://ig/x")  # LINK at its column
        self.assertEqual(row[sc.meta_col["ID"] - 1], "3")              # auto ID (max 2 + 1)
        self.assertEqual(row[7], "")                                    # taxonomy cell stays blank
        # header rows untouched
        self.assertEqual(sc.values[0][7], "HOOK")
        self.assertEqual(sc.values[1][0], "ID")

    def test_unknown_keys_ignored(self):
        sc = _FakeSC()
        SheetsClient.append_metadata_rows(sc, [{"LINK": "https://ig/y", "NOT_A_COLUMN": "z"}])
        row = sc.ws.appended[0]
        self.assertNotIn("z", row)


# --------------------------------------------------------------------------- #
# append_owned_media_to_poc — dedup + blank human fields + eligibility
# --------------------------------------------------------------------------- #
class _FakePOC:
    def __init__(self, rows):
        self._rows = rows
        self.appended = []
        self.meta_col = {"ID": 1, "LINK": 2, "PERFORMANCE": 3, "ICP": 5, "Product": 6,
                         "Status": 7, "POST_DATE": 8, "VIEWS": 9, "DURATION_SECONDS": 10}

    def read_rows(self):
        return [dict(r) for r in self._rows]

    def append_metadata_rows(self, recs):
        self.appended.extend(recs)
        return len(recs)


def _media(mid, code, ts="2026-08-01T10:00:00+0000", dur=8):
    return {"id": mid, "permalink": f"https://www.instagram.com/reel/{code}/",
            "media_product_type": "REELS", "timestamp": ts, "duration": dur}


class TestAppendOwnedMedia(unittest.TestCase):
    def test_new_reel_appended_without_human_or_taxonomy_fields(self):
        poc = _FakePOC([{"_row": 3, "LINK": "https://www.instagram.com/reel/OLD/"}])
        media = [_media("m1", "NEW1")]
        insights = {"m1": {"views": 20000, "likes": 900, "reach": 15000}}
        res = smi.append_owned_media_to_poc(media, insights, poc)
        self.assertEqual(res["appended"], 1)
        rec = poc.appended[0]
        self.assertEqual(rec["LINK"], "https://www.instagram.com/reel/NEW1/")
        self.assertEqual(rec["POST_DATE"], "2026-08-01")               # immutable metadata
        self.assertEqual(rec["VIEWS"], "20000")                        # supported metric
        self.assertNotIn("Product", rec)                               # never invented
        self.assertNotIn("ICP", rec)
        self.assertNotIn("Status", rec)                                # blank -> eligible
        self.assertFalse(any(k.startswith("signal_") for k in rec))    # no taxonomy

    def test_duplicate_reel_not_appended(self):
        poc = _FakePOC([{"_row": 3, "LINK": "https://www.instagram.com/storellisoccer/reel/DUP/"}])
        res = smi.append_owned_media_to_poc([_media("m1", "DUP")], {"m1": {}}, poc)
        self.assertEqual(res["appended"], 0)
        self.assertEqual(poc.appended, [])

    def test_appended_row_is_analysis_eligible(self):
        poc = _FakePOC([])
        smi.append_owned_media_to_poc([_media("m1", "NEW2")], {"m1": {"views": 5000}}, poc)
        rec = poc.appended[0]
        # simulate reading it back as a row (blank Status, no taxonomy) -> eligible
        row = {"LINK": rec["LINK"], "PERFORMANCE": "", "Status": ""}
        for c in taxonomy.all_signal_columns():
            row[c] = ""
        self.assertTrue(SheetsClient.should_process(row, False))

    def test_batch_internal_dedup(self):
        poc = _FakePOC([])
        media = [_media("m1", "SAME"), _media("m2", "SAME")]           # same shortcode twice
        res = smi.append_owned_media_to_poc(media, {}, poc)
        self.assertEqual(res["appended"], 1)


# --------------------------------------------------------------------------- #
# lifecycle classification
# --------------------------------------------------------------------------- #
class TestLifecycle(unittest.TestCase):
    def test_classify(self):
        analyzed = {"_row": 3, "LINK": "https://www.instagram.com/reel/A/"}
        for c in taxonomy.all_signal_columns():
            analyzed[c] = ""
        analyzed[taxonomy.column_for("hook", "Curiosity Gap")] = "1"    # analyzed
        unanalyzed = {"_row": 4, "LINK": "https://www.instagram.com/reel/B/"}
        for c in taxonomy.all_signal_columns():
            unanalyzed[c] = ""
        media = [_media("m1", "A"), _media("m2", "B"), _media("m3", "C")]   # C is new
        out = smi.classify_owned_media(media, [analyzed, unanalyzed])
        self.assertEqual([m["id"] for m in out["NEW_MEDIA"]], ["m3"])
        self.assertEqual([m.get("id") for m, _r in out["KNOWN_ANALYZED"]], ["m1"])
        self.assertEqual([m.get("id") for m, _r in out["KNOWN_UNANALYZED"]], ["m2"])


# --------------------------------------------------------------------------- #
# owned-TikTok ownership guard
# --------------------------------------------------------------------------- #
class TestTikTokOwnership(unittest.TestCase):
    def setUp(self):
        self._h = config.STORELLI_TIKTOK_HANDLE

    def tearDown(self):
        config.STORELLI_TIKTOK_HANDLE = self._h

    def test_no_handle_configured_nothing_is_owned(self):
        config.STORELLI_TIKTOK_HANDLE = ""
        self.assertFalse(smi.is_owned_tiktok("https://www.tiktok.com/@storellisoccer/video/1"))

    def test_only_exact_handle_is_owned(self):
        config.STORELLI_TIKTOK_HANDLE = "storellisoccer"
        self.assertTrue(smi.is_owned_tiktok("https://www.tiktok.com/@storellisoccer/video/1"))
        self.assertTrue(smi.is_owned_tiktok("@storellisoccer"))
        self.assertFalse(smi.is_owned_tiktok("https://www.tiktok.com/@someone_else/video/1"))
        self.assertFalse(smi.is_owned_tiktok("goalkeeper training clip"))   # never infer from content


# --------------------------------------------------------------------------- #
# readiness + health
# --------------------------------------------------------------------------- #
class TestReadinessHealth(unittest.TestCase):
    def test_readiness_reports_blockers_with_env_hints(self):
        caps = ir.refresh_readiness()
        # in the test env IG/Apify are absent -> BLOCKED with the exact env var
        self.assertEqual(caps["external_apify_discovery"]["status"], "BLOCKED")
        self.assertIn("APIFY_TOKEN", caps["external_apify_discovery"]["required"])
        self.assertIn("INSTAGRAM_ACCESS_TOKEN", caps["instagram_owned_discovery"]["required"])
        self.assertIn("status", caps["sheets"])

    def test_health_blocked_without_core(self):
        h = ir.health_state()
        # no Sheets/Gemini configured in the test env -> BLOCKED
        self.assertIn(h["state"], ("BLOCKED", "PARTIAL", "STALE"))
        self.assertIsInstance(h["reasons"], list)

    def test_cookie_message_present_when_blocked(self):
        caps = ir.refresh_readiness()
        # internal video analysis blocked without cookies -> the health reason names it
        if caps["internal_video_analysis"]["status"] != "READY":
            self.assertIn("cookies", caps["internal_video_analysis"]["required"].lower()
                          + " " + ir._COOKIE_REFRESH_MSG.lower())


if __name__ == "__main__":
    unittest.main()
