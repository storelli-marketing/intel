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
                  tests: list[dict], tests_ready: bool = False,
                  dashboard_url: str = "", notion_url: str = "") -> str:
    lines = [
        "*Storelli Marketing Brain Update*",
        "",
        f"Videos analyzed: {videos_analyzed}",
        f"Total tagged videos: {total_tagged}",
        f"New learnings generated: {new_learnings}",
        f"Notion updated: {'yes' if notion_updated else 'no'}",
        "",
        "Top winning signals:",
    ]
    if winning:
        lines += [f"{i}. {x}" for i, x in enumerate(winning[:3], 1)]
    else:
        lines.append("—")

    lines += ["", "Top weak signals:"]
    if weak:
        lines += [f"{i}. {x}" for i, x in enumerate(weak[:3], 1)]
    else:
        lines.append("—")

    lines += ["", "Next creative tests:"]
    if not tests_ready or not tests:
        lines.append("No strong creative tests yet — more tagged videos needed.")
    else:
        for i, t in enumerate(tests[:3], 1):
            lines += [
                f"{i}. Test: {t.get('test', '')}",
                f"   Product: {t.get('product', '')}",
                f"   ICP: {t.get('icp', '')}",
                f"   Execution: {t.get('execution', '')}",
            ]

    lines += ["", f"Open Notion: {notion_url or '(not set)'}",
              f"Open Dashboard: {dashboard_url or '(not set)'}"]
    return "\n".join(lines)


def post(text: str) -> int:
    if not config.SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL not configured")
    resp = httpx.post(config.SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    resp.raise_for_status()
    return resp.status_code
