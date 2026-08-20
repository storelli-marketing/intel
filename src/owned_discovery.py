"""Owned Storelli content discovery via PUBLIC Apify data (no Meta API).

This is the public-mode replacement for the optional Meta/Instagram Insights
path: it scans the ONE trusted Storelli account's public posts through the
existing Apify infrastructure, verifies ownership deterministically, and hands
the internal loop normalized media it can append/analyze/refresh.

Hard rules:
- Ownership is `creator_handle == config.STORELLI_INSTAGRAM_HANDLE`, matched
  exactly. Never inferred from caption, content, logo, brand mention, or a
  lookalike username. Anything else is EXTERNAL_INSPIRATION.
- Classification happens AFTER acquisition — being fetched by the same Apify
  actor never makes a row internal.
- Only fields the actor actually returned are populated. Private-only metrics
  (saves, reach, impressions, demographics, follower/non-follower, profile
  visits, website clicks) are NEVER inferred — they stay absent.
"""
from __future__ import annotations

import re
from typing import Optional

import config
from logger import get_logger

log = get_logger()

INTERNAL_OWNED = "INTERNAL_OWNED"
EXTERNAL_INSPIRATION = "EXTERNAL_INSPIRATION"

# Public metrics the Apify actor can plausibly return for a post/reel. Anything
# not in this set is private-only and must never be inferred.
PUBLIC_METRIC_FIELDS = ("views", "likes", "comments", "shares", "follower_count")
PRIVATE_ONLY_METRICS = ("saves", "reach", "impressions", "profile_visits",
                        "website_clicks", "age_split", "gender_split",
                        "location_split", "follower_nonfollower_split")


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _to_int(v):
    try:
        s = re.sub(r"[,\s]", "", str(v))
        return int(float(s))
    except (TypeError, ValueError):
        return None


def normalize_handle(v: str) -> str:
    """Extract a bare lowercase handle from a handle or profile/post URL."""
    s = _s(v).lower()
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", s)
    if m and m.group(1) not in ("p", "reel", "reels", "tv", "explore"):
        return m.group(1)
    return s.lstrip("@").strip("/")


def is_owned_handle(handle: str) -> bool:
    """True ONLY for the one exact trusted Storelli handle."""
    trusted = _s(config.STORELLI_INSTAGRAM_HANDLE).lower()
    return bool(trusted) and normalize_handle(handle) == trusted


# ---------------------------------------------------------------------------
# Part 4 — bounded owned-account scan
# ---------------------------------------------------------------------------
def build_owned_scan_input(handle: str, max_results: int,
                           newer_than: Optional[str] = None,
                           results_type: str = "posts") -> dict:
    """Actor input for a bounded scan of ONE public profile.

    Uses `directUrls` (a known profile URL is cheaper/more reliable than search)
    and the actor's `onlyPostsNewerThan` for the lookback. `results_type` may be
    "posts" or "reels" (the actor supports both)."""
    payload = {
        "directUrls": [f"https://www.instagram.com/{normalize_handle(handle)}/"],
        "resultsType": results_type,
        "resultsLimit": max(1, int(max_results)),
        "addParentData": False,
    }
    if newer_than:
        payload["onlyPostsNewerThan"] = newer_than
    return payload


def lookback_expression(last_refresh_iso: str = "") -> str:
    """'Since last successful refresh + safety buffer', else the default window.
    Returned in the actor's relative form (e.g. '10 days')."""
    from datetime import datetime, timezone
    days = config.OWNED_SCAN_LOOKBACK_DAYS
    if last_refresh_iso:
        try:
            ts = datetime.strptime(last_refresh_iso, "%Y-%m-%d %H:%M UTC").replace(
                tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - ts).days
            days = max(1, elapsed) + config.OWNED_SCAN_BUFFER_DAYS
        except (ValueError, TypeError):
            pass
    return f"{days} days"


# ---------------------------------------------------------------------------
# normalization — only what the actor actually returned
# ---------------------------------------------------------------------------
def normalize_owned_item(item: dict, platform: str = "Instagram") -> dict:
    """Apify item -> the common owned-media object. Absent fields stay absent
    (None/'' ), never fabricated. Reuses the same field aliases the external
    discovery normalizer learned from real responses, plus a few the actor has
    used across versions."""
    from inspiration_discovery import _first  # same alias-tolerant helper

    url = _s(_first(item, "url", "postUrl", "inputUrl", "webpage_url"))
    shortcode = _s(_first(item, "shortCode", "shortcode", "code"))
    media_id = _s(_first(item, "id", "postId", "pk"))
    if not url and shortcode:
        url = f"https://www.instagram.com/reel/{shortcode}/"
    if not shortcode and url:
        m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
        shortcode = m.group(1) if m else ""
    ptype = _s(_first(item, "type", "productType", "mediaType"))
    ts = _first(item, "timestamp", "takenAt", "takenAtTimestamp", "date")
    from inspiration_discovery import _iso_from_ts
    return {
        "platform": platform,
        "creator_handle": normalize_handle(_s(_first(item, "ownerUsername", "username",
                                                    "ownerUserName", "handle"))),
        "media_id": media_id,
        "shortcode": shortcode,
        "link": url,
        "caption": _s(_first(item, "caption", "text")),
        "post_date": _iso_from_ts(ts),
        "duration_seconds": _to_int(_first(item, "videoDuration", "duration",
                                           "videoMeta.duration")),
        "video_url": _s(_first(item, "videoUrl", "video_url", "videoUrlBackup")),
        "thumbnail_url": _s(_first(item, "displayUrl", "imageUrl", "thumbnailUrl")),
        "views": _to_int(_first(item, "videoPlayCount", "videoViewCount", "views",
                                "playCount")),
        "likes": _to_int(_first(item, "likesCount", "likes")),
        "comments": _to_int(_first(item, "commentsCount", "comments")),
        "shares": _to_int(_first(item, "sharesCount", "reshareCount", "shares")),
        "follower_count": _to_int(_first(item, "ownerFollowersCount", "followersCount",
                                         "followers")),
        "raw_source": "apify_public",
    }


def available_public_metrics(items: list) -> dict:
    """Which public metrics the REAL response actually carried (for honest
    reporting: 'views available? likes? shares?')."""
    out = {f: 0 for f in PUBLIC_METRIC_FIELDS}
    out["duration_seconds"] = 0
    out["video_url"] = 0
    for m in items:
        for f in list(out):
            if m.get(f) not in (None, ""):
                out[f] += 1
    return out


# ---------------------------------------------------------------------------
# Part 5 — ownership routing (AFTER acquisition)
# ---------------------------------------------------------------------------
def route_by_ownership(items: list) -> dict:
    """Split normalized items into internal-owned vs external-inspiration by the
    exact trusted handle. A lookalike handle is EXTERNAL."""
    owned, external = [], []
    for m in items:
        (owned if is_owned_handle(m.get("creator_handle", "")) else external).append(m)
    return {INTERNAL_OWNED: owned, EXTERNAL_INSPIRATION: external}


# ---------------------------------------------------------------------------
# scan entrypoint
# ---------------------------------------------------------------------------
def scan_owned_media(client=None, handle: Optional[str] = None,
                     max_results: Optional[int] = None,
                     last_refresh_iso: str = "", results_type: str = "reels") -> dict:
    """Bounded public scan of the trusted Storelli account.

    Returns {ok, owned, external_rejected, metrics_available, handle, error}.
    Fail-soft: an Apify error yields ok=False with a clean reason.
    """
    handle = normalize_handle(handle or config.STORELLI_INSTAGRAM_HANDLE)
    if not handle:
        return {"ok": False, "error": "no trusted Storelli handle configured "
                                      "(STORELLI_INSTAGRAM_HANDLE)", "owned": []}
    if client is None:
        if not config.APIFY_TOKEN:
            return {"ok": False, "error": "APIFY_TOKEN not configured", "owned": []}
        from inspiration_discovery import ApifyClient
        client = ApifyClient()
    n = max_results or config.OWNED_SCAN_MAX_RESULTS
    run_input = build_owned_scan_input(handle, n, lookback_expression(last_refresh_iso),
                                       results_type)
    try:
        items = client.run_actor(config.APIFY_INSTAGRAM_ACTOR_ID, run_input)
    except Exception as e:  # noqa: BLE001 - never kill the refresh
        log.warning("owned scan failed: %s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "owned": []}
    normalized = [normalize_owned_item(i) for i in (items or [])]
    routed = route_by_ownership(normalized)
    owned = routed[INTERNAL_OWNED]
    if normalized and not owned:
        log.warning("owned scan returned %d item(s) but none matched the trusted handle %r",
                    len(normalized), handle)
    followers = current_follower_count(owned) or fetch_profile_followers(client, handle)
    return {"ok": True, "error": "", "handle": handle, "owned": owned,
            "external_rejected": routed[EXTERNAL_INSPIRATION],
            "fetched": len(normalized), "followers": followers,
            "follower_source": ("post_results" if current_follower_count(owned)
                                else ("profile_details" if followers else "unavailable")),
            "metrics_available": available_public_metrics(owned)}


def fetch_profile_followers(client=None, handle: Optional[str] = None) -> Optional[int]:
    """One cheap `resultsType: details` call for the REAL current follower count.

    Post/reel results do NOT expose ownerFollowersCount (verified against the live
    actor), so the denominator has to come from the profile itself. Returns None
    on any failure — callers then fall back to a stored snapshot, then to
    config.STORELLI_IG_FOLLOWER_COUNT."""
    handle = normalize_handle(handle or config.STORELLI_INSTAGRAM_HANDLE)
    if client is None:
        if not config.APIFY_TOKEN:
            return None
        from inspiration_discovery import ApifyClient
        client = ApifyClient()
    try:
        data = client.run_actor(config.APIFY_INSTAGRAM_ACTOR_ID, {
            "directUrls": [f"https://www.instagram.com/{handle}/"],
            "resultsType": "details", "resultsLimit": 1})
    except Exception as e:  # noqa: BLE001
        log.warning("profile-details follower lookup failed: %s", e)
        return None
    for item in (data or []):
        n = _to_int(item.get("followersCount") or item.get("followers_count"))
        if n:
            return n
    return None


def current_follower_count(owned: list) -> Optional[int]:
    """The best current follower count from this scan (max non-null), else None.
    Callers fall back to config.STORELLI_IG_FOLLOWER_COUNT."""
    vals = [m["follower_count"] for m in owned if m.get("follower_count")]
    return max(vals) if vals else None
