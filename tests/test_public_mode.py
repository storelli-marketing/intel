"""Tests for PUBLIC MODE: owned Storelli discovery via Apify, no Meta dependency.

Covers Part 22: Meta-absent health, exact-owner routing, lookalike rejection,
real-shape normalization, the acquisition hierarchy (Apify media URL before
cookies), private metrics staying absent, follower-count policy, and the
conversational refresh follow-ups running through the stateful agent.

Run: python -m unittest tests.test_public_mode
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import conversation_refresh as CR
import intelligence_refresh as ir
import owned_discovery as od


# A realistic apify/instagram-scraper item (field names per the live actor +
# the aliases the repo's normalizer already learned from real responses).
_ITEM = {
    "id": "3512345678901234567",
    "shortCode": "DVp_bFuDirS",
    "url": "https://www.instagram.com/reel/DVp_bFuDirS/",
    "type": "Video",
    "productType": "clips",
    "caption": "Turf burn is not a badge of honour #goalkeeper",
    "timestamp": "2026-08-11T09:30:00.000Z",
    "videoUrl": "https://scontent.cdninstagram.com/o1/v/t16/f1/m86/video.mp4",
    "displayUrl": "https://scontent.cdninstagram.com/v/t51/thumb.jpg",
    "videoPlayCount": 20431,
    "likesCount": 903,
    "commentsCount": 41,
    "videoDuration": 8.4,
    "ownerUsername": "storellisoccer",
    "ownerFollowersCount": 171204,
}


class _FakeApify:
    def __init__(self, items):
        self.items = items
        self.last_input = None

    def run_actor(self, actor_id, run_input, timeout=300.0):
        self.last_input = run_input
        return self.items


# --------------------------------------------------------------------------- #
# ownership (Part 5)
# --------------------------------------------------------------------------- #
class TestOwnership(unittest.TestCase):
    def setUp(self):
        self._h = config.STORELLI_INSTAGRAM_HANDLE
        config.STORELLI_INSTAGRAM_HANDLE = "storellisoccer"

    def tearDown(self):
        config.STORELLI_INSTAGRAM_HANDLE = self._h

    def test_exact_owner_is_internal(self):
        self.assertTrue(od.is_owned_handle("storellisoccer"))
        self.assertTrue(od.is_owned_handle("@storellisoccer"))
        self.assertTrue(od.is_owned_handle("https://www.instagram.com/storellisoccer/reel/X/"))

    def test_lookalike_is_external(self):
        for imposter in ("storellisoccer_official", "storelli.soccer", "storellisoccerfan",
                         "storelli", "notstorellisoccer"):
            self.assertFalse(od.is_owned_handle(imposter), imposter)

    def test_ownership_never_inferred_from_content(self):
        # caption mentioning the brand must NOT make it owned
        item = dict(_ITEM, ownerUsername="somefan", caption="best Storelli gear ever @storellisoccer")
        routed = od.route_by_ownership([od.normalize_owned_item(item)])
        self.assertEqual(routed[od.INTERNAL_OWNED], [])
        self.assertEqual(len(routed[od.EXTERNAL_INSPIRATION]), 1)

    def test_routing_splits_mixed_batch(self):
        owned = od.normalize_owned_item(_ITEM)
        other = od.normalize_owned_item(dict(_ITEM, ownerUsername="randomkeeper"))
        routed = od.route_by_ownership([owned, other])
        self.assertEqual(len(routed[od.INTERNAL_OWNED]), 1)
        self.assertEqual(len(routed[od.EXTERNAL_INSPIRATION]), 1)
        self.assertEqual(routed[od.INTERNAL_OWNED][0]["creator_handle"], "storellisoccer")


# --------------------------------------------------------------------------- #
# normalization + public metrics (Parts 4/8)
# --------------------------------------------------------------------------- #
class TestNormalization(unittest.TestCase):
    def test_real_shape_normalizes(self):
        m = od.normalize_owned_item(_ITEM)
        self.assertEqual(m["creator_handle"], "storellisoccer")
        self.assertEqual(m["media_id"], "3512345678901234567")
        self.assertEqual(m["shortcode"], "DVp_bFuDirS")
        self.assertEqual(m["link"], "https://www.instagram.com/reel/DVp_bFuDirS/")
        self.assertEqual(m["views"], 20431)
        self.assertEqual(m["likes"], 903)
        self.assertEqual(m["comments"], 41)
        self.assertEqual(m["duration_seconds"], 8)
        self.assertEqual(m["follower_count"], 171204)
        self.assertTrue(m["video_url"].endswith(".mp4"))
        self.assertEqual(m["post_date"][:10], "2026-08-11")

    def test_absent_fields_stay_absent_never_fabricated(self):
        thin = {"url": "https://www.instagram.com/reel/AAA/", "ownerUsername": "storellisoccer"}
        m = od.normalize_owned_item(thin)
        self.assertIsNone(m["views"])
        self.assertIsNone(m["likes"])
        self.assertIsNone(m["shares"])
        self.assertIsNone(m["duration_seconds"])
        self.assertEqual(m["video_url"], "")

    def test_private_metrics_are_never_present(self):
        m = od.normalize_owned_item(_ITEM)
        for private in od.PRIVATE_ONLY_METRICS:
            self.assertNotIn(private, m, f"{private} must never be inferred in public mode")

    def test_metrics_availability_report(self):
        avail = od.available_public_metrics([od.normalize_owned_item(_ITEM)])
        self.assertEqual(avail["views"], 1)
        self.assertEqual(avail["likes"], 1)
        self.assertEqual(avail["shares"], 0)          # actor didn't return shares
        self.assertEqual(avail["video_url"], 1)

    def test_bounded_scan_input(self):
        payload = od.build_owned_scan_input("storellisoccer", 30, "10 days", "reels")
        self.assertEqual(payload["directUrls"],
                         ["https://www.instagram.com/storellisoccer/"])
        self.assertEqual(payload["resultsLimit"], 30)
        self.assertEqual(payload["resultsType"], "reels")
        self.assertEqual(payload["onlyPostsNewerThan"], "10 days")

    def test_lookback_uses_last_refresh_plus_buffer(self):
        self.assertTrue(od.lookback_expression("").endswith("days"))
        expr = od.lookback_expression("2020-01-01 00:00 UTC")
        self.assertRegex(expr, r"^\d+ days$")

    def test_follower_policy_prefers_scan_then_fallback(self):
        owned = [od.normalize_owned_item(_ITEM)]
        self.assertEqual(od.current_follower_count(owned), 171204)
        thin = [od.normalize_owned_item({"url": "https://www.instagram.com/reel/A/",
                                         "ownerUsername": "storellisoccer"})]
        self.assertIsNone(od.current_follower_count(thin))   # caller falls back to config


class TestScan(unittest.TestCase):
    def setUp(self):
        self._h = config.STORELLI_INSTAGRAM_HANDLE
        config.STORELLI_INSTAGRAM_HANDLE = "storellisoccer"

    def tearDown(self):
        config.STORELLI_INSTAGRAM_HANDLE = self._h

    def test_scan_returns_owned_only(self):
        client = _FakeApify([_ITEM, dict(_ITEM, ownerUsername="otherguy")])
        out = od.scan_owned_media(client=client)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["owned"]), 1)
        self.assertEqual(len(out["external_rejected"]), 1)
        self.assertEqual(out["handle"], "storellisoccer")

    def test_scan_failure_is_clean(self):
        class Boom:
            def run_actor(self, *a, **k):
                raise RuntimeError("Monthly usage hard limit exceeded")
        out = od.scan_owned_media(client=Boom())
        self.assertFalse(out["ok"])
        self.assertIn("limit", out["error"].lower())
        self.assertEqual(out["owned"], [])


# --------------------------------------------------------------------------- #
# acquisition hierarchy (Part 7)
# --------------------------------------------------------------------------- #
class TestAcquisition(unittest.TestCase):
    def _client(self):
        import gemini_client
        c = gemini_client.GeminiClient.__new__(gemini_client.GeminiClient)  # no API key needed
        return c

    def test_apify_url_used_first_no_ytdlp(self):
        c = self._client()
        calls = []
        c._download_direct = lambda url: (calls.append(("direct", url)) or "/tmp/v.mp4")
        c._download_ytdlp = lambda link, ck: (_ for _ in ()).throw(
            AssertionError("yt-dlp must not run when an Apify media URL works"))
        path, method = c.acquire("https://instagram.com/reel/X/", "https://cdn/video.mp4")
        self.assertEqual(method, "apify_media_url")
        self.assertEqual(len(calls), 1)

    def test_falls_back_to_public_then_cookies(self):
        c = self._client()
        order = []

        def direct(url):
            order.append("direct")
            raise RuntimeError("cdn 403")

        def ytdlp(link, use_cookies):
            order.append("cookies" if use_cookies else "public")
            if not use_cookies:
                raise RuntimeError("login required")
            return "/tmp/v.mp4"

        c._download_direct, c._download_ytdlp = direct, ytdlp
        real = config.YTDLP_COOKIES_PATH
        config.YTDLP_COOKIES_PATH = "/tmp/cookies.txt"
        try:
            _p, method = c.acquire("https://instagram.com/reel/X/", "https://cdn/v.mp4")
        finally:
            config.YTDLP_COOKIES_PATH = real
        self.assertEqual(order, ["direct", "public", "cookies"])
        self.assertEqual(method, "ytdlp_cookies")

    def test_missing_cookies_ok_when_apify_video_works(self):
        c = self._client()
        c._download_direct = lambda url: "/tmp/v.mp4"
        real = config.YTDLP_COOKIES_PATH
        config.YTDLP_COOKIES_PATH = ""
        try:
            _p, method = c.acquire("https://instagram.com/reel/X/", "https://cdn/v.mp4")
        finally:
            config.YTDLP_COOKIES_PATH = real
        self.assertEqual(method, "apify_media_url")

    def test_all_paths_fail_reports_every_attempt(self):
        import gemini_client
        c = self._client()
        c._download_direct = lambda url: (_ for _ in ()).throw(RuntimeError("403"))
        c._download_ytdlp = lambda link, ck: (_ for _ in ()).throw(RuntimeError("login"))
        with self.assertRaises(gemini_client.VideoDownloadError) as cm:
            c.acquire("https://instagram.com/reel/X/", "https://cdn/v.mp4")
        msg = str(cm.exception)
        self.assertIn("apify_media_url", msg)
        self.assertIn("ytdlp_public", msg)


# --------------------------------------------------------------------------- #
# health without Meta (Parts 2/17)
# --------------------------------------------------------------------------- #
class TestPublicModeHealth(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(config, k) for k in
                       ("APIFY_TOKEN", "GEMINI_API_KEY", "GOOGLE_SHEET_ID",
                        "GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "NOTION_API_KEY",
                        "NOTION_PARENT_PAGE_ID", "INSTAGRAM_ACCESS_TOKEN",
                        "INSTAGRAM_BUSINESS_ACCOUNT_ID", "STORELLI_INSTAGRAM_HANDLE",
                        "YTDLP_COOKIES_PATH")}
        # public mode fully configured; Meta + cookies absent on purpose
        config.APIFY_TOKEN = "x"
        config.GEMINI_API_KEY = "x"
        config.GOOGLE_SHEET_ID = "x"
        config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH = "/tmp/sa.json"
        config.NOTION_API_KEY = "x"
        config.NOTION_PARENT_PAGE_ID = "x"
        config.INSTAGRAM_ACCESS_TOKEN = ""
        config.INSTAGRAM_BUSINESS_ACCOUNT_ID = ""
        config.STORELLI_INSTAGRAM_HANDLE = "storellisoccer"
        config.YTDLP_COOKIES_PATH = ""
        self._runs = ir.last_runs
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ir.last_runs = lambda sheets=None, n=1: [{"FINISHED_AT": now, "STATUS": "success"}]

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(config, k, v)
        ir.last_runs = self._runs

    def test_meta_absent_is_healthy(self):
        h = ir.health_state()
        self.assertEqual(h["state"], "HEALTHY")

    def test_readiness_labels_meta_optional_not_blocked(self):
        caps = ir.refresh_readiness()
        self.assertEqual(caps["owned_public_discovery"]["status"], "READY")
        self.assertEqual(caps["public_metrics"]["status"], "READY")
        self.assertEqual(caps["private_instagram_insights"]["status"], "NOT_CONFIGURED")
        self.assertTrue(caps["private_instagram_insights"]["optional"])
        self.assertEqual(caps["cookie_fallback"]["status"], "NOT_CONFIGURED")
        self.assertTrue(caps["cookie_fallback"]["optional"])
        self.assertEqual(caps["owned_account_identity"]["handle"], "storellisoccer")

    def test_no_apify_is_blocked(self):
        config.APIFY_TOKEN = ""
        h = ir.health_state()
        self.assertEqual(h["state"], "BLOCKED")
        self.assertTrue(any("owned_public_discovery" in r for r in h["reasons"]))

    def test_missing_because_no_meta_lists_private_only(self):
        gaps = ir.missing_because_no_meta()
        self.assertIn("saves", gaps)
        self.assertIn("reach", gaps)
        self.assertTrue(any("demographic" in g for g in gaps))


# --------------------------------------------------------------------------- #
# conversational refresh (Parts 14/16)
# --------------------------------------------------------------------------- #
class TestConversationalRefresh(unittest.TestCase):
    def setUp(self):
        self._runs = ir.last_runs
        ir.last_runs = lambda sheets=None, n=1: [{
            "FINISHED_AT": "2026-08-17 09:00 UTC", "STATUS": "success",
            "INTERNAL_NEW_MEDIA": "6", "INTERNAL_ANALYZED": "6", "EXTERNAL_ADDED": "27",
            "EXTERNAL_QUALITY_80": "5", "PROFILES_UPDATED": "0",
            "IDEA_REGEN_RECOMMENDED": "False", "FAILED_COUNT": "0"}]

    def tearDown(self):
        ir.last_runs = self._runs

    def _pack(self):
        pack = CR.build_refresh_pack(sheets=None)
        pack.setdefault("new_reels", [])
        pack["top_pattern"] = "Curiosity Gap + Demo for BodyShield"
        return pack

    def test_topic_detection(self):
        self.assertEqual(CR.detect_refresh_topic("did we find anything new?"), CR.NEW_FINDINGS)
        self.assertEqual(CR.detect_refresh_topic("what actually matters?"), CR.WHAT_MATTERS)
        self.assertEqual(CR.detect_refresh_topic("what should we shoot because of that?"),
                         CR.ACT_ON_IT)
        self.assertEqual(CR.detect_refresh_topic(
            "are we missing anything because Meta isn't connected?"), CR.META_GAP)
        self.assertEqual(CR.detect_refresh_topic("give me 5 BodyShield ideas"), "")

    def test_new_findings_uses_refreshed_numbers(self):
        out = CR.render(CR.NEW_FINDINGS, self._pack())
        self.assertIn("6", out)
        self.assertIn("27", out)
        self.assertNotIn("*Why:*", out)          # not the templated scaffold

    def test_meta_gap_answer_is_not_a_blocker_story(self):
        out = CR.render(CR.META_GAP, self._pack())
        self.assertIn("private Insights", out)
        self.assertIn("public", out.lower())
        self.assertNotIn("BLOCKED", out)

    def test_shapes_differ_by_topic(self):
        pack = self._pack()
        a = CR.render(CR.NEW_FINDINGS, pack)
        b = CR.render(CR.WHY, pack)
        c = CR.render(CR.ACT_ON_IT, pack)
        self.assertNotEqual(a, b)
        self.assertNotEqual(b, c)

    def test_external_never_framed_as_proof(self):
        for topic in (CR.NEW_FINDINGS, CR.WHAT_MATTERS, CR.EVIDENCE, CR.ACT_ON_IT):
            out = CR.render(topic, self._pack()).lower()
            self.assertNotRegex(out, r"(external|inspiration|reference)[^.]{0,40}prov(e|es|en|ing)")

    def test_no_run_yet_is_honest(self):
        ir.last_runs = lambda sheets=None, n=1: []
        out = CR.render(CR.NEW_FINDINGS, CR.build_refresh_pack(sheets=None))
        self.assertIn("no automatic refresh", out.lower())


if __name__ == "__main__":
    unittest.main()
