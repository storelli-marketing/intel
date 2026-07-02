"""Slack bot — inbound events + outbound chat.postMessage.

Read-only companion to `slack_report.py`:
- `slack_report.py` = one-way outbound run summaries via incoming webhook.
- `slack_bot.py`    = interactive app_mention → in-thread reply, backed by
                      `social_brain.answer_question()`.

Verifies Slack request signatures against SLACK_SIGNING_SECRET (HMAC SHA256,
5-minute freshness window). Never writes to the Sheet and never triggers
video analysis — read/synthesize only.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time

import httpx

import config
from logger import get_logger

log = get_logger()

_SIG_MAX_AGE_SEC = 60 * 5
_SLACK_API = "https://slack.com/api"


def verify_request(body: bytes, timestamp: str, signature: str,
                   *, now: float | None = None) -> bool:
    """True iff the Slack signature is valid AND the timestamp is fresh.

    Slack's scheme: v0=HMAC_SHA256(signing_secret, f"v0:{ts}:{raw_body}").
    """
    secret = config.SLACK_SIGNING_SECRET
    if not (secret and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > _SIG_MAX_AGE_SEC:
        return False

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    mac = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={mac}"
    return hmac.compare_digest(expected, signature)


def is_retry(headers) -> bool:
    """Slack retries deliveries when we don't 200 fast enough. We always ack
    immediately and process in the background, so retries are pure duplicates
    and should be ignored."""
    return bool((headers.get("x-slack-retry-num") or headers.get("X-Slack-Retry-Num")))


def strip_mention(text: str) -> str:
    """Remove leading <@BOT_ID> mention(s) and normalize whitespace."""
    if not text:
        return ""
    cleaned = re.sub(r"<@[UW][A-Z0-9]+>", "", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def post_message(channel: str, text: str, thread_ts: str | None = None) -> dict:
    """POST to chat.postMessage. Reply in-thread when thread_ts is provided.

    Returns the parsed JSON response. Never raises on Slack's `ok: false`
    payloads — the caller logs and moves on.
    """
    if not config.SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN not configured")
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    headers = {
        "Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    resp = httpx.post(f"{_SLACK_API}/chat.postMessage", json=payload,
                      headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        log.warning("slack chat.postMessage not ok: %s", data.get("error"))
    return data
