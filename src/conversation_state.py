"""Conversation State Manager for the Slack strategist UX.

Keeps a compact, structured memory per Slack conversation so follow-ups
("why those?", "the first one", "make it shorter") can be understood in context
instead of re-triggering a fresh retrieval.

Reconstruction priority (Part L):
  1. actual Slack thread history (the `context` list passed in — web.py already
     sources it from conversations.replies first, then the in-memory cache);
  2. this module's short-TTL cache (survives within a window, not across a long
     gap or a fresh unrelated topic);
  3. explicit entities in the current message;
  4. clarification when still unresolved (handled by the resolver).

We deliberately store compact structured memory + short recent text, never giant
raw payloads.
"""
from __future__ import annotations

import re
import time
from typing import Optional

# Non-threaded conversation memory TTL — 45 min (Part L: 30–60 min).
_TTL_SEC = 45 * 60
_MAX_TURNS = 10
_CACHE: dict = {}          # key -> {"state": dict, "ts": float}
_MAX_KEYS = 500


# ---------------------------------------------------------------------------
# state shape
# ---------------------------------------------------------------------------
def new_state() -> dict:
    """A blank structured state (compact; no raw payloads)."""
    return {
        "recent_user": [],            # last few user message texts
        "recent_assistant": [],       # last few assistant message texts (short)
        "last_intent": "",
        "last_dialogue_act": "",
        "last_recommended_idea_ids": [],
        "last_recommended_idea_titles": [],
        "last_compared_items": [],
        "last_evaluated_notion_page": "",
        "last_calendar_items": [],
        "last_product": "",
        "last_icp": "",
        "last_semantic_connection": "",
        "last_sources_used": [],
        "last_recommendation": "",
        "last_answer_summary": "",
        "last_unresolved_question": "",
        "conversation_topic": "",
        "response_mode": "normal",    # concise | normal | deep
        # self-update context (Part 16) — what refresh/change we're discussing
        "last_refresh_discussed": "",
        "new_internal_reels": [],     # compact: [{link, title_or_date}]
        "new_external_count": 0,
        "strongest_changed_pattern": "",
        "winning_profiles_changed": False,
        "last_refresh_recommendation": "",
        # ACTIVE DECISION FRAME (see decision_frame.py) — the compact record of
        # "what problem are we currently solving", so a follow-up phrased as a
        # new question doesn't restart from the global capability set.
        "decision_frame": None,
        "timestamp": 0.0,
    }


# ---------------------------------------------------------------------------
# key + cache (secondary layer; thread history is primary)
# ---------------------------------------------------------------------------
def conversation_key(thread_ts: str = "", channel: str = "", user: str = "") -> str:
    if thread_ts:
        return f"thread:{thread_ts}"
    return f"cu:{channel}:{user}"


def _expired(entry: dict) -> bool:
    return (time.time() - entry.get("ts", 0)) > _TTL_SEC


def get(key: str) -> Optional[dict]:
    entry = _CACHE.get(key)
    if not entry:
        return None
    if _expired(entry):
        _CACHE.pop(key, None)
        return None
    return dict(entry["state"])


def put(key: str, state: dict) -> None:
    if len(_CACHE) >= _MAX_KEYS:
        # drop the oldest
        oldest = min(_CACHE.items(), key=lambda kv: kv[1].get("ts", 0))[0]
        _CACHE.pop(oldest, None)
    state = dict(state)
    state["timestamp"] = time.time()
    _CACHE[key] = {"state": state, "ts": time.time()}


def clear(key: str) -> None:
    _CACHE.pop(key, None)


# ---------------------------------------------------------------------------
# entity extraction from the prior assistant turn (no ideas needed here)
# ---------------------------------------------------------------------------
_PRODUCTS = (("bodyshield", "BodyShield"), ("leggings", "Leggings"), ("pants", "Pants"),
            ("sliders", "Sliders"), ("gloves", "Gloves"), ("glove", "Gloves"),
            ("exoshield", "ExoShield"), ("head guard", "Head Guard"))
_ICPS = (("parent", "Parents"), ("aspiring pro", "Aspiring Pro"), ("adult amateur", "Adult Amateur"),
         ("general", "General"))


def detect_product(text: str) -> str:
    low = (text or "").lower()
    for kw, label in _PRODUCTS:
        if kw in low:
            return label
    return ""


def detect_icp(text: str) -> str:
    low = (text or "").lower()
    for kw, label in _ICPS:
        if kw in low:
            return label
    return ""


def _source_tags(text: str) -> list:
    return sorted(set(re.findall(r"\[([SECNI]\d+)\]", text or "")))


def build_state(context: Optional[list], text: str = "", key: str = "") -> dict:
    """Reconstruct state from Slack thread history (primary), merged over the
    cached state (secondary). Compact — recent turns + extracted entities."""
    ctx = context or []
    state = get(key) if key else None
    state = state or new_state()

    users = [m.get("text", "") for m in ctx if m.get("role") == "user"]
    assistants = [m.get("text", "") for m in ctx if m.get("role") == "assistant"]
    state["recent_user"] = [u[:400] for u in users[-_MAX_TURNS:]]
    state["recent_assistant"] = [a[:600] for a in assistants[-_MAX_TURNS:]]

    last_assistant = assistants[-1] if assistants else ""
    if last_assistant:
        state["last_answer_summary"] = (last_assistant.strip().splitlines() or [""])[0][:160]
        state["last_sources_used"] = _source_tags(last_assistant)
        prod = detect_product(last_assistant)
        icp = detect_icp(last_assistant)
        if prod:
            state["last_product"] = prod
            state["conversation_topic"] = state["conversation_topic"] or prod
        if icp:
            state["last_icp"] = icp
    return state


def last_assistant_text(context: Optional[list]) -> str:
    return next((m.get("text", "") for m in reversed(context or [])
                if m.get("role") == "assistant"), "")
