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


def _post_type_from_url(url: str) -> str:
    u = str(url or "")
    if "/reel/" in u:
        return "Reel"
    if "/tv/" in u:
        return "Video"
    if "/p/" in u:
        return "Carousel"
    return "Unknown"


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

    def fetch_post(self, url: str) -> InspirationPost:
        """Fetch metadata for a SINGLE post/reel URL (no profile enumeration).
        Providers that support the human-in-the-loop URL queue implement this."""
        raise NotImplementedError(
            f"{self.name} provider cannot fetch an individual post URL")


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
        return _post_type_from_url(url)

    def fetch_post(self, url: str) -> InspirationPost:
        """Single-URL metadata extraction (NOT flat, NOT profile enumeration).
        This is the reliable Instagram path with authenticated cookies."""
        try:
            import yt_dlp
        except ImportError as e:  # pragma: no cover - yt-dlp is a hard dep
            raise RuntimeError("yt-dlp not installed") from e

        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
        }
        if config.YTDLP_COOKIES_PATH:
            opts["cookiefile"] = config.YTDLP_COOKIES_PATH

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url.strip(), download=False)
        if not isinstance(info, dict):
            raise RuntimeError(f"no metadata returned for {url}")

        ts = info.get("timestamp")
        published = ""
        if isinstance(ts, (int, float)):
            published = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        webpage = str(info.get("webpage_url") or url).strip()
        return InspirationPost(
            post_id=str(info.get("id") or "").strip(),
            post_url=webpage,
            post_type=_post_type_from_url(webpage or url),
            published_at=published,
            caption=str(info.get("description") or info.get("title") or "").strip(),
            thumbnail_url=str(info.get("thumbnail") or "").strip(),
            duration_seconds=str(info.get("duration") or "").strip(),
            view_count=str(info.get("view_count") or "").strip(),
            like_count=str(info.get("like_count") or "").strip(),
            comment_count=str(info.get("comment_count") or "").strip(),
            scrape_status="Success",
            published_ts=float(ts) if isinstance(ts, (int, float)) else None,
        )

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


def post_is_duplicate(platform: str, post: InspirationPost,
                      existing: dict[str, set]) -> bool:
    """True if the post's SOURCE_ID, POST_ID, or POST_URL is already known."""
    sid = make_source_id(platform, post)
    pid = (post.post_id or "").strip()
    url = (post.post_url or "").strip()
    return bool((sid and sid in existing.get("SOURCE_ID", set()))
                or (pid and pid in existing.get("POST_ID", set()))
                or (url and url in existing.get("POST_URL", set())))


def remember_post(platform: str, post: InspirationPost,
                  existing: dict[str, set]) -> None:
    """Fold a post's dedup keys into the running `existing` snapshot in place."""
    sid = make_source_id(platform, post)
    if sid:
        existing.setdefault("SOURCE_ID", set()).add(sid)
    if (post.post_id or "").strip():
        existing.setdefault("POST_ID", set()).add(post.post_id.strip())
    if (post.post_url or "").strip():
        existing.setdefault("POST_URL", set()).add(post.post_url.strip())


def dedup_posts(platform: str, posts: list[InspirationPost],
                existing: dict[str, set]) -> tuple[list[InspirationPost], int]:
    """Drop posts whose SOURCE_ID, POST_ID, or POST_URL already exists (in the
    sheet OR earlier in this same batch). Returns (fresh_posts, skipped_count)."""
    seen = {k: set(v) for k, v in existing.items()}
    fresh, skipped = [], 0
    for p in posts:
        if post_is_duplicate(platform, p, seen):
            skipped += 1
            continue
        fresh.append(p)
        remember_post(platform, p, seen)
    return fresh, skipped


def post_to_row(channel: dict, post: InspirationPost, *, scraped_at: str) -> dict:
    """Build an INSPIRATION_CONTENT row dict. SOURCE_TYPE is ALWAYS
    EXTERNAL_INSPIRATION; analysis/match/score fields are left blank for later
    milestones."""
    platform = str(channel.get("PLATFORM", "") or "Instagram").strip()
    return {
        # Human curation context (blank for the channel-scan path; populated by
        # the URL queue). Written only where the content tab has these columns.
        "QUEUE_ID": str(channel.get("QUEUE_ID", "")).strip(),
        "ADDED_BY": str(channel.get("ADDED_BY", "")).strip(),
        "REASON_FOR_ADDING": str(channel.get("REASON_FOR_ADDING", "")).strip(),
        "TARGET_PRODUCT": str(channel.get("TARGET_PRODUCT", "")).strip(),
        "TARGET_ICP": str(channel.get("TARGET_ICP", "")).strip(),
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
# Orchestrators
# ---------------------------------------------------------------------------
def _new_run(run_type: str, provider_name: str) -> dict:
    return {
        "RUN_ID": _run_id(),
        "RUN_TYPE": run_type,
        "STARTED_AT": _now_iso(),
        "FINISHED_AT": "",
        "STATUS": "Running",
        "PROVIDER": provider_name,
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


def _finalize_and_log_run(sheets, run: dict, errors: list[str],
                          failed: int, total: int) -> dict:
    run["FINISHED_AT"] = _now_iso()
    if not failed:
        run["STATUS"] = "Completed"
    elif total and failed >= total:
        run["STATUS"] = "Failed"
    else:
        run["STATUS"] = "Partial"
    run["ERROR_SUMMARY"] = " | ".join(errors)[:1000]
    try:
        sheets.append_run(run)
    except Exception as e:  # noqa: BLE001 - logging the run must not break the run
        log.warning("Could not write INSPIRATION_RUNS row: %s", e)
    return run


def scan_channels(sheets: Optional[InspirationSheets] = None,
                  provider: Optional[InspirationProvider] = None) -> dict:
    """Scan every ACTIVE monitored channel and append new external posts.

    Returns a run-summary dict (also written to INSPIRATION_RUNS).
    """
    sheets = sheets or InspirationSheets()
    provider = provider or get_provider()
    cfg = sheets.read_config()

    run = _new_run("Scan", provider.name)

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
                remember_post(platform, p, existing)

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

    return _finalize_and_log_run(sheets, run, errors,
                                 failed=run["CHANNELS_FAILED"],
                                 total=run["CHANNELS_SCANNED"])


def process_queue(sheets: Optional[InspirationSheets] = None,
                  provider: Optional[InspirationProvider] = None) -> dict:
    """Human-in-the-loop URL queue: process each pending INSPIRATION_URL_QUEUE
    row by fetching that single post's metadata (yt-dlp + cookies, no profile
    enumeration) and appending it to INSPIRATION_CONTENT.

    Each queue row is marked Processed / Duplicate / Failed with PROCESSED_AT,
    SOURCE_ID and ERROR_MESSAGE. Returns a run-summary dict (also logged to
    INSPIRATION_RUNS with RUN_TYPE=Queue).
    """
    sheets = sheets or InspirationSheets()
    provider = provider or get_provider()

    # Make sure the tab and curation columns exist so a first run never crashes.
    try:
        sheets.ensure_queue_tab()
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_queue_tab failed (continuing): %s", e)
    try:
        from inspiration_sheets import CONTENT_CURATION_COLUMNS
        added = sheets.ensure_content_columns(CONTENT_CURATION_COLUMNS)
        if added:
            log.info("Added curation columns to INSPIRATION_CONTENT: %s", added)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_content_columns failed (continuing): %s", e)

    run = _new_run("Queue", provider.name)
    queued = sheets.read_queued_urls()
    log.info("Inspiration queue: %d pending URL(s), provider=%s",
             len(queued), provider.name)
    errors: list[str] = []
    existing = sheets.existing_content_keys()

    for q in queued:
        url = str(q.get("POST_URL", "")).strip()
        platform = "Instagram"
        run["POSTS_DISCOVERED"] += 1
        try:
            post = provider.fetch_post(url)
            if not (post.post_id or post.post_url):
                raise RuntimeError(f"no usable metadata for {url}")
            sid = make_source_id(platform, post)

            if post_is_duplicate(platform, post, existing):
                run["POSTS_SKIPPED_EXISTING"] += 1
                sheets.update_queue_row(
                    q["_row"], status="Duplicate", processed_at=_now_iso(),
                    source_id=sid, error_message="")
                log.info("  duplicate: %s (%s)", url, sid)
                continue

            row = post_to_row(_queue_channel(q), post, scraped_at=_now_iso())
            sheets.append_content_rows([row])
            remember_post(platform, post, existing)
            run["POSTS_ADDED"] += 1
            sheets.update_queue_row(
                q["_row"], status="Processed", processed_at=_now_iso(),
                source_id=sid, error_message="")
            log.info("  processed: %s -> %s", url, sid)
        except Exception as e:  # noqa: BLE001 - one bad URL must not abort the run
            run["POSTS_FAILED"] += 1
            msg = f"{url}: {type(e).__name__}: {e}"
            errors.append(msg)
            log.error("  queue item failed: %s", msg)
            try:
                sheets.update_queue_row(
                    q["_row"], status="Failed", processed_at=_now_iso(),
                    error_message=str(e)[:400])
            except Exception:  # noqa: BLE001
                pass

    return _finalize_and_log_run(sheets, run, errors,
                                 failed=run["POSTS_FAILED"],
                                 total=len(queued))


def _queue_channel(q: dict) -> dict:
    """Adapt a queue row into the pseudo-'channel' dict post_to_row expects.
    CHANNEL_HANDLE / MACRO_INDUSTRY / SUBCATEGORY are preserved onto the content
    row; TARGET_PRODUCT / TARGET_ICP / REASON_FOR_ADDING have no INSPIRATION_
    CONTENT column yet, so they stay on the queue row (linked by SOURCE_ID) for
    later milestones — nothing is lost."""
    handle = str(q.get("CHANNEL_HANDLE", "")).strip()
    return {
        "CHANNEL_ID": handle,
        "PLATFORM": "Instagram",
        "HANDLE": handle,
        "MACRO_INDUSTRY": str(q.get("MACRO_INDUSTRY", "")).strip(),
        "SUBCATEGORY": str(q.get("SUBCATEGORY", "")).strip(),
        # Human curation context preserved onto the content row.
        "QUEUE_ID": str(q.get("QUEUE_ID", "")).strip(),
        "ADDED_BY": str(q.get("ADDED_BY", "")).strip(),
        "REASON_FOR_ADDING": str(q.get("REASON_FOR_ADDING", "")).strip(),
        "TARGET_PRODUCT": str(q.get("TARGET_PRODUCT", "")).strip(),
        "TARGET_ICP": str(q.get("TARGET_ICP", "")).strip(),
    }


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


def print_queue_summary(run: dict) -> None:
    print("\nInspiration URL queue processed.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Provider:               {run.get('PROVIDER')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"URLs seen:              {run.get('POSTS_DISCOVERED')}")
    print(f"Added to content:       {run.get('POSTS_ADDED')}")
    print(f"Duplicates skipped:     {run.get('POSTS_SKIPPED_EXISTING')}")
    print(f"Failed:                 {run.get('POSTS_FAILED')}")
    if run.get("ERROR_SUMMARY"):
        print(f"Errors:                 {run.get('ERROR_SUMMARY')}")
