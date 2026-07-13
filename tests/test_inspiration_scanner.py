"""Unit tests for the inspiration scanner: provider abstraction, dedup,
row-building, limit resolution, and an end-to-end scan with in-memory fakes
(no network). Also asserts every written row carries
SOURCE_TYPE=EXTERNAL_INSPIRATION.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import inspiration_scanner as scan
from inspiration_scanner import (InspirationPost, InspirationProvider,
                                 YtDlpInstagramProvider, dedup_posts,
                                 make_source_id, post_to_row, resolve_limits,
                                 get_provider)
from inspiration_sheets import SOURCE_TYPE_EXTERNAL


def _post(pid="", url="", ptype="Reel"):
    return InspirationPost(post_id=pid, post_url=url, post_type=ptype)


class TestProviderAbstraction(unittest.TestCase):
    def test_base_provider_is_abstract(self):
        with self.assertRaises(TypeError):
            InspirationProvider()  # abstract — cannot instantiate

    def test_get_provider_default_is_ytdlp(self):
        p = get_provider()
        self.assertIsInstance(p, YtDlpInstagramProvider)
        self.assertEqual(p.name, "ytdlp")

    def test_get_provider_unknown_raises(self):
        with self.assertRaises(RuntimeError):
            get_provider("does-not-exist")


class TestSourceId(unittest.TestCase):
    def test_prefers_platform_and_post_id(self):
        self.assertEqual(
            make_source_id("Instagram", _post(pid="ABC123")), "instagram:ABC123")

    def test_falls_back_to_url(self):
        self.assertEqual(
            make_source_id("Instagram", _post(url="https://ig/reel/x/")),
            "https://ig/reel/x/")


class TestDedup(unittest.TestCase):
    def test_skips_existing_source_id(self):
        existing = {"SOURCE_ID": {"instagram:A"}, "POST_ID": set(), "POST_URL": set()}
        posts = [_post(pid="A", url="u1"), _post(pid="B", url="u2")]
        fresh, skipped = dedup_posts("Instagram", posts, existing)
        self.assertEqual(skipped, 1)
        self.assertEqual([p.post_id for p in fresh], ["B"])

    def test_skips_existing_post_id_and_url(self):
        existing = {"SOURCE_ID": set(), "POST_ID": {"B"}, "POST_URL": {"u3"}}
        posts = [_post(pid="B", url="u2"), _post(pid="C", url="u3"),
                 _post(pid="D", url="u4")]
        fresh, skipped = dedup_posts("Instagram", posts, existing)
        self.assertEqual(skipped, 2)  # B by post_id, C by url
        self.assertEqual([p.post_id for p in fresh], ["D"])

    def test_within_batch_dedup(self):
        existing = {"SOURCE_ID": set(), "POST_ID": set(), "POST_URL": set()}
        posts = [_post(pid="A", url="u1"), _post(pid="A", url="u1")]
        fresh, skipped = dedup_posts("Instagram", posts, existing)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(skipped, 1)


class TestRowBuilding(unittest.TestCase):
    def test_source_type_is_always_external(self):
        ch = {"CHANNEL_ID": "c1", "PLATFORM": "Instagram", "HANDLE": "@x",
              "MACRO_INDUSTRY": "Sports", "SUBCATEGORY": "Goalkeeper"}
        row = post_to_row(ch, _post(pid="A", url="https://ig/reel/A/"),
                          scraped_at="2026-07-13T00:00:00+00:00")
        self.assertEqual(row["SOURCE_TYPE"], SOURCE_TYPE_EXTERNAL)
        self.assertEqual(row["SOURCE_ID"], "instagram:A")
        self.assertEqual(row["ANALYSIS_STATUS"], "Not Analyzed")
        self.assertEqual(row["CHANNEL_ID"], "c1")
        self.assertEqual(row["SHORTLISTED"], "FALSE")


class TestResolveLimits(unittest.TestCase):
    def test_channel_row_wins(self):
        ch = {"LOOKBACK_DAYS": "7", "MAX_POSTS_PER_SCAN": "5"}
        cfg = {"DEFAULT_LOOKBACK_DAYS": "30", "DEFAULT_MAX_POSTS_PER_SCAN": "20"}
        self.assertEqual(resolve_limits(ch, cfg), (7, 5))

    def test_config_default_used_when_row_blank(self):
        ch = {"LOOKBACK_DAYS": "", "MAX_POSTS_PER_SCAN": ""}
        cfg = {"DEFAULT_LOOKBACK_DAYS": "45", "DEFAULT_MAX_POSTS_PER_SCAN": "12"}
        self.assertEqual(resolve_limits(ch, cfg), (45, 12))

    def test_hard_fallback_when_nothing_set(self):
        self.assertEqual(resolve_limits({}, {}),
                         (scan.DEFAULT_LOOKBACK_DAYS, scan.DEFAULT_MAX_POSTS_PER_SCAN))


# ---------------------------------------------------------------------------
# End-to-end scan with in-memory fakes (no network, no Google Sheets)
# ---------------------------------------------------------------------------
class FakeProvider(InspirationProvider):
    name = "fake"

    def __init__(self, posts_by_handle):
        self._posts = posts_by_handle

    def fetch_recent_posts(self, *, handle, profile_url, lookback_days, max_posts):
        return list(self._posts.get(handle, []))[:max_posts]


class FakeSheets:
    """In-memory stand-in for InspirationSheets. Refuses internal-tab writes by
    construction (it simply has no internal tab)."""
    def __init__(self, channels, existing=None, cfg=None):
        self._channels = channels
        self._existing = existing or {"SOURCE_ID": set(), "POST_ID": set(), "POST_URL": set()}
        self._cfg = cfg or {}
        self.appended = []
        self.runs = []
        self.status_updates = []

    def read_config(self):
        return dict(self._cfg)

    def read_active_channels(self):
        return list(self._channels)

    def existing_content_keys(self):
        return {k: set(v) for k, v in self._existing.items()}

    def append_content_rows(self, rows):
        for r in rows:
            # Mirror the real guard so the test fails loudly on any unlabeled row.
            if r.get("SOURCE_TYPE") != SOURCE_TYPE_EXTERNAL:
                raise ValueError("unlabeled external row")
        self.appended.extend(rows)
        return len(rows)

    def append_run(self, run):
        self.runs.append(run)

    def update_channel_status(self, row_index, **kw):
        self.status_updates.append((row_index, kw))


class TestScanEndToEnd(unittest.TestCase):
    def _channel(self, row, handle):
        return {"_row": row, "CHANNEL_ID": "c" + str(row), "PLATFORM": "Instagram",
                "HANDLE": handle, "PROFILE_URL": "https://ig/" + handle,
                "MACRO_INDUSTRY": "Sports", "SUBCATEGORY": "Goalkeeper",
                "LOOKBACK_DAYS": "30", "MAX_POSTS_PER_SCAN": "10"}

    def test_scan_writes_only_external_rows_and_dedups(self):
        channels = [self._channel(2, "acct_a"), self._channel(3, "acct_b")]
        provider = FakeProvider({
            "acct_a": [_post(pid="A1", url="uA1"), _post(pid="A2", url="uA2")],
            # acct_b shares A2 (cross-channel dup) + one new post B1
            "acct_b": [_post(pid="A2", url="uA2"), _post(pid="B1", url="uB1")],
        })
        # A1 already exists in the sheet — should be skipped.
        sheets = FakeSheets(channels, existing={
            "SOURCE_ID": {"instagram:A1"}, "POST_ID": set(), "POST_URL": set()})

        run = scan.scan_channels(sheets=sheets, provider=provider)

        added_ids = [r["POST_ID"] for r in sheets.appended]
        self.assertEqual(sorted(added_ids), ["A2", "B1"])   # A1 existing, A2 cross-dedup
        self.assertTrue(all(r["SOURCE_TYPE"] == SOURCE_TYPE_EXTERNAL
                            for r in sheets.appended))
        self.assertEqual(run["POSTS_ADDED"], 2)
        self.assertEqual(run["POSTS_SKIPPED_EXISTING"], 2)  # A1 + duplicate A2
        self.assertEqual(run["CHANNELS_SCANNED"], 2)
        self.assertEqual(run["CHANNELS_FAILED"], 0)
        self.assertEqual(run["STATUS"], "Completed")
        self.assertEqual(run["PROVIDER"], "fake")

    def test_run_is_logged_and_channel_status_updated(self):
        channels = [self._channel(2, "acct_a")]
        provider = FakeProvider({"acct_a": [_post(pid="A1", url="uA1")]})
        sheets = FakeSheets(channels)
        run = scan.scan_channels(sheets=sheets, provider=provider)
        self.assertEqual(len(sheets.runs), 1)               # exactly one run row
        self.assertIn("RUN_ID", sheets.runs[0])
        self.assertTrue(sheets.status_updates)              # channel bookkeeping written

    def test_one_failing_channel_does_not_abort_run(self):
        class BoomProvider(InspirationProvider):
            name = "boom"
            def fetch_recent_posts(self, **kw):
                if kw["handle"] == "bad":
                    raise RuntimeError("scrape exploded")
                return [_post(pid="OK1", url="uOK1")]

        channels = [self._channel(2, "bad"), self._channel(3, "good")]
        sheets = FakeSheets(channels)
        run = scan.scan_channels(sheets=sheets, provider=BoomProvider())
        self.assertEqual(run["CHANNELS_FAILED"], 1)
        self.assertEqual(run["POSTS_ADDED"], 1)             # good channel still ingested
        self.assertEqual(run["STATUS"], "Partial")
        self.assertIn("scrape exploded", run["ERROR_SUMMARY"])


if __name__ == "__main__":
    unittest.main()
