"""Slack bot — inbound events + outbound chat.postMessage.

Read-only companion to `slack_report.py`:
- `slack_report.py` = one-way outbound run summaries via incoming webhook.
- `slack_bot.py`    = interactive app_mention / DM / active-thread-reply →
                      reply, backed by `social_brain.answer_conversation()`.

Verifies Slack request signatures against SLACK_SIGNING_SECRET (HMAC SHA256,
5-minute freshness window). Never writes to the Sheet, never writes to
Notion, never triggers video analysis — read/synthesize only.

Conversation memory: the primary source of context is live Slack thread
history (`fetch_thread_context`, best-effort — returns None on any failure or
missing scope so the caller can fall back cleanly). A small in-memory-only
cache (`remember` / `cached_context` / `is_active_thread`) supplements this —
it's what lets a thread be recognized as "the bot is already participating
here" even without history-read scopes, and it resets on restart by design
(no database, per the conversational-mode guardrails).
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

# --- bot identity (cached) --------------------------------------------------
_bot_id_cache: dict = {"id": None, "fetched": False}


def get_bot_user_id() -> str | None:
    """Best-effort, cached bot user id via auth.test. None on any failure —
    callers must treat that as 'unknown' rather than crash."""
    if _bot_id_cache["fetched"]:
        return _bot_id_cache["id"]
    _bot_id_cache["fetched"] = True
    if not config.SLACK_BOT_TOKEN:
        return None
    try:
        resp = httpx.post(f"{_SLACK_API}/auth.test",
                          headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                          timeout=10)
        data = resp.json()
        if data.get("ok"):
            _bot_id_cache["id"] = data.get("user_id")
    except Exception as e:  # noqa: BLE001 - identity lookup is best-effort
        log.warning("slack_bot: could not resolve bot user id: %s", e)
    return _bot_id_cache["id"]


# --- lightweight in-memory conversation cache -------------------------------
# Keyed by (channel, thread_key) where thread_key is the thread_ts, or the
# channel id itself for un-threaded DMs. Never persisted; resets on restart.
_THREAD_CACHE: dict[tuple, list[dict]] = {}
_MAX_CACHE_MESSAGES = 10
_MAX_TRACKED_THREADS = 500  # simple bound so this can't grow unbounded


def _mem_key(channel: str, thread_ts: str) -> tuple:
    return (channel, thread_ts or channel)


def is_active_thread(channel: str, thread_ts: str) -> bool:
    """True if we've already participated in this thread this process's
    lifetime — used to allow plain follow-up replies (no re-mention needed)
    without listening to unrelated channel chatter."""
    return _mem_key(channel, thread_ts) in _THREAD_CACHE


def remember(channel: str, thread_ts: str, role: str, text: str) -> None:
    """Append a turn to the per-thread cache (role is 'user' or 'assistant')."""
    key = _mem_key(channel, thread_ts)
    if key not in _THREAD_CACHE and len(_THREAD_CACHE) >= _MAX_TRACKED_THREADS:
        _THREAD_CACHE.pop(next(iter(_THREAD_CACHE)))  # drop oldest-inserted
    _THREAD_CACHE.setdefault(key, []).append({"role": role, "text": text})
    _THREAD_CACHE[key] = _THREAD_CACHE[key][-_MAX_CACHE_MESSAGES:]


def cached_context(channel: str, thread_ts: str) -> list[dict]:
    return list(_THREAD_CACHE.get(_mem_key(channel, thread_ts), []))


def fetch_thread_context(channel: str, thread_ts: str, limit: int = 10) -> list[dict] | None:
    """Best-effort live fetch of thread history via conversations.replies.

    Returns None (not []) on any failure or missing scope, so the caller can
    tell 'no history available' apart from 'empty thread' and fall back to
    the in-memory cache instead of silently answering with no context.
    """
    if not (config.SLACK_BOT_TOKEN and thread_ts):
        return None
    try:
        resp = httpx.get(f"{_SLACK_API}/conversations.replies",
                         headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
                         params={"channel": channel, "ts": thread_ts, "limit": limit},
                         timeout=10)
        data = resp.json()
        if not data.get("ok"):
            log.warning("slack_bot: conversations.replies unavailable (%s) — "
                        "falling back to in-memory thread cache.", data.get("error"))
            return None
    except Exception as e:  # noqa: BLE001 - history fetch is best-effort
        log.warning("slack_bot: fetch_thread_context failed: %s", e)
        return None

    bot_id = get_bot_user_id()
    out = []
    for m in (data.get("messages") or [])[-limit:]:
        role = "assistant" if (bot_id and m.get("user") == bot_id) else "user"
        out.append({"role": role, "text": strip_mention(m.get("text", ""))})
    return out


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


def update_message(channel: str, ts: str, text: str) -> bool:
    """chat.update — replace an existing message's text in place. Used to
    turn a "thinking..." progress message into the final answer without
    ever leaving a duplicate message behind. Best-effort: returns False (never
    raises) on any failure so a progress-UI glitch can't break the real answer."""
    if not (config.SLACK_BOT_TOKEN and channel and ts):
        return False
    try:
        resp = httpx.post(f"{_SLACK_API}/chat.update",
                          headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
                                  "Content-Type": "application/json; charset=utf-8"},
                          json={"channel": channel, "ts": ts, "text": text}, timeout=15)
        data = resp.json()
        if not data.get("ok"):
            log.warning("slack chat.update not ok: %s", data.get("error"))
        return bool(data.get("ok"))
    except Exception as e:  # noqa: BLE001 - progress UI is best-effort, never fatal
        log.warning("slack_bot: update_message failed: %s", e)
        return False


def set_assistant_status(channel: str, thread_ts: str, status: str) -> bool:
    """Best-effort: Slack's assistant.threads.setStatus — the native
    "thinking..." indicator for AI apps, needing the assistant:write scope.
    An empty status clears it. Returns False on any failure/missing scope
    (this app's default scopes don't include it) so the caller falls back to
    the message-update approach instead."""
    if not (config.SLACK_BOT_TOKEN and channel and thread_ts):
        return False
    try:
        resp = httpx.post(f"{_SLACK_API}/assistant.threads.setStatus",
                          headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}",
                                  "Content-Type": "application/json; charset=utf-8"},
                          json={"channel_id": channel, "thread_ts": thread_ts, "status": status},
                          timeout=10)
        data = resp.json()
        if not data.get("ok"):
            log.info("slack_bot: assistant.threads.setStatus unavailable (%s); "
                     "using message-update progress fallback.", data.get("error"))
        return bool(data.get("ok"))
    except Exception as e:  # noqa: BLE001 - progress UI is best-effort, never fatal
        log.info("slack_bot: assistant.threads.setStatus unavailable (%s); "
                 "using message-update progress fallback.", e)
        return False


class ProgressReporter:
    """Shows the user visible, public progress stages while an answer is
    being composed — short status summaries only ("checking Notion Brain"),
    never private chain-of-thought. Prefers Slack's native
    assistant.threads.setStatus (probed once, on the first call); falls back
    to posting one message and editing it in place (chat.postMessage +
    chat.update) so no duplicate "thinking" messages are ever left behind.
    Entirely best-effort — any Slack API hiccup here degrades silently
    without affecting whether the real answer gets posted."""

    def __init__(self, channel: str, thread_ts: str):
        self.channel = channel
        self.thread_ts = thread_ts
        self._mode: str | None = None  # "assistant" | "message" | None
        self._msg_ts: str | None = None

    def start(self, text: str) -> None:
        if set_assistant_status(self.channel, self.thread_ts, text):
            self._mode = "assistant"
            return
        self._mode = "message"
        try:
            data = post_message(self.channel, text, thread_ts=self.thread_ts)
            if data.get("ok"):
                self._msg_ts = data.get("ts")
        except Exception as e:  # noqa: BLE001 - progress UI is best-effort, never fatal
            log.warning("slack_bot: ProgressReporter.start failed to post: %s", e)

    def update(self, text: str) -> None:
        if self._mode == "assistant":
            set_assistant_status(self.channel, self.thread_ts, text)
        elif self._mode == "message" and self._msg_ts:
            update_message(self.channel, self._msg_ts, text)
        # mode is None (start() never got a message posted either) -> skip;
        # the final answer in finish()/fail() still gets posted as normal.

    def finish(self, final_text: str) -> None:
        if self._mode == "assistant":
            set_assistant_status(self.channel, self.thread_ts, "")
            post_message(self.channel, final_text, thread_ts=self.thread_ts)
        elif self._mode == "message" and self._msg_ts:
            update_message(self.channel, self._msg_ts, final_text)
        else:
            post_message(self.channel, final_text, thread_ts=self.thread_ts)

    def fail(self, short_reason: str) -> None:
        self.finish(f"I hit an error while answering. The backend is alive, but {short_reason}.")
