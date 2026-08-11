"""Tests for automatic Instagram metrics ingestion (owned media -> POC).

Proves the safety + honesty guarantees:
  * missing IG credentials -> a clear, actionable error (never a crash);
  * API media/insights responses normalize correctly;
  * an unavailable metric is skipped, never fatal;
  * matching is by canonical LINK / shortcode only — never row order or fuzzy;
  * dry-run writes nothing; apply fills empty metric cells only and never
    overwrites a populated cell;
  * unmatched media is never written; only owned media is ever considered;
  * account-level demographics are NOT written as per-post demographics;
  * Slack explains missing config and never applies a write.

Run: python -m unittest tests.test_instagram_ingest
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import instagram_insights_client as iic
import social_metrics_ingest as smi
import social_analytics as sa
import taxonomy


def _poc(n, link, perf="Great", **extra):
    r = {"_row": n, "LINK": link, "PERFORMANCE": perf, "ICP": "", "Product": "",
         "Status": "completed", "Storytelling structure": ""}
    for c in taxonomy.all_signal_columns():
        r[c] = ""
    r.update(extra)
    return r


# A SheetsClient-like fake: read_rows + meta_col (+ ws for apply, added per-test).
class FakeSheets:
    def __init__(self, rows, meta_col):
        self._rows = rows
        self.meta_col = meta_col

    def read_rows(self):
        return [dict(r) for r in self._rows]


class FakeClient:
    def __init__(self, media=None, insights=None, demographics=None):
        self._media = media or []
        self._insights = insights or {}
        self._demographics = demographics or {}

    def fetch_media(self, max_items=500):
        return list(self._media)

    def fetch_media_insights(self, mid, mpt=""):
        return dict(self._insights.get(mid, {}))

    def fetch_account_demographics(self):
        return dict(self._demographics)


_LINK1 = "https://www.instagram.com/storellisoccer/reel/DVp_bFuDirS/"
_META_COLS = {"ID": 1, "LINK": 2, "PERFORMANCE": 3, "Status": 7, "VIEWS": 8, "REACH": 9,
              "LIKES": 10, "COMMENTS": 11, "SAVES": 12, "SHARES": 13, "ENGAGEMENT_RATE": 14,
              "POST_DATE": 15, "DURATION_SECONDS": 16}


# --------------------------------------------------------------------------- #
# Part A — config
# --------------------------------------------------------------------------- #
class TestConfig(unittest.TestCase):
    def test_missing_credentials_clear_error(self):
        rep = smi.pull_instagram_metrics(dry_run=True)   # no client, no creds in test env
        self.assertFalse(rep["ok"])
        self.assertFalse(rep["configured"])
        self.assertIn("not configured", rep["error"].lower())
        self.assertTrue(rep["missing_vars"])
        text = smi.render_pull_report(rep)
        self.assertIn("instagram_manage_insights", text)
        self.assertIn("SOCIAL_METRICS_IMPORT_STAGING", text)   # points at fallback

    def test_client_refuses_without_creds(self):
        with self.assertRaises(iic.InstagramConfigError):
            iic.InstagramInsightsClient(token="", ig_user_id="")


# --------------------------------------------------------------------------- #
# Part B — response normalization + graceful metrics
# --------------------------------------------------------------------------- #
class TestNormalization(unittest.TestCase):
    def test_normalize_media(self):
        m = iic.normalize_media({"id": "1", "permalink": _LINK1, "media_product_type": "REELS",
                                 "timestamp": "2026-08-01T10:00:00+0000", "duration": 8.4})
        self.assertEqual(m["id"], "1")
        self.assertEqual(m["media_product_type"], "REELS")
        self.assertEqual(m["duration"], 8.4)

    def test_flatten_classic_and_total_value(self):
        data = [{"name": "reach", "values": [{"value": 1500}]},
                {"name": "likes", "total_value": {"value": 900}}]
        self.assertEqual(iic._flatten_insight_values(data), {"reach": 1500, "likes": 900})

    def test_insights_unavailable_is_not_fatal(self):
        # http_get raises for the rich set, returns partial for the minimal set
        calls = {"n": 0}

        def http_get(path, params):
            calls["n"] += 1
            if "insights" in path and calls["n"] == 1:
                raise RuntimeError("(#100) metric not available for this media")
            return {"data": [{"name": "reach", "values": [{"value": 10}]}]}

        client = iic.InstagramInsightsClient(token="t", ig_user_id="u", http_get=http_get)
        ins = client.fetch_media_insights("m1", "REELS")
        self.assertEqual(ins, {"reach": 10})           # degraded to the minimal set, no crash

    def test_all_insights_unavailable_returns_empty(self):
        def http_get(path, params):
            raise RuntimeError("nope")
        client = iic.InstagramInsightsClient(token="t", ig_user_id="u", http_get=http_get)
        self.assertEqual(client.fetch_media_insights("m1", "REELS"), {})


# --------------------------------------------------------------------------- #
# Part C — URL canonicalization / shortcode / mapping
# --------------------------------------------------------------------------- #
class TestMapping(unittest.TestCase):
    def test_shortcode_extraction(self):
        self.assertEqual(smi.extract_instagram_shortcode(_LINK1), "DVp_bFuDirS")
        self.assertEqual(smi.extract_instagram_shortcode("https://instagram.com/p/ABC/?x=1"), "ABC")
        self.assertEqual(smi.extract_instagram_shortcode("https://www.instagram.com/reels/Z9/"), "Z9")
        self.assertEqual(smi.extract_instagram_shortcode("not a url"), "")

    def test_canonicalization_matches_variants(self):
        a = smi.canonicalize_instagram_url(_LINK1)
        b = smi.canonicalize_instagram_url("http://instagram.com/reel/DVp_bFuDirS?igshid=zz")
        self.assertEqual(a, b)

    def test_mapping_by_shortcode_not_row_order(self):
        media = [{"id": "m1", "permalink": "https://www.instagram.com/reel/DVp_bFuDirS/"},
                 {"id": "m2", "permalink": "https://www.instagram.com/reel/OTHER/"}]
        # POC rows in a DIFFERENT order than media; only shortcode should match
        rows = [_poc(9, "https://www.instagram.com/storellisoccer/reel/OTHER/"),
                _poc(3, _LINK1)]
        mp = smi.map_media_to_poc_rows(media, rows)
        pairs = {m["id"]: r["_row"] for m, r in mp["matched"]}
        self.assertEqual(pairs, {"m1": 3, "m2": 9})     # matched by shortcode, not order
        self.assertEqual(mp["unmatched_poc"], [])

    def test_unmatched_and_media_not_in_poc(self):
        media = [{"id": "m1", "permalink": "https://www.instagram.com/reel/OWNEDONLY/"}]
        rows = [_poc(3, _LINK1)]
        mp = smi.map_media_to_poc_rows(media, rows)
        self.assertEqual(mp["matched"], [])
        self.assertEqual(len(mp["unmatched_poc"]), 1)
        self.assertEqual(len(mp["media_not_in_poc"]), 1)   # owned media not tracked in POC


# --------------------------------------------------------------------------- #
# Part E — build values + fill planning + write policy
# --------------------------------------------------------------------------- #
class TestFillPlanning(unittest.TestCase):
    def test_build_metric_values_derives_engagement_and_skips_absent(self):
        media = {"timestamp": "2026-08-01T10:00:00+0000", "duration": 8.4}
        ins = {"views": 20000, "reach": 15000, "likes": 900, "total_interactions": 1500}
        vals = smi.build_metric_values(media, ins)
        self.assertEqual(vals["POST_DATE"], "2026-08-01")
        self.assertEqual(vals["VIEWS"], "20000")
        self.assertEqual(vals["ENGAGEMENT_RATE"], "10.0")   # 1500/15000*100, real derived
        self.assertNotIn("COMMENTS", vals)                  # absent -> not invented
        self.assertNotIn("AGE_SPLIT", vals)                 # never from the media API

    def test_plan_fills_empty_only_never_overwrite(self):
        media = [{"id": "m1", "permalink": _LINK1, "media_product_type": "REELS"}]
        # POC row already has VIEWS populated; LIKES empty
        rows = [_poc(3, _LINK1, VIEWS="999")]
        mp = smi.map_media_to_poc_rows(media, rows)
        insights = {"m1": {"views": 20000, "likes": 900}}
        plan = smi.plan_fills(mp, insights, list(_META_COLS))
        self.assertEqual(plan["already_populated"].get("VIEWS"), 1)   # not overwritten
        self.assertEqual(plan["would_fill"].get("LIKES"), 1)
        self.assertNotIn("VIEWS", plan["fills"].get(3, {}))           # empty-only


# --------------------------------------------------------------------------- #
# Parts D/E — dry-run + apply orchestration (with fakes)
# --------------------------------------------------------------------------- #
class TestOrchestration(unittest.TestCase):
    def _client(self):
        return FakeClient(
            media=[{"id": "m1", "permalink": _LINK1, "media_product_type": "REELS",
                    "timestamp": "2026-08-01T10:00:00+0000", "duration": 8},
                   {"id": "m2", "permalink": "https://www.instagram.com/reel/OWNEDONLY/",
                    "media_product_type": "REELS", "timestamp": "2026-07-01T00:00:00+0000"}],
            insights={"m1": {"views": 20000, "reach": 15000, "likes": 900, "comments": 40,
                             "saved": 120, "shares": 30, "total_interactions": 1090}},
            demographics={"follower_demographics": {"gender": {"M": 62, "F": 38}}})

    def test_dry_run_writes_nothing_and_is_safe(self):
        rows = [_poc(3, _LINK1), _poc(4, "https://www.instagram.com/reel/NOMATCH/")]
        snap = copy.deepcopy(rows)
        rep = smi.pull_instagram_metrics(dry_run=True, client=self._client(),
                                         sheets=FakeSheets(rows, _META_COLS))
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["media_fetched"], 2)
        self.assertEqual(rep["matched_rows"], 1)
        self.assertEqual(rep["media_not_in_poc"], 1)    # OWNEDONLY not in POC -> not written
        self.assertTrue(rep["safe"])
        self.assertGreater(rep["cells_to_fill"], 0)
        self.assertTrue(rep["has_account_demographics"])
        self.assertEqual(rows, snap, "dry-run must not mutate POC rows")

    def test_apply_fills_empty_only_and_verifies(self):
        rows = [_poc(3, _LINK1, VIEWS="777")]           # VIEWS pre-populated
        written = {}

        class WS:
            def batch_update(self, updates):
                for u in updates:
                    written[u["range"]] = u["values"][0][0]

        sheets = FakeSheets(rows, _META_COLS)
        sheets.ws = WS()
        # after batch_update, read_rows should reflect writes for verify -> emulate:
        applied = {}

        def read_rows_after():
            r = dict(rows[0])
            r.update(applied)
            return [r]

        # patch WS.batch_update to also stage into `applied` by column
        def batch_update(updates):
            import gspread
            inv = {v: k for k, v in _META_COLS.items()}
            for u in updates:
                # decode A1 -> col letter -> col index -> name (row is 3)
                col = gspread.utils.a1_to_rowcol(u["range"])[1]
                applied[inv[col]] = u["values"][0][0]
                written[u["range"]] = u["values"][0][0]
        sheets.ws.batch_update = batch_update
        sheets.read_rows = read_rows_after

        rep = smi.pull_instagram_metrics(apply=True, client=self._client(), sheets=sheets)
        self.assertTrue(rep["wrote"])
        self.assertTrue(rep["verify_ok"])
        self.assertNotIn("VIEWS", applied)              # pre-populated -> never overwritten
        self.assertIn("LIKES", applied)                 # empty -> filled
        self.assertEqual(applied["LIKES"], "900")

    def test_apply_refused_when_not_safe(self):
        # no media matches -> NOT SAFE -> apply refuses, writes nothing
        client = FakeClient(media=[{"id": "mX", "permalink": "https://www.instagram.com/reel/NONE/"}])
        rows = [_poc(3, _LINK1)]

        class WS:
            def batch_update(self, updates):
                raise AssertionError("must not write when NOT SAFE")
        sheets = FakeSheets(rows, _META_COLS)
        sheets.ws = WS()
        rep = smi.pull_instagram_metrics(apply=True, client=client, sheets=sheets)
        self.assertFalse(rep.get("wrote"))
        self.assertIn("refused", rep)

    def test_only_owned_media_considered(self):
        # The client only ever returns owned media (the API contract). Mapping
        # never fabricates a match for a link the API didn't return.
        client = FakeClient(media=[])                    # account returned no media
        rows = [_poc(3, _LINK1)]
        rep = smi.pull_instagram_metrics(dry_run=True, client=client,
                                         sheets=FakeSheets(rows, _META_COLS))
        self.assertEqual(rep["matched_rows"], 0)
        self.assertFalse(rep["safe"])                    # nothing owned -> not safe to apply


# --------------------------------------------------------------------------- #
# Part F — account demographics are not per-post
# --------------------------------------------------------------------------- #
class TestDemographicsHonesty(unittest.TestCase):
    def test_account_demographics_never_fill_per_post_columns(self):
        media = [{"id": "m1", "permalink": _LINK1, "media_product_type": "REELS"}]
        rows = [_poc(3, _LINK1)]
        mp = smi.map_media_to_poc_rows(media, rows)
        # even if the media insights somehow carried demographic-looking keys, the
        # per-post value builder never emits AGE/GENDER/LOCATION/FOLLOWER splits
        plan = smi.plan_fills(mp, {"m1": {"gender": "M 62 / F 38", "age": "x"}},
                              list(_META_COLS) + ["AGE_SPLIT", "GENDER_SPLIT"])
        self.assertNotIn("AGE_SPLIT", plan["would_fill"])
        self.assertNotIn("GENDER_SPLIT", plan["would_fill"])


# --------------------------------------------------------------------------- #
# Part G — Slack config status (never applies)
# --------------------------------------------------------------------------- #
class TestSlack(unittest.TestCase):
    def test_slack_explains_missing_config(self):
        for q in ("are Instagram metrics configured?", "can you update the metrics automatically?",
                  "pull Instagram metrics"):
            out = sa.answer_social_analytics_question(q)
            self.assertIsNotNone(out)
            self.assertIn("not configured", out.lower())
            self.assertIn("never", out.lower())          # never auto-applies from Slack
            # never fabricates a value / never claims it wrote
            self.assertNotIn("wrote", out.lower())

    def test_slack_demographics_missing_is_account_level(self):
        out = sa.answer_social_analytics_question("why are demographics missing?")
        self.assertIn("account level", out.lower())
        self.assertIn("per reel", out.lower())
        self.assertIn("won't fabricate", out.lower())

    def test_slack_routing_flags(self):
        for q in ("are Instagram metrics configured?", "refresh IG metrics",
                  "why are demographics missing?"):
            self.assertTrue(sa.is_social_analytics_query(q), q)


if __name__ == "__main__":
    unittest.main()
