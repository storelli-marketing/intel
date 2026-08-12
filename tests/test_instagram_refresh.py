"""Tests for the operational Instagram refresh (verify + incremental + policy).

Proves:
  * missing / invalid credentials -> clear status, never a crash, token never shown;
  * a valid connection resolves the account + lists available/unavailable metrics;
  * insufficient permissions surface as insights_access=False (not a fake success);
  * incremental refresh recognizes already-synced media and fetches new ones;
  * the mutable-metric policy: immutable fill-once, cumulative update ONLY on an
    API-owned cell, and a manual edit is never overwritten;
  * apply writes only metric columns (never taxonomy / Product / ICP / Status);
  * account demographics never fill per-post columns; secrets are never logged.

Run: python -m unittest tests.test_instagram_refresh
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import social_metrics_ingest as smi
import social_analytics as sa
import taxonomy

_L1 = "https://www.instagram.com/storellisoccer/reel/DVp_bFuDirS/"
_META = {"LINK": 2, "PERFORMANCE": 3, "Status": 7, "VIEWS": 8, "REACH": 9, "LIKES": 10,
         "COMMENTS": 11, "SAVES": 12, "SHARES": 13, "ENGAGEMENT_RATE": 14, "POST_DATE": 15,
         "DURATION_SECONDS": 16}
_INV = {v: k for k, v in _META.items()}


def _poc(n, link, **extra):
    r = {"_row": n, "LINK": link, "PERFORMANCE": "Great", "ICP": "", "Product": "",
         "Status": "completed", "Storytelling structure": ""}
    for c in taxonomy.all_signal_columns():
        r[c] = ""
    r.update(extra)
    return r


class FakeClient:
    def __init__(self, account=True, media=True, insights=True, likes=900,
                 account_raises=False, insights_raises=False):
        self._account, self._media, self._insights = account, media, insights
        self._likes, self._account_raises = likes, account_raises
        self._insights_raises = insights_raises

    def fetch_account(self, fields=""):
        if self._account_raises:
            raise RuntimeError("(#190) invalid OAuth access token")
        return {"id": "179", "username": "storellisoccer", "followers_count": 170000,
                "media_count": 196}

    def token_health(self):
        return {"known": True, "is_valid": True, "expires_at": 0, "scopes": ["instagram_basic"]}

    def fetch_media(self, max_items=500):
        if not self._media:
            return []
        return [{"id": "m1", "permalink": _L1, "media_product_type": "REELS",
                 "timestamp": "2026-08-01T10:00:00+0000", "duration": 8}]

    def fetch_media_insights(self, mid, mpt=""):
        if self._insights_raises:
            raise RuntimeError("no perm")
        if not self._insights:
            return {}
        return {"views": 20000, "reach": 15000, "likes": self._likes, "comments": 40,
                "saved": 120, "shares": 30, "total_interactions": 1090}

    def fetch_account_demographics(self):
        return {}


class FakeSheets:
    """Read/write-capturing SheetsClient stand-in (with a ws + spreadsheet)."""

    def __init__(self, rows):
        self._rows = {r["_row"]: dict(r) for r in rows}
        self.meta_col = _META
        self.written = {}          # (row,col) -> value
        outer = self

        class WS:
            def batch_update(self, updates, value_input_option=None):
                import gspread
                for u in updates:
                    row, col = gspread.utils.a1_to_rowcol(u["range"].split(":")[0])
                    name = _INV.get(col)
                    if name and row in outer._rows:
                        outer._rows[row][name] = u["values"][0][0]
                        outer.written[(row, name)] = u["values"][0][0]
            spreadsheet = None
        self.ws = WS()

    def read_rows(self):
        return [dict(r) for r in self._rows.values()]


# --------------------------------------------------------------------------- #
# verify_connection
# --------------------------------------------------------------------------- #
class TestVerify(unittest.TestCase):
    def test_missing_credentials(self):
        v = smi.verify_connection()          # no client, no creds in test env
        self.assertFalse(v["connected"])
        self.assertFalse(v["configured"])
        out = smi.render_connection_report(v)
        self.assertIn("Connected: NO", out)
        self.assertIn("access token never printed", out)

    def test_valid_connection_lists_metrics(self):
        v = smi.verify_connection(client=FakeClient())
        self.assertTrue(v["connected"])
        self.assertEqual(v["account"]["username"], "storellisoccer")
        self.assertTrue(v["media_access"])
        self.assertTrue(v["insights_access"])
        self.assertIn("likes", v["available_metrics"])
        self.assertIn("impressions", v["unavailable_metrics"])

    def test_invalid_token_is_blocker(self):
        v = smi.verify_connection(client=FakeClient(account_raises=True))
        self.assertFalse(v["connected"])
        self.assertIn("account", v["blocker"].lower())

    def test_insufficient_permissions_insights_false(self):
        v = smi.verify_connection(client=FakeClient(insights_raises=True))
        self.assertTrue(v["media_access"])       # account+media ok
        self.assertFalse(v["insights_access"])   # insights not accessible -> honest False

    def test_token_never_appears_in_report(self):
        v = smi.verify_connection(client=FakeClient())
        out = smi.render_connection_report(v)
        self.assertNotIn("access_token", out.lower())
        self.assertNotIn("token=", out.lower())


# --------------------------------------------------------------------------- #
# mutable-metric policy (pure plan)
# --------------------------------------------------------------------------- #
class TestPolicy(unittest.TestCase):
    def _plan(self, rows, sync_state, likes=1000):
        media = [{"id": "m1", "permalink": _L1, "media_product_type": "REELS",
                  "timestamp": "2026-08-01T10:00:00+0000", "duration": 8}]
        mp = smi.map_media_to_poc_rows(media, rows)
        ins = {"m1": {"views": 20000, "reach": 15000, "likes": likes, "comments": 40,
                      "saved": 120, "shares": 30, "total_interactions": 1090}}
        return smi.plan_incremental(mp, ins, list(_META), sync_state)

    def test_first_fill_all_empty(self):
        plan = self._plan([_poc(3, _L1)], {})
        self.assertEqual(plan["counts"]["first_fill"], 9)
        self.assertEqual(plan["counts"]["update"], 0)

    def test_cumulative_updates_only_api_owned(self):
        rows = [_poc(3, _L1, VIEWS="20000", LIKES="900", COMMENTS="40")]
        ss = {"DVp_bFuDirS": {"values": {"VIEWS": "20000", "LIKES": "900", "COMMENTS": "40"}}}
        plan = self._plan(rows, ss, likes=1000)
        self.assertEqual(plan["counts"]["update"], 1)          # LIKES 900->1000
        self.assertEqual(plan["fills"][3], {"LIKES": "1000",   # only the changed +
                                            "REACH": "15000", "SAVES": "120", "SHARES": "30",
                                            "ENGAGEMENT_RATE": "7.27", "POST_DATE": "2026-08-01",
                                            "DURATION_SECONDS": "8"})

    def test_manual_edit_never_overwritten(self):
        rows = [_poc(3, _L1, LIKES="555")]                     # human-typed, not our last value
        ss = {"DVp_bFuDirS": {"values": {"LIKES": "900"}}}
        plan = self._plan(rows, ss, likes=1000)
        self.assertEqual(plan["counts"]["manual_protected"], 1)
        self.assertNotIn("LIKES", plan["fills"].get(3, {}))    # never overwritten

    def test_immutable_filled_once_not_changed(self):
        rows = [_poc(3, _L1, POST_DATE="2020-01-01", DURATION_SECONDS="99")]
        ss = {"DVp_bFuDirS": {"values": {}}}
        plan = self._plan(rows, ss)
        self.assertGreaterEqual(plan["counts"]["immutable_kept"], 2)
        self.assertNotIn("POST_DATE", plan["fills"].get(3, {}))
        self.assertNotIn("DURATION_SECONDS", plan["fills"].get(3, {}))

    def test_account_demographics_never_per_post(self):
        media = [{"id": "m1", "permalink": _L1, "media_product_type": "REELS"}]
        mp = smi.map_media_to_poc_rows(media, [_poc(3, _L1)])
        plan = smi.plan_incremental(mp, {"m1": {"gender": "M 60 / F 40"}},
                                    list(_META) + ["AGE_SPLIT", "GENDER_SPLIT"], {})
        self.assertNotIn("AGE_SPLIT", str(plan["fills"]))
        self.assertNotIn("GENDER_SPLIT", str(plan["fills"]))


# --------------------------------------------------------------------------- #
# refresh orchestration
# --------------------------------------------------------------------------- #
class TestRefresh(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        rows = [_poc(3, _L1)]
        sheets = FakeSheets(rows)
        rep = smi.refresh_instagram_metrics(dry_run=True, client=FakeClient(), sheets=sheets,
                                            sync_state={})
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["safe"])
        self.assertEqual(sheets.written, {})           # nothing written
        self.assertEqual(rep["new_rows"], 1)

    def test_not_connected_blocks_refresh(self):
        rep = smi.refresh_instagram_metrics(dry_run=True, client=FakeClient(account_raises=True),
                                            sheets=FakeSheets([_poc(3, _L1)]), sync_state={})
        self.assertFalse(rep["ok"])

    def test_apply_writes_only_metric_columns(self):
        rows = [_poc(3, _L1)]
        sheets = FakeSheets(rows)
        rep = smi.refresh_instagram_metrics(apply=True, client=FakeClient(), sheets=sheets,
                                            sync_state={})
        self.assertTrue(rep["wrote"])
        written_cols = {name for (_r, name) in sheets.written}
        self.assertTrue(written_cols)                  # something written
        # only metric columns — never taxonomy / Product / ICP / Status
        self.assertNotIn("Status", written_cols)
        self.assertNotIn("Product", written_cols)
        self.assertNotIn("ICP", written_cols)
        self.assertTrue(all(c not in taxonomy.all_signal_columns() for c in written_cols))
        self.assertIn("VIEWS", written_cols)

    def test_apply_refused_when_no_match(self):
        # media that doesn't match any POC row -> not safe -> refuse
        class NoMatch(FakeClient):
            def fetch_media(self, max_items=500):
                return [{"id": "mX", "permalink": "https://www.instagram.com/reel/NONE/",
                         "media_product_type": "REELS"}]
        sheets = FakeSheets([_poc(3, _L1)])
        rep = smi.refresh_instagram_metrics(apply=True, client=NoMatch(), sheets=sheets,
                                            sync_state={})
        self.assertFalse(rep["safe"])
        self.assertFalse(rep.get("wrote"))
        self.assertEqual(sheets.written, {})


# --------------------------------------------------------------------------- #
# Slack status
# --------------------------------------------------------------------------- #
class TestSlackStatus(unittest.TestCase):
    def setUp(self):
        self._orig = smi._load_poc
        rows = [_poc(3, _L1, VIEWS="20000"), _poc(4, "https://ig/x")]
        smi._load_poc = lambda sheets=None: FakeSheets(rows)
        # avoid sync-state network read
        self._rss = smi.read_sync_state
        smi.read_sync_state = lambda poc: {"DVp_bFuDirS": {"last": "2026-08-11 09:00 UTC"}}

    def tearDown(self):
        smi._load_poc = self._orig
        smi.read_sync_state = self._rss

    def test_how_many_have_metrics(self):
        out = sa.answer_social_analytics_question("how many reels have metrics?")
        self.assertIn("1 of 2", out)

    def test_last_refreshed(self):
        out = sa.answer_social_analytics_question("when were metrics last refreshed?")
        self.assertIn("2026-08-11", out)

    def test_missing_reels(self):
        out = sa.answer_social_analytics_question("are any reels missing metrics?")
        self.assertIn("1 reels are missing", out)

    def test_tracking(self):
        out = sa.answer_social_analytics_question("what metrics are we actually tracking?")
        self.assertIn("VIEWS", out)

    def test_connected_status_no_secrets(self):
        import re
        out = sa.answer_social_analytics_question("are IG metrics connected?")
        self.assertIsNotNone(out)
        # naming the env var is fine; a token-shaped VALUE must never appear
        self.assertNotRegex(out, r"\b(EAA|IGQ)[A-Za-z0-9_-]{20,}")


if __name__ == "__main__":
    unittest.main()
