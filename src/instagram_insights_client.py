"""Instagram Insights client — official Meta Graph API, owned media only.

Fetches Storelli-owned Instagram media + their insights and account-level
audience demographics through the official Graph API. It NEVER scrapes, never
uses cookies, and only ever reads the configured Storelli IG business account —
so it can only see owned content, never a competitor/inspiration video.

Design notes:
- Every metric is best-effort: media type, media age, account permissions and
  API version all affect which insights exist. One unavailable metric (or one
  media item that errors) must NEVER fail the whole run — it degrades to
  "unavailable" and the caller records that honestly.
- The single network seam is `http_get(path, params) -> dict`. Tests inject a
  fake; production uses httpx against graph.facebook.com. No global state.
"""
from __future__ import annotations

from typing import Callable, Optional

import config
from logger import get_logger

log = get_logger()

_GRAPH = "https://graph.facebook.com"

# Media fields we ask for (all optional in the response; we read what's there).
_MEDIA_FIELDS = ("id,permalink,media_type,media_product_type,timestamp,caption,"
                 "thumbnail_url")

# Candidate per-media insight metrics by media_product_type. The Graph API
# rejects the WHOLE insights call if any metric is invalid for that media, so we
# request a type-appropriate set and degrade on error.
_REEL_METRICS = ("views", "reach", "likes", "comments", "saved", "shares",
                 "total_interactions", "ig_reels_avg_watch_time", "ig_reels_video_view_total_time")
_FEED_METRICS = ("reach", "likes", "comments", "saved", "shares", "total_interactions",
                 "profile_visits", "profile_activity")
# Fallback minimal set if the richer request is rejected.
_MIN_METRICS = ("reach", "likes", "comments", "saved", "shares")

# Account-level demographic metrics (new metric_type=total_value + breakdown API).
_DEMO_METRICS = ("follower_demographics", "reached_audience_demographics",
                 "engaged_audience_demographics")
_DEMO_BREAKDOWNS = ("age", "gender", "city", "country")


class InstagramConfigError(RuntimeError):
    """Raised when automatic IG ingestion isn't configured."""


class InstagramInsightsClient:
    def __init__(self, token: Optional[str] = None, ig_user_id: Optional[str] = None,
                 api_version: Optional[str] = None,
                 http_get: Optional[Callable[[str, dict], dict]] = None):
        self.token = token if token is not None else config.INSTAGRAM_ACCESS_TOKEN
        self.ig_user_id = ig_user_id if ig_user_id is not None else config.INSTAGRAM_BUSINESS_ACCOUNT_ID
        self.version = api_version or config.META_API_VERSION
        self._http_get = http_get
        if not (self.token and self.ig_user_id):
            raise InstagramConfigError(config.IG_INGEST_NOT_CONFIGURED_MSG)

    # ---- transport ----------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        if self._http_get is not None:
            return self._http_get(path, params)
        import httpx
        url = f"{_GRAPH}/{self.version}/{path}"
        r = httpx.get(url, params={**params, "access_token": self.token}, timeout=30.0)
        r.raise_for_status()
        return r.json()

    # ---- owned media --------------------------------------------------------
    def fetch_media(self, max_items: int = 500) -> list:
        """Return owned media items (paged). Each is the raw API dict with the
        requested fields. Best-effort: a paging/API error stops paging cleanly
        and returns what was collected so far."""
        out, after, pages = [], None, 0
        while len(out) < max_items and pages < 50:
            params = {"fields": _MEDIA_FIELDS, "limit": 100}
            if after:
                params["after"] = after
            try:
                resp = self._get(f"{self.ig_user_id}/media", params)
            except Exception as e:  # noqa: BLE001
                log.warning("IG fetch_media page %d failed: %s", pages, e)
                break
            out.extend(resp.get("data", []) or [])
            after = (resp.get("paging", {}) or {}).get("cursors", {}).get("after")
            if not after:
                break
            pages += 1
        return out[:max_items]

    # ---- per-media insights -------------------------------------------------
    def _metrics_for(self, media_product_type: str) -> tuple:
        mpt = (media_product_type or "").upper()
        return _REEL_METRICS if mpt == "REELS" else _FEED_METRICS

    def fetch_media_insights(self, media_id: str, media_product_type: str = "") -> dict:
        """Return {metric: value} for one media, best-effort. Tries a
        type-appropriate metric set, then a minimal set, then {} — never raises."""
        for metrics in (self._metrics_for(media_product_type), _MIN_METRICS):
            try:
                resp = self._get(f"{media_id}/insights", {"metric": ",".join(metrics)})
            except Exception as e:  # noqa: BLE001
                log.info("IG insights (%s) unavailable for %s: %s",
                         ",".join(metrics)[:40], media_id, e)
                continue
            return _flatten_insight_values(resp.get("data", []) or [])
        return {}

    # ---- account-level demographics ----------------------------------------
    def fetch_account_demographics(self) -> dict:
        """Return {metric: {breakdown: {label: value}}} at the ACCOUNT level, or
        {} when unavailable. These are account-wide, NOT per-post."""
        out: dict = {}
        for metric in _DEMO_METRICS:
            for breakdown in _DEMO_BREAKDOWNS:
                try:
                    resp = self._get(f"{self.ig_user_id}/insights", {
                        "metric": metric, "period": "lifetime",
                        "metric_type": "total_value", "breakdown": breakdown})
                except Exception as e:  # noqa: BLE001
                    log.info("IG demographics %s/%s unavailable: %s", metric, breakdown, e)
                    continue
                parsed = _parse_demographic_breakdown(resp.get("data", []) or [])
                if parsed:
                    out.setdefault(metric, {})[breakdown] = parsed
        return out


# ---------------------------------------------------------------------------
# response normalization (pure — easy to unit-test)
# ---------------------------------------------------------------------------
def _flatten_insight_values(data: list) -> dict:
    """Graph insights `data` -> {metric_name: numeric value}. Handles both the
    classic `values:[{value}]` shape and the newer `total_value:{value}` shape."""
    out: dict = {}
    for item in data:
        name = item.get("name")
        if not name:
            continue
        if isinstance(item.get("total_value"), dict) and "value" in item["total_value"]:
            out[name] = item["total_value"]["value"]
        else:
            vals = item.get("values") or []
            if vals and isinstance(vals[0], dict) and "value" in vals[0]:
                out[name] = vals[0]["value"]
    return out


def _parse_demographic_breakdown(data: list) -> dict:
    """Parse a `metric_type=total_value` breakdown response -> {label: value}."""
    out: dict = {}
    for item in data:
        tv = item.get("total_value") or {}
        for br in (tv.get("breakdowns") or []):
            for res in (br.get("results") or []):
                dims = res.get("dimension_values") or []
                val = res.get("value")
                if dims and val is not None:
                    out[str(dims[0])] = val
    return out


def normalize_media(raw: dict) -> dict:
    """Raw media item -> a stable normalized dict the ingest layer consumes."""
    return {
        "id": str(raw.get("id", "")).strip(),
        "permalink": str(raw.get("permalink", "")).strip(),
        "media_type": str(raw.get("media_type", "")).strip(),
        "media_product_type": str(raw.get("media_product_type", "")).strip(),
        "timestamp": str(raw.get("timestamp", "")).strip(),
        "caption": str(raw.get("caption", "")).strip(),
        "thumbnail_url": str(raw.get("thumbnail_url", "")).strip(),
        "duration": raw.get("duration"),
    }
