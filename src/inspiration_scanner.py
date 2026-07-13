"""Inspiration Layer — Milestone 1: monitored-channel ingestion.

Reads ACTIVE accounts from the MONITORED CHANNELS tab, pulls recent post
*metadata* from each (no media download, no Gemini, no taxonomy tagging yet),
deduplicates against what is already stored, and appends new rows to
INSPIRATION_CONTENT with SOURCE_TYPE=EXTERNAL_INSPIRATION. Every run is logged
to INSPIRATION_RUNS.

Explicitly OUT of scope for this milestone (do not add here yet):
  taxonomy analysis of external content, matching to internal learnings,
  idea generation, idea scoring, Slack changes.

Provider abstraction
--------------------
`InspirationProvider` is the seam. The MVP concrete provider
(`YtDlpInstagramProvider`) reuses the repo's already-working yt-dlp + cookie
setup to read profile metadata — the safest source already compatible with the
deployment. Swapping in a hosted scraper (e.g. Apify) later is just another
subclass; nothing else changes.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config
from inspiration_sheets import SOURCE_TYPE_EXTERNAL, InspirationSheets
from logger import get_logger

log = get_logger()

# Fallback defaults when neither the channel row nor INSPIRATION_CONFIG says.
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_MAX_POSTS_PER_SCAN = 20


# ---------------------------------------------------------------------------
# Post metadata model
# ---------------------------------------------------------------------------
@dataclass
class InspirationPost:
    """Normalized external post metadata (one Instagram post)."""
    post_id: str = ""
    post_url: str = ""
    post_type: str = ""          # Reel / Carousel / Image / Video / Unknown
    published_at: str = ""       # ISO8601 if known
    caption: str = ""
    thumbnail_url: str = ""
    media_url: str = ""
    duration_seconds: str = ""
    view_count: str = ""
    like_count: str = ""
    comment_count: str = ""
    scrape_status: str = "Success"   # Success / Partial / Failed
    published_ts: Optional[float] = field(default=None, repr=False)  # epoch, internal


def make_source_id(platform: str, post: InspirationPost) -> str:
    """Stable primary key for a post. Prefers platform:post_id, falls back to
    the post URL so a post is never keyless."""
    pid = (post.post_id or "").strip()
    if pid:
        return f"{platform.strip().lower()}:{pid}"
    return (post.post_url or "").strip()


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------
class InspirationProvider(abc.ABC):
    """Fetches recent post metadata for one channel. Read-only; must never
    download media or write anywhere."""

    name = "abstract"

    @abc.abstractmethod
    def fetch_recent_posts(self, *, handle: str, profile_url: str,
                           lookback_days: int, max_posts: int) -> list[InspirationPost]:
        ...


class YtDlpInstagramProvider(InspirationProvider):
    """MVP provider: yt-dlp flat metadata extraction over a profile URL, using
    the repo's existing authenticated cookie file. Never downloads media."""

    name = "ytdlp"

    def _ydl_opts(self) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
        }
        if config.YTDLP_COOKIES_PATH:
            opts["cookiefile"] = config.YTDLP_COOKIES_PATH
        return opts

    @staticmethod
    def _post_type(entry: dict) -> str:
        url = str(entry.get("url") or entry.get("webpage_url") or "")
        if "/reel/" in url:
            return "Reel"
        if "/tv/" in url:
            return "Video"
        return "Unknown"

    def fetch_recent_posts(self, *, handle: str, profile_url: str,
                           lookback_days: int, max_posts: int) -> list[InspirationPost]:
        try:
            import yt_dlp
        except ImportError as e:  # pragma: no cover - yt-dlp is a hard dep
            raise RuntimeError("yt-dlp not installed") from e

        target = profile_url.strip() or f"https://www.instagram.com/{handle.strip().lstrip('@')}/"
        cutoff = None
        if lookback_days and lookback_days > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - lookback_days * 86400

        with yt_dlp.YoutubeDL(self._ydl_opts()) as ydl:
            info = ydl.extract_info(target, download=False)
        entries = list(info.get("entries") or []) if isinstance(info, dict) else []

        posts: list[InspirationPost] = []
        for entry in entries:
            if len(posts) >= max_posts:
                break
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp")
            # Lookback filter only when we actually have a timestamp; flat
            # extraction often omits it, in which case we keep the post but
            # mark it Partial rather than silently dropping recent content.
            if cutoff is not None and isinstance(ts, (int, float)) and ts < cutoff:
                continue
            pid = str(entry.get("id") or "").strip()
            url = str(entry.get("url") or entry.get("webpage_url") or "").strip()
            if url and not url.startswith("http"):
                url = f"https://www.instagram.com/reel/{url}/"
            published = ""
            if isinstance(ts, (int, float)):
                published = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            has_meta = isinstance(ts, (int, float)) or entry.get("view_count") is not None
            posts.append(InspirationPost(
                post_id=pid,
                post_url=url,
                post_type=self._post_type(entry),
                published_at=published,
                caption=str(entry.get("title") or entry.get("description") or "").strip(),
                thumbnail_url=str(entry.get("thumbnail") or "").strip(),
                view_count=str(entry.get("view_count") or "").strip(),
                like_count=str(entry.get("like_count") or "").strip(),
                comment_count=str(entry.get("comment_count") or "").strip(),
                duration_seconds=str(entry.get("duration") or "").strip(),
                scrape_status="Success" if has_meta else "Partial",
                published_ts=float(ts) if isinstance(ts, (int, float)) else None,
            ))
        return posts


_PROVIDERS = {
    "ytdlp": YtDlpInstagramProvider,
}


def get_provider(name: Optional[str] = None) -> InspirationProvider:
    key = (name or config.INSPIRATION_PROVIDER or "ytdlp").strip().lower()
    cls = _PROVIDERS.get(key)
    if not cls:
        raise RuntimeError(
            f"Unknown inspiration provider {key!r}. Available: "
            f"{', '.join(sorted(_PROVIDERS))}.")
    return cls()


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without any network)
# ---------------------------------------------------------------------------
def _int(value, default: int) -> int:
    try:
        n = int(str(value).strip())
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def resolve_limits(channel: dict, cfg: dict) -> tuple[int, int]:
    """Effective (lookback_days, max_posts) for a channel: per-row value first,
    then INSPIRATION_CONFIG defaults, then hard-coded fallbacks."""
    lookback = _int(channel.get("LOOKBACK_DAYS"),
                    _int(cfg.get("DEFAULT_LOOKBACK_DAYS"), DEFAULT_LOOKBACK_DAYS))
    max_posts = _int(channel.get("MAX_POSTS_PER_SCAN"),
                     _int(cfg.get("DEFAULT_MAX_POSTS_PER_SCAN"), DEFAULT_MAX_POSTS_PER_SCAN))
    return lookback, max_posts


def dedup_posts(platform: str, posts: list[InspirationPost],
                existing: dict[str, set]) -> tuple[list[InspirationPost], int]:
    """Drop posts whose SOURCE_ID, POST_ID, or POST_URL already exists (in the
    sheet OR earlier in this same batch). Returns (fresh_posts, skipped_count)."""
    seen_sid = set(existing.get("SOURCE_ID", set()))
    seen_pid = set(existing.get("POST_ID", set()))
    seen_url = set(existing.get("POST_URL", set()))
    fresh, skipped = [], 0
    for p in posts:
        sid = make_source_id(platform, p)
        pid = (p.post_id or "").strip()
        url = (p.post_url or "").strip()
        if (sid and sid in seen_sid) or (pid and pid in seen_pid) \
                or (url and url in seen_url):
            skipped += 1
            continue
        fresh.append(p)
        if sid:
            seen_sid.add(sid)
        if pid:
            seen_pid.add(pid)
        if url:
            seen_url.add(url)
    return fresh, skipped


def post_to_row(channel: dict, post: InspirationPost, *, scraped_at: str) -> dict:
    """Build an INSPIRATION_CONTENT row dict. SOURCE_TYPE is ALWAYS
    EXTERNAL_INSPIRATION; analysis/match/score fields are left blank for later
    milestones."""
    platform = str(channel.get("PLATFORM", "") or "Instagram").strip()
    return {
        "SOURCE_ID": make_source_id(platform, post),
        "CHANNEL_ID": str(channel.get("CHANNEL_ID", "")).strip(),
        "PLATFORM": platform,
        "HANDLE": str(channel.get("HANDLE", "")).strip(),
        "POST_ID": post.post_id,
        "POST_URL": post.post_url,
        "POST_TYPE": post.post_type,
        "PUBLISHED_AT": post.published_at,
        "CAPTION": post.caption,
        "THUMBNAIL_URL": post.thumbnail_url,
        "MEDIA_URL": post.media_url,
        "DURATION_SECONDS": post.duration_seconds,
        "VIEW_COUNT": post.view_count,
        "LIKE_COUNT": post.like_count,
        "COMMENT_COUNT": post.comment_count,
        "SCRAPED_AT": scraped_at,
        "SCRAPE_STATUS": post.scrape_status,
        "ANALYSIS_STATUS": "Not Analyzed",
        # The single most important invariant of this whole layer:
        "SOURCE_TYPE": SOURCE_TYPE_EXTERNAL,
        "MACRO_INDUSTRY": str(channel.get("MACRO_INDUSTRY", "")).strip(),
        "SUBCATEGORY": str(channel.get("SUBCATEGORY", "")).strip(),
        "SHORTLISTED": "FALSE",
        "LAST_UPDATED_AT": scraped_at,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_id() -> str:
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def scan_channels(sheets: Optional[InspirationSheets] = None,
                  provider: Optional[InspirationProvider] = None) -> dict:
    """Scan every ACTIVE monitored channel and append new external posts.

    Returns a run-summary dict (also written to INSPIRATION_RUNS).
    """
    sheets = sheets or InspirationSheets()
    provider = provider or get_provider()
    cfg = sheets.read_config()

    started = _now_iso()
    run = {
        "RUN_ID": _run_id(),
        "RUN_TYPE": "Scan",
        "STARTED_AT": started,
        "FINISHED_AT": "",
        "STATUS": "Running",
        "PROVIDER": provider.name,
        "CHANNELS_SCANNED": 0,
        "CHANNELS_FAILED": 0,
        "POSTS_DISCOVERED": 0,
        "POSTS_ADDED": 0,
        "POSTS_SKIPPED_EXISTING": 0,
        "POSTS_FAILED": 0,
        "POSTS_ANALYZED": 0,       # not this milestone
        "POSTS_SHORTLISTED": 0,    # not this milestone
        "NOTION_PAGES_CREATED": 0,  # not this milestone
        "ERROR_SUMMARY": "",
    }

    channels = sheets.read_active_channels()
    log.info("Inspiration scan: %d active channel(s), provider=%s",
             len(channels), provider.name)
    errors: list[str] = []

    # Snapshot existing keys once; dedup_posts also dedups within this batch.
    existing = sheets.existing_content_keys()

    for ch in channels:
        handle = str(ch.get("HANDLE", "")).strip()
        profile_url = str(ch.get("PROFILE_URL", "")).strip()
        platform = str(ch.get("PLATFORM", "") or "Instagram").strip()
        lookback, max_posts = resolve_limits(ch, cfg)
        label = handle or profile_url or ch.get("CHANNEL_ID", "?")
        run["CHANNELS_SCANNED"] += 1
        try:
            posts = provider.fetch_recent_posts(
                handle=handle, profile_url=profile_url,
                lookback_days=lookback, max_posts=max_posts)
            run["POSTS_DISCOVERED"] += len(posts)

            fresh, skipped = dedup_posts(platform, posts, existing)
            run["POSTS_SKIPPED_EXISTING"] += skipped

            scraped_at = _now_iso()
            rows = [post_to_row(ch, p, scraped_at=scraped_at) for p in fresh]
            added = sheets.append_content_rows(rows)
            run["POSTS_ADDED"] += added

            # Fold this channel's new keys into the running snapshot so a later
            # channel in the same run cannot re-add the same post.
            for p in fresh:
                sid = make_source_id(platform, p)
                if sid:
                    existing["SOURCE_ID"].add(sid)
                if p.post_id:
                    existing["POST_ID"].add(p.post_id)
                if p.post_url:
                    existing["POST_URL"].add(p.post_url)

            last_pid = fresh[-1].post_id if fresh else str(ch.get("LAST_POST_ID", ""))
            sheets.update_channel_status(
                ch["_row"], last_scanned_at=scraped_at, last_post_id=last_pid,
                scan_status="Completed", error_message="")
            log.info("  %s: discovered=%d added=%d skipped=%d",
                     label, len(posts), added, skipped)
        except Exception as e:  # noqa: BLE001 - one bad channel must not abort the run
            run["CHANNELS_FAILED"] += 1
            run["POSTS_FAILED"] += 1
            msg = f"{label}: {type(e).__name__}: {e}"
            errors.append(msg)
            log.error("  scan failed for %s", msg)
            try:
                sheets.update_channel_status(
                    ch["_row"], last_scanned_at=_now_iso(),
                    scan_status="Failed", error_message=str(e)[:400])
            except Exception:  # noqa: BLE001
                pass

    run["FINISHED_AT"] = _now_iso()
    run["STATUS"] = "Completed" if not run["CHANNELS_FAILED"] else (
        "Failed" if run["CHANNELS_FAILED"] == run["CHANNELS_SCANNED"] and channels
        else "Partial")
    run["ERROR_SUMMARY"] = " | ".join(errors)[:1000]

    try:
        sheets.append_run(run)
    except Exception as e:  # noqa: BLE001 - logging the run must not break the run
        log.warning("Could not write INSPIRATION_RUNS row: %s", e)

    return run


def print_scan_summary(run: dict) -> None:
    print("\nInspiration scan complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Provider:               {run.get('PROVIDER')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Channels scanned:       {run.get('CHANNELS_SCANNED')}")
    print(f"Channels failed:        {run.get('CHANNELS_FAILED')}")
    print(f"Posts discovered:       {run.get('POSTS_DISCOVERED')}")
    print(f"Posts added:            {run.get('POSTS_ADDED')}")
    print(f"Posts skipped (dupes):  {run.get('POSTS_SKIPPED_EXISTING')}")
    if run.get("ERROR_SUMMARY"):
        print(f"Errors:                 {run.get('ERROR_SUMMARY')}")
