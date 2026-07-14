"""Unit tests for the Apify Research + Discovery Layer.

All offline — no real Apify calls (a FakeApifyClient returns canned IG/TikTok
items). Proves normalization, copyright/relevance filtering, ranking, caps,
dedup, isolation, and failure handling.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import inspiration_discovery as disc
from inspiration_discovery import (ApifyClient, DiscoveryCandidate,
                                   assess_safety, candidate_to_row,
                                   decide_candidate, discover_inspiration,
                                   mechanism_relevance, normalize_instagram,
                                   normalize_tiktok, priority_score,
                                   rank_candidate, view_follower_ratio,
                                   _effective_max)
from inspiration_scanner import InspirationPost
from inspiration_sheets import SOURCE_TYPE_EXTERNAL
import performance
import correlations as corr
from sheets_client import SheetsClient
import taxonomy

A_SIGNAL_COL = taxonomy.all_signal_columns()[0]


# ---- sample actor outputs --------------------------------------------------
IG_GOOD = {
    "url": "https://www.instagram.com/reel/ABC123/", "shortcode": "ABC123",
    "caption": "Goalkeeper training: do this not that to avoid turf burn #goalkeeper",
    "ownerUsername": "gk_coach", "ownerFollowersCount": 8000,
    "displayUrl": "https://cdn/thumb.jpg", "videoViewCount": 120000,
    "likesCount": 5000, "commentsCount": 100, "timestamp": 1712000000,
    "type": "Video", "hashtags": ["goalkeeper", "keeper"],
}
IG_FAMOUS = {
    "url": "https://www.instagram.com/reel/XYZ/", "shortcode": "XYZ",
    "caption": "Messi incredible free kick highlights vs Real Madrid",
    "ownerUsername": "footballedits", "ownerFollowersCount": 500000,
    "videoViewCount": 2000000,
}
TT_GOOD = {
    "webVideoUrl": "https://www.tiktok.com/@coach_mike/video/999", "id": "999",
    "text": "3 things young athletes need for injury prevention #prehab",
    "authorMeta": {"name": "coach_mike", "fans": 12000},
    "covers": {"default": "https://cdn/cov.jpg"}, "videoMeta": {"duration": 30},
    "createTimeISO": "2025-01-01T00:00:00Z", "playCount": 300000,
    "diggCount": 9000, "commentCount": 50,
}


def _query(row=2, **over):
    q = {"_row": row, "QUERY_ID": "Q1", "PLATFORM": "Instagram", "QUERY_TYPE": "Hashtag",
         "QUERY": "goalkeeper training", "RESEARCH_RING": "Ring 1",
         "MACRO_INDUSTRY": "Sports", "SUBCATEGORY": "Goalkeeper", "ACTIVE": "TRUE",
         "MAX_RESULTS": "10", "SHOULD_FIND": "goalkeeper, training",
         "SHOULD_AVOID": "", "REASON_FOR_QUERY": "keeper pain points"}
    q.update(over)
    return q


class FakeApifyClient:
    def __init__(self, items=None, raise_on=None):
        self._items = items or []
        self._raise_on = raise_on   # actor id substring to fail on

    def run_actor(self, actor_id, run_input, timeout=300.0):
        if self._raise_on and self._raise_on in actor_id:
            raise RuntimeError("actor boom")
        return list(self._items)


class FakeDiscoverySheets:
    def __init__(self, queries, existing=None):
        self._queries = queries
        self._existing = existing or {"SOURCE_ID": set(), "POST_ID": set(), "POST_URL": set()}
        self.appended = []
        self.runs = []
        self.query_updates = []

    def ensure_queries_tab(self):
        return False

    def ensure_content_columns(self, columns):
        return []

    def read_active_queries(self):
        return list(self._queries)

    def existing_content_keys(self):
        return {k: set(v) for k, v in self._existing.items()}

    def append_content_rows(self, rows):
        for r in rows:
            if r.get("SOURCE_TYPE") != SOURCE_TYPE_EXTERNAL:
                raise ValueError("unlabeled external row")
        self.appended.extend(rows)
        return len(rows)

    def update_query_row(self, row_index, **kw):
        self.query_updates.append((row_index, kw))

    def append_run(self, run):
        self.runs.append(run)


# ---------------------------------------------------------------------------
class TestTokenGuard(unittest.TestCase):
    def test_missing_token_fails_cleanly(self):
        original = config.APIFY_TOKEN
        try:
            config.APIFY_TOKEN = ""      # simulate an unconfigured environment
            with self.assertRaises(RuntimeError):
                ApifyClient()
        finally:
            config.APIFY_TOKEN = original


class TestNormalization(unittest.TestCase):
    def test_instagram_normalizes(self):
        c = normalize_instagram(IG_GOOD)
        self.assertEqual(c.platform, "Instagram")
        self.assertEqual(c.post.post_id, "ABC123")
        self.assertEqual(c.post.post_url, "https://www.instagram.com/reel/ABC123/")
        self.assertEqual(c.post.post_type, "Reel")
        self.assertEqual(c.handle, "gk_coach")
        self.assertEqual(c.follower_count, 8000)
        self.assertEqual(c.post.view_count, "120000")
        self.assertEqual(c.post.like_count, "5000")

    def test_tiktok_normalizes(self):
        c = normalize_tiktok(TT_GOOD)
        self.assertEqual(c.platform, "TikTok")
        self.assertEqual(c.post.post_id, "999")
        self.assertEqual(c.post.post_url, "https://www.tiktok.com/@coach_mike/video/999")
        self.assertEqual(c.handle, "coach_mike")
        self.assertEqual(c.follower_count, 12000)
        self.assertEqual(c.post.view_count, "300000")
        self.assertEqual(c.post.thumbnail_url, "https://cdn/cov.jpg")
        self.assertEqual(c.post.duration_seconds, "30")


class TestSafetyFilter(unittest.TestCase):
    def test_famous_player_and_match_rejected(self):
        c = normalize_instagram(IG_FAMOUS)
        score, status, reason = assess_safety(c, _query())
        self.assertEqual(status, "Rejected")
        self.assertLess(score, disc.COPYRIGHT_SAFETY_THRESHOLD)
        accept, _ = decide_candidate(rank_candidate(c, _query()), _query())
        self.assertFalse(accept)

    def test_highlight_keyword_rejected(self):
        c = DiscoveryCandidate(post=InspirationPost(post_id="1", post_url="u",
                               caption="full match highlights extended"), platform="TikTok")
        _, status, _ = assess_safety(c, _query())
        self.assertEqual(status, "Rejected")

    def test_should_avoid_filtering(self):
        c = normalize_tiktok(TT_GOOD)   # normally safe
        q = _query(SHOULD_AVOID="prehab, ballet")
        _, status, reason = assess_safety(c, q)
        self.assertEqual(status, "Rejected")
        self.assertIn("SHOULD_AVOID", reason)

    def test_good_content_is_safe(self):
        c = normalize_instagram(IG_GOOD)
        score, status, _ = assess_safety(c, _query())
        self.assertEqual(status, "Safe")
        self.assertEqual(score, 1.0)

    def test_off_domain_non_sports_protection_rejected(self):
        for cap in ["Flexible stab proof neck protection #bodyarmor",
                    "This glove is almost impossible to cut #nitrile #cut resistant",
                    "Try this next time you do twist braids #hairtok"]:
            c = DiscoveryCandidate(post=InspirationPost(post_id="1", post_url="u",
                                   caption=cap), platform="TikTok")
            _, status, reason = assess_safety(c, _query())
            self.assertEqual(status, "Rejected", cap)

    def test_legit_sports_protection_not_over_rejected(self):
        # Must NOT be caught by the off-domain filter.
        c = DiscoveryCandidate(post=InspirationPost(post_id="1", post_url="u",
            caption="Best goalkeeper padded gloves and shin protection for soccer training"),
            platform="TikTok")
        _, status, _ = assess_safety(c, _query())
        self.assertEqual(status, "Safe")


class TestRanking(unittest.TestCase):
    def test_view_follower_ratio(self):
        self.assertEqual(view_follower_ratio(120000, 8000), 15.0)
        self.assertIsNone(view_follower_ratio(100, 0))
        self.assertIsNone(view_follower_ratio(None, 1000))

    def test_high_ratio_small_creator_beats_giant_low_ratio(self):
        # Equal mechanism + safety; small creator has far higher ratio.
        small = priority_score(mechanism=0.8, ratio_s=disc._ratio_score(15.0),
                               absolute_s=disc._absolute_view_score(120000), safety=1.0)
        giant = priority_score(mechanism=0.8, ratio_s=disc._ratio_score(0.05),
                               absolute_s=disc._absolute_view_score(5000000), safety=1.0)
        self.assertGreater(small, giant)

    def test_mechanism_relevance_positive(self):
        c = normalize_instagram(IG_GOOD)
        self.assertGreater(mechanism_relevance(c, _query()), 0.3)


class TestCaps(unittest.TestCase):
    def test_effective_max_hard_capped(self):
        self.assertEqual(_effective_max(_query(MAX_RESULTS="1000")),
                         min(config.APIFY_MAX_RESULTS_PER_QUERY, disc.HARD_CAP_PER_QUERY))

    def test_effective_max_default_when_blank(self):
        self.assertEqual(_effective_max(_query(MAX_RESULTS="")),
                         min(config.APIFY_DEFAULT_MAX_RESULTS,
                             config.APIFY_MAX_RESULTS_PER_QUERY, disc.HARD_CAP_PER_QUERY))

    def test_run_cap_enforced(self):
        many = [dict(IG_GOOD, shortcode=f"ID{i}", url=f"https://ig/reel/ID{i}/")
                for i in range(10)]
        sheets = FakeDiscoverySheets([_query(2), _query(3)])
        client = FakeApifyClient(items=many)
        original = config.APIFY_MAX_RESULTS_PER_RUN
        try:
            config.APIFY_MAX_RESULTS_PER_RUN = 3
            run = discover_inspiration(sheets=sheets, client=client)
        finally:
            config.APIFY_MAX_RESULTS_PER_RUN = original
        self.assertLessEqual(len(sheets.appended), 3)
        self.assertEqual(run["POSTS_ADDED"], len(sheets.appended))


class TestOrchestrator(unittest.TestCase):
    def test_source_type_always_external_and_ranking_written(self):
        sheets = FakeDiscoverySheets([_query(2)])
        client = FakeApifyClient(items=[IG_GOOD, IG_FAMOUS])  # 1 safe, 1 rejected
        run = discover_inspiration(sheets=sheets, client=client)
        self.assertEqual(run["RUN_TYPE"], "Discovery")
        self.assertEqual(len(sheets.appended), 1)               # famous rejected
        row = sheets.appended[0]
        self.assertEqual(row["SOURCE_TYPE"], SOURCE_TYPE_EXTERNAL)
        self.assertEqual(row["SAFETY_STATUS"], "Safe")
        self.assertEqual(row["DISCOVERY_QUERY_ID"], "Q1")
        self.assertEqual(row["VIEW_FOLLOWER_RATIO"], 15.0)
        self.assertTrue(float(row["PRIORITY_SCORE"]) > 0)
        self.assertEqual(len(sheets.runs), 1)

    def test_dedup_across_discovery_and_manual_queue(self):
        # A manual-queue post with the same shortcode already lives in content.
        sheets = FakeDiscoverySheets([_query(2)], existing={
            "SOURCE_ID": {"instagram:ABC123"}, "POST_ID": set(), "POST_URL": set()})
        client = FakeApifyClient(items=[IG_GOOD])
        run = discover_inspiration(sheets=sheets, client=client)
        self.assertEqual(sheets.appended, [])
        self.assertEqual(run["POSTS_ADDED"], 0)
        self.assertGreaterEqual(run["POSTS_SKIPPED_EXISTING"], 1)

    def test_failed_query_does_not_abort_run(self):
        sheets = FakeDiscoverySheets([_query(2, PLATFORM="TikTok", QUERY_TYPE="Search"),
                                      _query(3, PLATFORM="Instagram")])
        # Fail only the TikTok actor; Instagram still succeeds.
        client = FakeApifyClient(items=[IG_GOOD], raise_on="tiktok")
        run = discover_inspiration(sheets=sheets, client=client)
        self.assertEqual(run["CHANNELS_FAILED"], 1)
        self.assertEqual(run["POSTS_ADDED"], 1)                 # IG query still added
        self.assertEqual(run["STATUS"], "Partial")
        self.assertIn("boom", run["ERROR_SUMMARY"])


class TestIsolationFromInternal(unittest.TestCase):
    def test_discovered_row_excluded_from_evidence(self):
        c = rank_candidate(normalize_instagram(IG_GOOD), _query())
        row = candidate_to_row(c, _query(), scraped_at="2026-07-14T00:00:00+00:00")
        ext = {"_row": 999, "PERFORMANCE": "Great", A_SIGNAL_COL: "1", **row}
        internal = {"_row": 1, "PERFORMANCE": "Great", A_SIGNAL_COL: "1"}
        rows = [internal, ext]
        buckets = performance.buckets_for_rows(rows)
        self.assertIn(1, buckets)
        self.assertNotIn(999, buckets)                          # external excluded
        analyzed = [r for r in rows
                    if SheetsClient.is_analyzed(r) and r["_row"] in buckets]
        self.assertEqual(corr.compute(analyzed, buckets), corr.compute([internal], buckets))


if __name__ == "__main__":
    unittest.main()
