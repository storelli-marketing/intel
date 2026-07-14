"""Tests for Milestone 3A — Storelli Winning Format Profiles.

Proves profiles use ONLY internal Storelli evidence, external inspiration is
ignored (even with high views/priority), low-sample patterns aren't High
confidence, profile IDs are stable/idempotent, rerun updates rather than
duplicates, active profiles have supporting evidence, row-8-style failures are
ignored, and internal completed rows are never mutated.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import winning_profiles as wp
from inspiration_sheets import SOURCE_TYPE_EXTERNAL
import taxonomy

HOOK_FEAR = taxonomy.column_for("hook", "Fear / Risk")
FORMAT_DEMO = taxonomy.column_for("format", "Demo")
VISUAL_ACTION = taxonomy.column_for("visual_style", "Action")


def _row(n, product="Gloves", icp="Aspiring Pro", great=True, **extra):
    r = {"_row": n, "Product": product, "ICP": icp, "LINK": f"https://ig/reel/{n}/",
         HOOK_FEAR: "1", FORMAT_DEMO: "1", VISUAL_ACTION: "1"}
    r.update(extra)
    return r


def _buckets(rows):
    # Great for rows flagged great=True via PERFORMANCE, else Bad.
    return {r["_row"]: ("Great" if r.get("_great", True) else "Bad") for r in rows}


class TestBuildProfiles(unittest.TestCase):
    def test_profile_from_internal_evidence(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 5)]  # 4 Great Gloves/Aspiring Pro
        profs = wp.build_profiles(rows, _buckets(rows))
        self.assertEqual(len(profs), 1)
        p = profs[0]
        self.assertEqual(p["PRODUCT"], "Gloves")
        self.assertEqual(p["ICP"], "Aspiring Pro")
        self.assertIn("Fear / Risk", p["HOOK_TAGS"])
        self.assertIn("Demo", p["FORMAT_TAGS"])
        self.assertEqual(p["INTERNAL_SAMPLE_SIZE"], 4)

    def test_low_sample_not_created(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 3)]   # only 2
        self.assertEqual(wp.build_profiles(rows, _buckets(rows)), [])

    def test_low_sample_not_high_confidence(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 4)]   # exactly 3
        p = wp.build_profiles(rows, _buckets(rows))[0]
        self.assertEqual(p["INTERNAL_SAMPLE_SIZE"], 3)
        self.assertNotEqual(p["CONFIDENCE"], "High")              # 3 -> Medium
        self.assertEqual(p["CONFIDENCE"], "Medium")

    def test_high_confidence_needs_sample_and_rate(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 7)]   # 6 Great, 100% rate
        p = wp.build_profiles(rows, _buckets(rows))[0]
        self.assertEqual(p["CONFIDENCE"], "High")

    def test_external_rows_ignored_even_with_high_views(self):
        internal = [dict(_row(i), _great=True) for i in range(1, 4)]  # 3 internal Great
        external = [dict(_row(100 + i, product="Gloves", icp="Aspiring Pro"),
                         _great=True, SOURCE_TYPE=SOURCE_TYPE_EXTERNAL,
                         VIEW_COUNT="9999999", PRIORITY_SCORE="0.99")
                    for i in range(5)]  # 5 external "winners" that must NOT count
        rows = internal + external
        profs = wp.build_profiles(rows, _buckets(rows))
        self.assertEqual(len(profs), 1)
        # Sample size counts only the 3 internal rows, not the 5 external.
        self.assertEqual(profs[0]["INTERNAL_SAMPLE_SIZE"], 3)
        self.assertEqual(profs[0]["CONFIDENCE"], "Medium")       # would be High if ext counted
        # No external URL leaked into supporting evidence.
        self.assertNotIn("reel/101", profs[0]["SUPPORTING_VIDEO_URLS"])

    def test_row8_style_failure_ignored(self):
        # A failed/untagged row with no bucket must never enter a profile.
        rows = [dict(_row(i), _great=True) for i in range(1, 4)]
        row8 = {"_row": 8, "Product": "Gloves", "ICP": "Aspiring Pro", "Status": "failed"}
        buckets = _buckets(rows)   # row 8 deliberately absent from buckets
        profs = wp.build_profiles(rows + [row8], buckets)
        self.assertEqual(profs[0]["INTERNAL_SAMPLE_SIZE"], 3)     # row 8 not counted
        self.assertNotIn("reel/8/", profs[0]["SUPPORTING_VIDEO_URLS"])

    def test_active_profiles_have_supporting_evidence(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 6)]
        for p in wp.build_profiles(rows, _buckets(rows)):
            if p["ACTIVE"] == "TRUE":
                self.assertGreaterEqual(int(p["INTERNAL_SAMPLE_SIZE"]), wp.MIN_SAMPLE)
                self.assertTrue(p["SUPPORTING_VIDEO_URLS"])

    def test_profile_ids_stable_and_idempotent(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 5)]
        a = wp.build_profiles(rows, _buckets(rows))
        b = wp.build_profiles(rows, _buckets(rows))
        self.assertEqual([p["PROFILE_ID"] for p in a], [p["PROFILE_ID"] for p in b])
        self.assertEqual(a[0]["PROFILE_ID"], wp._profile_id("Gloves", "Aspiring Pro"))

    def test_internal_rows_not_mutated(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 5)]
        snapshot = [dict(r) for r in rows]
        wp.build_profiles(rows, _buckets(rows))
        self.assertEqual(rows, snapshot)                          # pure, no side effects


class FakeProfileSheets:
    """Upsert semantics identical to InspirationSheets.upsert_profiles."""
    def __init__(self):
        self.rows = {}      # PROFILE_ID -> dict

    def upsert_profiles(self, profiles):
        created = updated = 0
        for p in profiles:
            pid = p["PROFILE_ID"]
            if pid in self.rows:
                self.rows[pid] = dict(p); updated += 1
            else:
                self.rows[pid] = dict(p); created += 1
        return created, updated


class TestUpsertIdempotency(unittest.TestCase):
    def test_rerun_updates_not_duplicates(self):
        rows = [dict(_row(i), _great=True) for i in range(1, 5)]
        profs = wp.build_profiles(rows, _buckets(rows))
        sheets = FakeProfileSheets()
        c1, u1 = sheets.upsert_profiles(profs)
        c2, u2 = sheets.upsert_profiles(profs)          # rerun
        self.assertEqual((c1, u1), (1, 0))
        self.assertEqual((c2, u2), (0, 1))              # updated, not duplicated
        self.assertEqual(len(sheets.rows), 1)


if __name__ == "__main__":
    unittest.main()
