"""Apify-powered Research + Discovery Layer (matryoshka rings).

Sits BEFORE external inspiration analysis. Reads research queries from the
APIFY_DISCOVERY_QUERIES tab, runs Instagram/TikTok Apify actors, normalizes the
output, filters for copyright safety + mechanism relevance, ranks by
view/follower ratio, and writes safe candidates into INSPIRATION_CONTENT with
SOURCE_TYPE=EXTERNAL_INSPIRATION.

Boundaries (unchanged from the rest of the Inspiration Layer):
- External inspiration is NOT Storelli proof. View/follower ratio is only a
  discovery-priority signal; it never enters performance buckets, correlations,
  the Signal Library, Marketing Learnings, or any "what works" calculation.
- Discovery only. No matching, winning-format profiles, idea generation, or
  scoring here.

This module reuses the existing InspirationPost model and post_to_row/dedup
helpers so discovery and the manual URL queue converge on one schema and one
dedup keyspace.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import config
from inspiration_scanner import (InspirationPost, _finalize_and_log_run,
                                 _new_run, _now_iso, make_source_id,
                                 post_is_duplicate, post_to_row, remember_post)
from inspiration_sheets import (CONTENT_DISCOVERY_COLUMNS, SOURCE_TYPE_EXTERNAL,
                                InspirationSheets)
from logger import get_logger

log = get_logger()

# Hard safety cap applied even if a query row asks for more.
HARD_CAP_PER_QUERY = 25
COPYRIGHT_SAFETY_THRESHOLD = 0.5      # below this => reject
MECHANISM_RELEVANCE_THRESHOLD = 0.15  # below this => skip (weak relevance)


# ---------------------------------------------------------------------------
# Copyright / relevance keyword banks
# ---------------------------------------------------------------------------
# Famous players / named pros (non-exhaustive, lowercased). Presence in caption/
# handle/hashtags strongly implies match/celebrity footage -> reject.
FAMOUS_PLAYERS = {
    "messi", "ronaldo", "cristiano", "cr7", "neymar", "mbappe", "mbappé",
    "haaland", "benzema", "salah", "kane", "debruyne", "de bruyne", "modric",
    "courtois", "neuer", "alisson", "ederson", "donnarumma", "oblak", "ter stegen",
    "lewandowski", "vinicius", "bellingham", "griezmann", "suarez", "kroos",
    "buffon", "casillas", "iker casillas", "maignan", "onana",
}
# Match / broadcast / highlight content -> reject.
MATCH_TERMS = {
    "highlight", "highlights", "full match", "full-match", "matchday",
    "extended highlights", "match highlights", "full time", "fulltime",
    "goal compilation", "goals compilation", "save compilation", "compilation",
    "broadcast", "live stream", "livestream", "watch live", "vs ", " v ",
    "matchweek", "post match", "post-match",
}
# Leagues / competitions / national teams -> reject.
LEAGUE_TERMS = {
    "champions league", "ucl", "premier league", "epl", "world cup", "worldcup",
    "la liga", "laliga", "serie a", "bundesliga", "ligue 1", "ligue1", "uefa",
    "fifa", "euros", "euro 2024", "euro 2028", "copa america", "el clasico",
    "national team", "mls cup", "concacaf", "afcon",
}
# Fan/celebrity edits, gambling, and other disallowed buckets.
EDIT_TERMS = {"fan edit", "fanedit", "edit ", " edit", "4k edit", "football edit",
              "soccer edit", "edits", "capcut edit"}
GAMBLING_TERMS = {"bet", "betting", "odds", "gambling", "casino", "parlay",
                  "sportsbook", "bookie"}
BLOCK_MISC = {"onlyfans", "nsfw", "porn", "xxx", "election", "trump", "biden",
              "gore", "graphic injury"}
# Non-sports "protection" and beauty/lifestyle false positives that generic
# Ring-5/7 terms ("protective gear", "do this not that") pull in. Phrases are
# chosen to avoid false-matching legitimate sports content (e.g. bare "hair"
# would hit "chair"; bare "tactical" would hit soccer "tactics") — so we match
# specific multi-word phrases instead.
OFF_DOMAIN_TERMS = {
    "body armor", "body armour", "stab proof", "stab-proof", "stabproof",
    "stab vest", "bulletproof", "bullet proof", "knife", "tactical vest",
    "tactical gear", "cut resistant", "cut-resistant", "nitrile",
    "industrial glove", "executive protection", "neck protection",
    "beauty tutorial", "hair tutorial", "hairstyle", "hair hack", "hairhack",
    "hairtok", "braids", "twist braid", "makeup",
}

# Positive mechanism signals we WANT (creator-led education, protection demos,
# mistake->correction, confidence framing, gear proof, etc.).
POSITIVE_TERMS = {
    "coach", "coaching", "tutorial", "how to", "drill", "training", "technique",
    "do this not that", "do this, not that", "dont do this", "don't do this",
    "mistake", "mistakes", "correction", "before and after", "before/after",
    "protection", "protective gear", "gear test", "gear review", "impact test",
    "injury prevention", "prehab", "recovery", "mobility", "confidence",
    "fear", "afraid", "mental", "psychology", "youth", "parents", "beginner",
    "beginners", "i wish i knew", "watch this before", "3 things", "explained",
    "safety", "diving", "landing", "turf burn", "bruise", "goalkeeper", "keeper",
    "review", "demo", "ugc",
}


# ---------------------------------------------------------------------------
# Normalized candidate
# ---------------------------------------------------------------------------
@dataclass
class DiscoveryCandidate:
    post: InspirationPost
    handle: str = ""
    follower_count: Optional[int] = None
    platform: str = "Instagram"
    hashtags: str = ""
    # filled by the ranking pass:
    view_follower_ratio: Optional[float] = field(default=None)
    ratio_score: float = 0.0
    absolute_view_score: float = 0.0
    mechanism_relevance_score: float = 0.0
    copyright_safety_score: float = 1.0
    priority_score: float = 0.0
    safety_status: str = "Safe"      # Safe | Rejected | Needs Review | Skipped
    rejection_reason: str = ""


# ---------------------------------------------------------------------------
# Normalization (accepts the many field aliases IG/TikTok actors emit)
# ---------------------------------------------------------------------------
def _first(d: dict, *keys):
    for k in keys:
        if "." in k:
            cur = d
            ok = True
            for part in k.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok and cur not in (None, ""):
                return cur
        elif d.get(k) not in (None, ""):
            return d[k]
    return None


def _to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _hashtags_str(item) -> str:
    tags = item.get("hashtags") or item.get("challenges")
    out = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                out.append(t)
            elif isinstance(t, dict):
                out.append(str(t.get("name") or t.get("title") or ""))
    return " ".join("#" + t.lstrip("#") for t in out if t)


def normalize_instagram(item: dict) -> DiscoveryCandidate:
    url = str(_first(item, "url", "postUrl", "inputUrl", "webpage_url") or "").strip()
    pid = str(_first(item, "shortcode", "id", "postId", "code") or "").strip()
    if not url and pid:
        url = f"https://www.instagram.com/reel/{pid}/"
    ptype = str(_first(item, "type", "productType", "mediaType") or "").strip() or "Unknown"
    ts = _first(item, "timestamp", "takenAt", "takenAtTimestamp", "date")
    published = _iso_from_ts(ts)
    post = InspirationPost(
        post_id=pid,
        post_url=url,
        post_type="Reel" if "reel" in url.lower() or "video" in ptype.lower() else ptype or "Unknown",
        published_at=published,
        caption=str(_first(item, "caption", "text") or "").strip(),
        thumbnail_url=str(_first(item, "displayUrl", "imageUrl", "thumbnailUrl") or "").strip(),
        media_url=str(_first(item, "videoUrl", "video_url") or "").strip(),
        view_count=str(_to_int(_first(item, "videoViewCount", "views", "videoPlayCount")) or "").strip(),
        like_count=str(_to_int(_first(item, "likesCount", "likes")) or "").strip(),
        comment_count=str(_to_int(_first(item, "commentsCount", "comments")) or "").strip(),
        scrape_status="Success",
    )
    return DiscoveryCandidate(
        post=post, platform="Instagram",
        handle=str(_first(item, "ownerUsername", "username", "handle") or "").strip(),
        follower_count=_to_int(_first(item, "ownerFollowersCount", "followersCount", "followers")),
        hashtags=_hashtags_str(item),
    )


def normalize_tiktok(item: dict) -> DiscoveryCandidate:
    url = str(_first(item, "webVideoUrl", "url", "videoUrl") or "").strip()
    pid = str(_first(item, "id", "videoId") or "").strip()
    post = InspirationPost(
        post_id=pid,
        post_url=url,
        post_type="Video",
        published_at=_iso_from_ts(_first(item, "createTimeISO", "createTime", "date")),
        caption=str(_first(item, "text", "description", "desc") or "").strip(),
        thumbnail_url=str(_first(item, "covers.default", "cover", "thumbnail",
                                 "videoMeta.coverUrl") or "").strip(),
        duration_seconds=str(_to_int(_first(item, "videoMeta.duration", "duration")) or "").strip(),
        view_count=str(_to_int(_first(item, "playCount", "views")) or "").strip(),
        like_count=str(_to_int(_first(item, "diggCount", "likes")) or "").strip(),
        comment_count=str(_to_int(_first(item, "commentCount", "comments")) or "").strip(),
        scrape_status="Success",
    )
    return DiscoveryCandidate(
        post=post, platform="TikTok",
        handle=str(_first(item, "authorMeta.name", "author", "username") or "").strip(),
        follower_count=_to_int(_first(item, "authorMeta.fans", "followers", "followerCount")),
        hashtags=_hashtags_str(item),
    )


def _iso_from_ts(ts) -> str:
    if ts in (None, ""):
        return ""
    # Already an ISO-ish string?
    if isinstance(ts, str) and ("-" in ts or "T" in ts):
        return ts
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Copyright / relevance filtering (pure, testable — no visual/face recognition)
# ---------------------------------------------------------------------------
def _blob(cand: DiscoveryCandidate) -> str:
    return " ".join([cand.post.caption or "", cand.handle or "", cand.hashtags or ""]).lower()


def _avoid_terms(query_row: dict) -> list[str]:
    raw = str(query_row.get("SHOULD_AVOID", "") or "")
    return [t.strip().lower() for t in raw.replace("|", ",").split(",") if t.strip()]


def assess_safety(cand: DiscoveryCandidate, query_row: dict) -> tuple[float, str, str]:
    """Return (copyright_safety_score, safety_status, rejection_reason).

    Hard rejects: famous players, match/broadcast/highlight, league/competition,
    fan/celebrity edits, gambling, and disallowed misc buckets, plus any explicit
    SHOULD_AVOID term. Rejection uses caption/hashtags/handle/query context only —
    never face recognition.
    """
    text = _blob(cand)

    def hit(terms):
        # NB: some bank terms carry intentional spaces (" v ", "vs ") — match
        # them verbatim, never stripped, so we don't false-hit inside words.
        return next((t for t in terms if t and t in text), None)

    for label, bank in (("famous player", FAMOUS_PLAYERS), ("match/highlight footage", MATCH_TERMS),
                        ("league/competition footage", LEAGUE_TERMS), ("fan/celebrity edit", EDIT_TERMS)):
        h = hit(bank)
        if h:
            return 0.0, "Rejected", f"{label}: '{h.strip()}'"
    h = hit(GAMBLING_TERMS)
    if h:
        return 0.0, "Rejected", f"gambling content: '{h.strip()}'"
    h = hit(OFF_DOMAIN_TERMS)
    if h:
        return 0.15, "Rejected", f"off-domain / non-sports protection: '{h.strip()}'"
    h = hit(BLOCK_MISC)
    if h:
        return 0.0, "Rejected", f"disallowed content: '{h.strip()}'"
    h = hit(_avoid_terms(query_row))
    if h:
        return 0.1, "Rejected", f"matched SHOULD_AVOID: '{h.strip()}'"
    return 1.0, "Safe", ""


def _should_find_terms(query_row: dict) -> list[str]:
    raw = str(query_row.get("SHOULD_FIND", "") or "")
    return [t.strip().lower() for t in raw.replace("|", ",").split(",") if t.strip()]


def mechanism_relevance(cand: DiscoveryCandidate, query_row: dict) -> float:
    """0..1 relevance from positive-mechanism terms + the query's SHOULD_FIND
    hints. Never uses engagement numbers."""
    text = _blob(cand)
    pos_hits = sum(1 for t in POSITIVE_TERMS if t in text)
    find_hits = sum(1 for t in _should_find_terms(query_row) if t in text)
    # Diminishing returns; SHOULD_FIND weighted a bit higher.
    score = 1 - math.exp(-(pos_hits + 1.5 * find_hits) / 2.5)
    return round(min(1.0, score), 4)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def view_follower_ratio(views: Optional[int], followers: Optional[int]) -> Optional[float]:
    if not followers or followers <= 0 or views is None:
        return None
    return round(views / followers, 4)


def _ratio_score(ratio: Optional[float]) -> float:
    if ratio is None:
        return 0.0
    return round(min(1.0, ratio / 5.0), 4)   # 5x-followers views == max


def _absolute_view_score(views: Optional[int]) -> float:
    if not views or views <= 0:
        return 0.0
    return round(min(1.0, math.log10(views) / 7.0), 4)  # ~10M views == max


def priority_score(mechanism: float, ratio_s: float, absolute_s: float,
                   safety: float) -> float:
    return round(0.40 * mechanism + 0.30 * ratio_s + 0.15 * absolute_s
                 + 0.15 * safety, 4)


def rank_candidate(cand: DiscoveryCandidate, query_row: dict) -> DiscoveryCandidate:
    """Populate all safety + ranking fields on the candidate."""
    safety_score, status, reason = assess_safety(cand, query_row)
    cand.copyright_safety_score = safety_score
    cand.safety_status = status
    cand.rejection_reason = reason

    views = _to_int(cand.post.view_count)
    cand.view_follower_ratio = view_follower_ratio(views, cand.follower_count)
    cand.ratio_score = _ratio_score(cand.view_follower_ratio)
    cand.absolute_view_score = _absolute_view_score(views)
    cand.mechanism_relevance_score = mechanism_relevance(cand, query_row)
    cand.priority_score = priority_score(
        cand.mechanism_relevance_score, cand.ratio_score,
        cand.absolute_view_score, cand.copyright_safety_score)
    return cand


def decide_candidate(cand: DiscoveryCandidate, query_row: dict) -> tuple[bool, str]:
    """Apply the priority rule. Returns (accept, reason_if_not).

    1. copyright below threshold -> reject
    2. famous-player/match/highlight -> reject (already reflected in safety)
    3. weak mechanism relevance -> skip
    4/5. otherwise accept (ranking orders them later).
    """
    if cand.safety_status == "Rejected" or cand.copyright_safety_score < COPYRIGHT_SAFETY_THRESHOLD:
        return False, cand.rejection_reason or "failed copyright-safety threshold"
    if cand.mechanism_relevance_score < MECHANISM_RELEVANCE_THRESHOLD:
        return False, "weak mechanism relevance"
    # Optional per-query hard gates.
    min_ratio = _num(query_row.get("MIN_VIEW_FOLLOWER_RATIO"))
    if (min_ratio is not None and cand.view_follower_ratio is not None
            and cand.view_follower_ratio < min_ratio):
        return False, f"below MIN_VIEW_FOLLOWER_RATIO ({min_ratio})"
    max_followers = _num(query_row.get("MAX_FOLLOWER_COUNT"))
    if (max_followers is not None and cand.follower_count is not None
            and cand.follower_count > max_followers):
        return False, f"above MAX_FOLLOWER_COUNT ({max_followers})"
    min_views = _num(query_row.get("MIN_VIEW_COUNT"))
    views = _to_int(cand.post.view_count)
    if min_views is not None and views is not None and views < min_views:
        return False, f"below MIN_VIEW_COUNT ({min_views})"
    return True, ""


def _num(v):
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Apify client
# ---------------------------------------------------------------------------
class ApifyClient:
    """Minimal Apify actor runner (run-sync-get-dataset-items). The token is
    passed as a query param and never logged."""

    def __init__(self, token: Optional[str] = None):
        self.token = (token or config.APIFY_TOKEN or "").strip()
        if not self.token:
            raise RuntimeError("APIFY_TOKEN is not set; discovery cannot run.")

    def run_actor(self, actor_id: str, run_input: dict, timeout: float = 300.0) -> list[dict]:
        import httpx
        actor = actor_id.replace("/", "~")
        url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        resp = httpx.post(url, params={"token": self.token}, json=run_input,
                          timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("items", [])


def build_actor_input(platform: str, query_type: str, query: str,
                      max_results: int, lookback_days: Optional[int]) -> dict:
    """Best-effort actor input for the default IG/TikTok scrapers."""
    q = query.strip()
    qt = (query_type or "Search").strip().lower()
    if platform.strip().lower() == "tiktok":
        if qt == "hashtag":
            return {"hashtags": [q.lstrip("#")], "resultsPerPage": max_results,
                    "shouldDownloadVideos": False, "shouldDownloadCovers": False}
        if qt == "profile":
            return {"profiles": [q.lstrip("@")], "resultsPerPage": max_results,
                    "shouldDownloadVideos": False, "shouldDownloadCovers": False}
        if qt == "url":
            return {"postURLs": [q], "shouldDownloadVideos": False,
                    "shouldDownloadCovers": False}
        return {"searchQueries": [q], "resultsPerPage": max_results,
                "shouldDownloadVideos": False, "shouldDownloadCovers": False}
    # Instagram
    if qt == "url":
        return {"directUrls": [q], "resultsType": "posts", "resultsLimit": max_results}
    if qt == "profile":
        return {"search": q.lstrip("@"), "searchType": "user", "searchLimit": 1,
                "resultsType": "posts", "resultsLimit": max_results}
    # Search + Hashtag both go through hashtag search (IG has no good text search).
    return {"search": q.lstrip("#"), "searchType": "hashtag", "searchLimit": 1,
            "resultsType": "posts", "resultsLimit": max_results}


def _normalizer_for(platform: str):
    return normalize_tiktok if platform.strip().lower() == "tiktok" else normalize_instagram


# ---------------------------------------------------------------------------
# Content-row assembly
# ---------------------------------------------------------------------------
def candidate_to_row(cand: DiscoveryCandidate, query_row: dict, *, scraped_at: str) -> dict:
    pseudo_channel = {
        "CHANNEL_ID": cand.handle,
        "PLATFORM": cand.platform,
        "HANDLE": cand.handle,
        "MACRO_INDUSTRY": str(query_row.get("MACRO_INDUSTRY", "")).strip(),
        "SUBCATEGORY": str(query_row.get("SUBCATEGORY", "")).strip(),
        "TARGET_PRODUCT": str(query_row.get("TARGET_PRODUCT", "")).strip(),
        "TARGET_ICP": str(query_row.get("TARGET_ICP", "")).strip(),
        "REASON_FOR_ADDING": str(query_row.get("REASON_FOR_QUERY", "")).strip(),
    }
    row = post_to_row(pseudo_channel, cand.post, scraped_at=scraped_at)
    row.update({
        "DISCOVERY_QUERY_ID": str(query_row.get("QUERY_ID", "")).strip(),
        "DISCOVERY_PLATFORM": cand.platform,
        "DISCOVERY_QUERY": str(query_row.get("QUERY", "")).strip(),
        "RESEARCH_RING": str(query_row.get("RESEARCH_RING", "")).strip(),
        "SEMANTIC_DISTANCE": str(query_row.get("SEMANTIC_DISTANCE", "")).strip(),
        "REASON_FOR_QUERY": str(query_row.get("REASON_FOR_QUERY", "")).strip(),
        "SHOULD_FIND": str(query_row.get("SHOULD_FIND", "")).strip(),
        "SHOULD_AVOID": str(query_row.get("SHOULD_AVOID", "")).strip(),
        "FOLLOWER_COUNT": "" if cand.follower_count is None else cand.follower_count,
        "VIEW_FOLLOWER_RATIO": "" if cand.view_follower_ratio is None else cand.view_follower_ratio,
        "ABSOLUTE_VIEW_SCORE": cand.absolute_view_score,
        "RATIO_SCORE": cand.ratio_score,
        "MECHANISM_RELEVANCE_SCORE": cand.mechanism_relevance_score,
        "COPYRIGHT_SAFETY_SCORE": cand.copyright_safety_score,
        "PRIORITY_SCORE": cand.priority_score,
        "SAFETY_STATUS": cand.safety_status,
        "REJECTION_REASON": cand.rejection_reason,
    })
    return row


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _effective_max(query_row: dict) -> int:
    raw = _num(query_row.get("MAX_RESULTS"))
    n = int(raw) if raw and raw > 0 else config.APIFY_DEFAULT_MAX_RESULTS
    hard = max(1, min(config.APIFY_MAX_RESULTS_PER_QUERY, HARD_CAP_PER_QUERY))
    return max(1, min(n, hard))


def discover_inspiration(sheets: Optional[InspirationSheets] = None,
                         client: Optional[ApifyClient] = None) -> dict:
    """Run every ACTIVE discovery query, filter/rank candidates, and append safe
    ones to INSPIRATION_CONTENT. Returns a run summary (RUN_TYPE=Discovery)."""
    sheets = sheets or InspirationSheets()
    if client is None:
        client = ApifyClient()   # raises cleanly if APIFY_TOKEN is missing

    try:
        sheets.ensure_queries_tab()
        sheets.ensure_content_columns(CONTENT_DISCOVERY_COLUMNS)
    except Exception as e:  # noqa: BLE001
        log.warning("discovery tab/column ensure failed (continuing): %s", e)

    run = _new_run("Discovery", "apify")
    queries = sheets.read_active_queries()
    run["CHANNELS_SCANNED"] = len(queries)
    log.info("Apify discovery: %d active query(ies)", len(queries))
    errors: list[str] = []
    existing = sheets.existing_content_keys()
    run_cap = max(1, config.APIFY_MAX_RESULTS_PER_RUN)
    total_added = 0

    for q in queries:
        platform = str(q.get("PLATFORM", "") or "Instagram").strip()
        qtype = str(q.get("QUERY_TYPE", "") or "Search").strip()
        query = str(q.get("QUERY", "")).strip()
        max_results = _effective_max(q)
        added = skipped = 0
        try:
            if total_added >= run_cap:
                sheets.update_query_row(q["_row"], last_run_at=_now_iso(),
                                        last_run_status="Skipped", results_added=0,
                                        results_skipped=0,
                                        error_message=f"run cap {run_cap} reached")
                continue

            actor = (config.APIFY_TIKTOK_ACTOR_ID if platform.lower() == "tiktok"
                     else config.APIFY_INSTAGRAM_ACTOR_ID)
            lookback = _num(q.get("LOOKBACK_DAYS"))
            run_input = build_actor_input(platform, qtype, query, max_results,
                                          int(lookback) if lookback else None)
            items = client.run_actor(actor, run_input)
            run["POSTS_DISCOVERED"] += len(items)

            normalize = _normalizer_for(platform)
            candidates = [normalize(it) for it in items if isinstance(it, dict)]
            # Rank, then order by priority so caps keep the best.
            for c in candidates:
                rank_candidate(c, q)
            candidates.sort(key=lambda c: c.priority_score, reverse=True)

            scraped_at = _now_iso()
            for c in candidates:
                if total_added >= run_cap:
                    break
                accept, reason = decide_candidate(c, q)
                if not accept:
                    skipped += 1
                    continue
                if not (c.post.post_id or c.post.post_url):
                    skipped += 1
                    continue
                if post_is_duplicate(c.platform, c.post, existing):
                    skipped += 1
                    continue
                sheets.append_content_rows([candidate_to_row(c, q, scraped_at=scraped_at)])
                remember_post(c.platform, c.post, existing)
                added += 1
                total_added += 1

            run["POSTS_ADDED"] += added
            run["POSTS_SKIPPED_EXISTING"] += skipped
            sheets.update_query_row(
                q["_row"], last_run_at=scraped_at, last_run_status="Completed",
                results_added=added, results_skipped=skipped, error_message="")
            log.info("  query %s (%s/%s): discovered=%d added=%d skipped=%d",
                     q.get("QUERY_ID", "?"), platform, qtype, len(items), added, skipped)
        except Exception as e:  # noqa: BLE001 - one query must not abort the run
            run["CHANNELS_FAILED"] += 1
            run["POSTS_FAILED"] += 1
            msg = f"{q.get('QUERY_ID', query)}: {type(e).__name__}: {e}"
            errors.append(msg)
            log.error("  discovery query failed: %s", msg)
            try:
                sheets.update_query_row(q["_row"], last_run_at=_now_iso(),
                                        last_run_status="Failed",
                                        error_message=str(e)[:400])
            except Exception:  # noqa: BLE001
                pass

    return _finalize_and_log_run(sheets, run, errors,
                                 failed=run["CHANNELS_FAILED"],
                                 total=len(queries))


def print_discovery_summary(run: dict) -> None:
    print("\nApify inspiration discovery complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Provider:               {run.get('PROVIDER')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Queries run:            {run.get('CHANNELS_SCANNED')}")
    print(f"Queries failed:         {run.get('CHANNELS_FAILED')}")
    print(f"Candidates discovered:  {run.get('POSTS_DISCOVERED')}")
    print(f"Added to content:       {run.get('POSTS_ADDED')}")
    print(f"Skipped (dupe/reject):  {run.get('POSTS_SKIPPED_EXISTING')}")
    if run.get("ERROR_SUMMARY"):
        print(f"Errors:                 {run.get('ERROR_SUMMARY')}")
