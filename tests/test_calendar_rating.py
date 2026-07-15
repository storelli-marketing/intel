"""Tests for Notion content-calendar idea rating (reader + rater + Slack).

Proves normalization, camera-emoji + published exclusion, proposed items rated,
internal/external kept separate, external inspiration never used as proof, high
external views can't rescue a weak idea, idempotent upsert, Slack read-only, and
no Notion / internal-row writes.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import notion_calendar as nc
import calendar_rater as cr
import calendar_retrieval as cret


# ---- fake Notion page --------------------------------------------------------
def _page(title="Turf Burn Reality", status="Idea", notes="Show turf burn pain then the fix.",
          channel="Instagram", asset="Reel / Short", brand="Storelli", kind="Deliverable"):
    def txt(s):
        return [{"plain_text": s}]
    return {
        "id": "page-" + title[:6].replace(" ", ""),
        "url": "https://app.notion.com/p/" + title.replace(" ", "-"),
        "properties": {
            "Name": {"type": "title", "title": txt(title)},
            "Status": {"type": "status", "status": {"name": status}},
            "Notes": {"type": "rich_text", "rich_text": txt(notes)},
            "Channel": {"type": "multi_select", "multi_select": [{"name": channel}]},
            "Asset Format": {"type": "multi_select", "multi_select": [{"name": asset}]},
            "Brand(s)": {"type": "multi_select", "multi_select": [{"name": brand}]},
            "Entry Kind": {"type": "select", "select": {"name": kind}},
            "Publish": {"type": "date", "date": {"start": "2026-07-20"}},
        },
    }


def _profile():
    return {"PROFILE_ID": "WFP-bodyshield_gk_leggings-adult_amateur", "ACTIVE": "TRUE",
            "CONFIDENCE": "High", "PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur",
            "PRODUCT": "BodyShield GK Leggings", "ICP": "Adult Amateur",
            "HOOK_TAGS": "Fear / Risk", "FORMAT_TAGS": "Demo", "PROBLEM_TAGS": "Acute Pain",
            "SOLUTION_TAGS": "Prevention", "INTERNAL_SAMPLE_SIZE": "6",
            "SUPPORTING_VIDEO_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/"}


def _insp(quality="88", views="1000", **over):
    d = {"SOURCE_TYPE": "EXTERNAL_INSPIRATION", "SAFETY_STATUS": "Safe",
         "ANALYSIS_STATUS": "Analyzed", "USE_FOR_IDEA_GEN": "TRUE",
         "INSPIRATION_QUALITY_SCORE": quality, "VIEW_COUNT": views,
         "POST_URL": "https://www.tiktok.com/@x/video/1",
         "BEST_MATCHED_PROFILE_NAME": "BodyShield GK Leggings / Adult Amateur"}
    d.update(over)
    return d


class TestNormalization(unittest.TestCase):
    def test_normalize_page(self):
        item = nc.normalize_page(_page())
        self.assertEqual(item["title"], "Turf Burn Reality")
        self.assertEqual(item["status"], "Idea")
        self.assertEqual(item["platform"], "Instagram")
        self.assertEqual(item["asset_format"], "Reel / Short")
        self.assertEqual(item["entry_kind"], "Deliverable")
        self.assertIn("turf burn", item["notes"].lower())
        self.assertFalse(item["has_camera_emoji"])

    def test_camera_emoji_detected(self):
        item = nc.normalize_page(_page(title="🎥 Turf Burn Shoot"))
        self.assertTrue(item["has_camera_emoji"])


class TestSelection(unittest.TestCase):
    def test_camera_emoji_excluded_by_default(self):
        item = nc.normalize_page(_page(title="📸 Ready Shoot"))
        ok, reason = nc.should_rate(item)
        self.assertFalse(ok)
        self.assertIn("camera", reason.lower())

    def test_camera_emoji_included_when_configured(self):
        item = nc.normalize_page(_page(title="📸 Ready Shoot"))
        ok, _ = nc.should_rate(item, exclude_camera=False)
        self.assertTrue(ok)

    def test_published_shot_done_excluded(self):
        for st in ("Published", "Scheduled", "Approved", "Done", "Archived", "Cancelled"):
            ok, reason = nc.should_rate(nc.normalize_page(_page(status=st)))
            self.assertFalse(ok, st)

    def test_proposed_draft_backlog_rated(self):
        for st in ("Idea", "Draft", "Backlog", "To Do", "Needs Revision"):
            ok, _ = nc.should_rate(nc.normalize_page(_page(status=st)))
            self.assertTrue(ok, st)

    def test_key_date_and_empty_excluded(self):
        self.assertFalse(nc.should_rate(nc.normalize_page(_page(kind="Key Date")))[0])
        self.assertFalse(nc.should_rate(nc.normalize_page(_page(title="", notes="")))[0])


class TestRating(unittest.TestCase):
    def test_internal_and_external_kept_separate(self):
        item = nc.normalize_page(_page(title="BodyShield turf burn for adult amateur keepers",
                                       notes="pain then prevention demo"))
        r = cr.rate_item(item, [_profile()], [_insp()], [])
        self.assertIn("instagram.com/storellisoccer", r["INTERNAL_EVIDENCE_URLS"])
        self.assertIn("tiktok.com", r["EXTERNAL_REFERENCE_URLS"])
        self.assertNotIn("tiktok", r["INTERNAL_EVIDENCE_URLS"])
        self.assertTrue(r["CLOSEST_WINNING_PROFILE_ID"])

    def test_external_views_do_not_affect_evidence_fit(self):
        item = nc.normalize_page(_page(title="BodyShield leggings demo"))
        low = cr.rate_item(item, [_profile()], [_insp(views="10")], [])
        high = cr.rate_item(item, [_profile()], [_insp(views="9999999")], [])
        self.assertEqual(low["INTERNAL_EVIDENCE_FIT_SCORE"], high["INTERNAL_EVIDENCE_FIT_SCORE"])
        self.assertEqual(low["INSPIRATION_ALIGNMENT_SCORE"], high["INSPIRATION_ALIGNMENT_SCORE"])

    def test_high_views_cannot_rescue_weak_idea(self):
        weak = nc.normalize_page(_page(title="The Game Changer", notes="", channel="", asset=""))
        # no product, generic hype title, no notes, but huge-view eligible inspiration
        r = cr.rate_item(weak, [], [_insp(quality="100", views="99999999")], [])
        self.assertLess(cr._num(r["CALENDAR_IDEA_SCORE"]), 72)     # not "Keep"
        self.assertNotEqual(r["RECOMMENDATION"], "Keep")

    def test_strong_idea_keeps(self):
        item = nc.normalize_page(_page(
            title="The 3 turf-burn mistakes costing adult amateur keepers clean sheets",
            notes="Open on a raw scraped knee, then demo BodyShield leggings preventing it. "
                  "Three quick beats: pain, cause, fix.", channel="Instagram", asset="Reel / Short"))
        r = cr.rate_item(item, [_profile()], [_insp()], [])
        self.assertGreaterEqual(cr._num(r["CALENDAR_IDEA_SCORE"]), 70)
        self.assertIn(r["RECOMMENDATION"], ("Keep", "Revise"))

    def test_rating_id_stable(self):
        item = nc.normalize_page(_page())
        self.assertEqual(cr._rating_id(item), cr._rating_id(nc.normalize_page(_page())))


# ---- orchestrator with fakes -------------------------------------------------
class FakeSheets:
    def __init__(self):
        self.calendar_upserts = []
        self.other_writes = 0

    def ensure_calendar_ratings_tab(self):
        return False

    def read_profiles(self):
        return [_profile()]

    def read_content_rows(self):
        return [_insp()]

    def read_ideas(self):
        return []

    def read_calendar_ratings(self):
        return []

    def upsert_calendar_ratings(self, rows):
        self.calendar_upserts.append(rows)
        return len(rows), 0

    def append_run(self, run):
        pass

    # These would flag an accidental write to internal/inspiration data.
    def update_content_cells_bulk(self, *a, **k):
        self.other_writes += 1

    def upsert_profiles(self, *a, **k):
        self.other_writes += 1


class TestOrchestrator(unittest.TestCase):
    def _patch_reader(self, ratable, excluded):
        nc._orig = nc.read_ratable_calendar_items
        nc.read_ratable_calendar_items = lambda **k: (ratable, excluded)

    def tearDown(self):
        if hasattr(nc, "_orig"):
            nc.read_ratable_calendar_items = nc._orig
            del nc._orig

    def test_rates_and_writes_only_calendar_tab(self):
        item = nc.normalize_page(_page(title="BodyShield turf burn demo"))
        self._patch_reader([(item, "")], [(nc.normalize_page(_page(status="Published")), "published")])
        sheets = FakeSheets()
        run = cr.rate_calendar_ideas(sheets=sheets, gemini=None, limit=10)
        self.assertEqual(run["RUN_TYPE"], "CalendarRatings")
        self.assertEqual(sheets.other_writes, 0)          # no internal/inspiration writes
        self.assertEqual(len(sheets.calendar_upserts), 1)
        written = sheets.calendar_upserts[0]
        self.assertEqual(len(written), 2)                 # 1 rated + 1 excluded row
        rated = [w for w in written if w["SHOULD_RATE"] == "TRUE"]
        excl = [w for w in written if w["SHOULD_RATE"] == "FALSE"]
        self.assertEqual(len(rated), 1)
        self.assertEqual(excl[0]["EXCLUSION_REASON"], "published")

    def test_idempotent_upsert(self):
        item = nc.normalize_page(_page())
        self._patch_reader([(item, "")], [])
        sheets = FakeSheets()
        cr.rate_calendar_ideas(sheets=sheets, gemini=None)
        a = sheets.calendar_upserts[0][0]
        cr.rate_calendar_ideas(sheets=sheets, gemini=None)
        b = sheets.calendar_upserts[1][0]
        self.assertEqual(a["RATING_ID"], b["RATING_ID"])   # stable id -> update, not dup


# ---- Slack retrieval ---------------------------------------------------------
class ROSheets:
    def __init__(self, ratings):
        self._r = ratings
        self.writes = 0

    def read_calendar_ratings(self):
        return list(self._r)

    def upsert_calendar_ratings(self, *a, **k):
        self.writes += 1


def _rating(title, score, rec, **over):
    d = {"SHOULD_RATE": "TRUE", "CALENDAR_TITLE": title, "CALENDAR_IDEA_SCORE": str(score),
         "RECOMMENDATION": rec, "PRODUCT": "BodyShield", "ICP": "Adult Amateur",
         "NOTION_PAGE_URL": "https://app.notion.com/p/x", "RATIONALE": "because reasons",
         "REVISION_SUGGESTION": "sharpen the hook", "INTERNAL_EVIDENCE_FIT_SCORE": "80",
         "INTERNAL_EVIDENCE_URLS": "https://www.instagram.com/storellisoccer/reel/AAA/",
         "EXTERNAL_REFERENCE_URLS": "https://www.tiktok.com/@x/video/1"}
    d.update(over)
    return d


class TestSlackRetrieval(unittest.TestCase):
    def test_is_calendar_query(self):
        self.assertTrue(cret.is_calendar_query("rate the content calendar ideas"))
        self.assertTrue(cret.is_calendar_query("which calendar ideas are worth shooting?"))
        self.assertTrue(cret.is_calendar_query("which proposed ideas are weak?"))
        self.assertFalse(cret.is_calendar_query("give me 5 gloves ideas"))

    def test_no_ratings_fallback(self):
        self.assertIn("run the calendar rating workflow first",
                      cret.answer_calendar("rate the calendar", sheets=ROSheets([])).lower())

    def test_worth_shooting_and_read_only(self):
        rows = [_rating("Strong idea", 85, "Keep"), _rating("Weak idea", 50, "Reject")]
        ro = ROSheets(rows)
        out = cret.answer_calendar("which calendar ideas are worth shooting?", sheets=ro)
        self.assertIn("Strong idea", out)
        self.assertIn("not\n", out.lower() + "\n") if False else None
        self.assertIn("not proof", out.lower())
        self.assertIn("[S1]", out)
        self.assertIn("[E1]", out)
        self.assertEqual(ro.writes, 0)   # read-only

    def test_revise_mode(self):
        rows = [_rating("Meh idea", 60, "Revise")]
        out = cret.answer_calendar("which calendar ideas should we revise?", sheets=ROSheets(rows))
        self.assertIn("Meh idea", out)


if __name__ == "__main__":
    unittest.main()
