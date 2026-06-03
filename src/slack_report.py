"""Slack reporting layer.

Posts a plain-text run summary to SLACK_WEBHOOK_URL. build_message() is pure
(testable offline); post() does the HTTP call.
"""
from __future__ import annotations

import httpx

import config
from logger import get_logger

log = get_logger()


def build_message(*, videos_analyzed, total_tagged: int, new_learnings: int,
                  notion_updated: bool, winning: list[str], weak: list[str],
                  tests: list[str], dashboard_url: str = "", notion_url: str = "") -> str:
    def numbered(items):
        items = [i for i in items if i]
        return "\n".join(f"{n}. {x}" for n, x in enumerate(items[:3], 1)) or "—"

    return "\n".join([
        "*Storelli Marketing Brain Update*",
        "",
        f"Videos analyzed: {videos_analyzed}",
        f"Total tagged videos: {total_tagged}",
        f"New learnings generated: {new_learnings}",
        f"Notion updated: {'yes' if notion_updated else 'no'}",
        "",
        "Top winning signals:",
        numbered(winning),
        "",
        "Top weak signals:",
        numbered(weak),
        "",
        "Next creative tests:",
        numbered(tests),
        "",
        f"Dashboard: {dashboard_url or '(not set)'}",
        f"Notion: {notion_url or '(not set)'}",
    ])


def post(text: str) -> int:
    if not config.SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL not configured")
    resp = httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    resp.raise_for_status()
    return resp.status_code
