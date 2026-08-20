"""Self-update awareness INSIDE the stateful strategist conversation.

This is deliberately NOT a separate refresh chatbot. It builds a compact
evidence pack from the last refresh + the newest internal evidence, and renders
conversational answers whose SHAPE depends on the dialogue act — so "did we find
anything new?" → "what actually matters?" → "why?" → "what should we shoot?" all
resolve against the same refresh, exactly like idea follow-ups do.

Facts come only from the run history and the existing evidence layers. External
references stay reference-only. Nothing is invented; when a number isn't tracked
we say so.
"""
from __future__ import annotations

from typing import Optional

import slack_response_style as st
from logger import get_logger

log = get_logger()

# What the user is asking ABOUT a refresh (a sub-topic, resolved by the agent).
NEW_FINDINGS = "refresh_new_findings"
WHAT_MATTERS = "refresh_what_matters"
WHY = "refresh_why"
ACT_ON_IT = "refresh_act_on_it"
EVIDENCE = "refresh_evidence"
META_GAP = "refresh_meta_gap"
STATUS = "refresh_status"

_NEW_KW = ("find anything new", "anything new", "find new", "new videos", "new reels",
           "new storelli", "did we find", "anything interesting", "new things",
           "brain find", "did the brain find", "brain found")
_WHAT_MATTERS_KW = ("what actually matters", "what matters", "which one matters",
                    "most important", "which matters most", "what's worth")
_ACT_KW = ("what should we shoot because", "what should we do with that",
           "what do we do with that", "what should we do about that",
           "shoot because of that", "act on that")
_STATUS_KW = ("brain up to date", "is the brain", "last refresh", "last update",
              "when did it refresh", "when did the brain", "refreshed",
              "did anything fail", "brain health", "when will it refresh",
              "next refresh", "what changed", "anything change", "did that pattern",
              "pattern get stronger", "profiles change", "winning profiles")
_META_KW = ("because meta", "meta isn't connected", "meta is not connected",
            "without meta", "missing because meta", "meta not connected",
            "no meta access", "meta connection")


def _lower(t) -> str:
    return " " + str(t or "").lower().strip() + " "


def detect_refresh_topic(text: str) -> str:
    """Which refresh sub-question this is, or '' when it isn't about the refresh."""
    t = _lower(text)
    if any(k in t for k in _META_KW):
        return META_GAP
    if any(k in t for k in _ACT_KW):
        return ACT_ON_IT
    if any(k in t for k in _WHAT_MATTERS_KW):
        return WHAT_MATTERS
    if any(k in t for k in _NEW_KW):
        return NEW_FINDINGS
    if any(k in t for k in _STATUS_KW):
        return STATUS
    return ""


# ---------------------------------------------------------------------------
# evidence pack
# ---------------------------------------------------------------------------
def _n(row, key) -> int:
    try:
        return int(str(row.get(key, "0") or 0))
    except (ValueError, TypeError):
        return 0


def build_refresh_pack(sheets=None) -> dict:
    """Compact pack: last run + the newest internal reels + strongest pattern.
    Fail-soft — an unreachable sheet yields an empty pack the renderer handles."""
    import intelligence_refresh as ir
    pack = {"run": None, "new_reels": [], "external_added": 0, "quality_80": 0,
            "profiles_changed": False, "regen": False, "top_pattern": "",
            "meta_missing": [], "health": ""}
    try:
        runs = ir.last_runs(sheets, n=1)
        pack["run"] = runs[0] if runs else None
    except Exception:  # noqa: BLE001
        pass
    try:
        pack["meta_missing"] = ir.missing_because_no_meta()
    except Exception:  # noqa: BLE001
        pass
    r = pack["run"] or {}
    pack["new_internal"] = _n(r, "INTERNAL_NEW_MEDIA")
    pack["analyzed"] = _n(r, "INTERNAL_ANALYZED")
    pack["external_added"] = _n(r, "EXTERNAL_ADDED")
    pack["quality_80"] = _n(r, "EXTERNAL_QUALITY_80")
    pack["profiles_changed"] = _n(r, "PROFILES_UPDATED") > 0
    pack["regen"] = str(r.get("IDEA_REGEN_RECOMMENDED", "")).strip().lower() in ("true", "1", "yes")
    pack["when"] = r.get("FINISHED_AT") or r.get("STARTED_AT") or ""
    pack["status"] = r.get("STATUS", "")
    pack["failed"] = _n(r, "FAILED_COUNT")
    # newest internal evidence + the pattern it reinforces (from winning profiles)
    try:
        pack["new_reels"], pack["top_pattern"] = _recent_internal_evidence(sheets)
    except Exception as e:  # noqa: BLE001
        log.info("refresh pack: internal evidence unavailable: %s", e)
    return pack


def _recent_internal_evidence(sheets=None) -> tuple:
    """(recent internal reels, strongest pattern label). Reads the existing
    brain — winning profiles are internal PROOF."""
    from inspiration_sheets import InspirationSheets
    s = sheets or InspirationSheets()
    profiles = [p for p in s.read_profiles()
                if str(p.get("ACTIVE", "")).strip().lower() == "true"]
    profiles.sort(key=lambda p: str(p.get("CONFIDENCE", "")).lower() == "high", reverse=True)
    top = profiles[0] if profiles else {}
    pattern = ""
    if top:
        hooks = str(top.get("HOOK_TAGS", "")).split(",")[0].strip()
        fmt = str(top.get("FORMAT_TAGS", "")).split(",")[0].strip()
        prod = str(top.get("PRODUCT", "")).strip()
        if hooks or fmt:
            pattern = f"{hooks} + {fmt}".strip(" +") + (f" for {prod}" if prod else "")
    reels = []
    for u in str(top.get("SUPPORTING_VIDEO_URLS", "")).replace("\n", ";").split(";"):
        u = u.strip()
        if u.startswith("http"):
            reels.append({"link": u, "label": pattern or "internal proof"})
    return reels[:3], pattern


# ---------------------------------------------------------------------------
# renderers — shape varies by sub-question (Part 15: no fixed template)
# ---------------------------------------------------------------------------
def _sources(pack: dict, n: int = 1) -> str:
    rows = []
    for i, r in enumerate(pack.get("new_reels", [])[:n], 1):
        rows.append((f"S{i}", r["link"], f"Storelli internal proof — {r['label'][:38]}"))
    return st.compact_sources(rows) if rows else ""


def _no_run_yet() -> str:
    return ("No automatic refresh has landed yet, so nothing new has come in through it. "
            "Once the weekly run is on I'll be able to tell you exactly what changed.")


def render(topic: str, pack: dict, mode: str = st.MODE_DEFAULT,
           focus_index: Optional[int] = None) -> str:
    """Conversational answer for a refresh sub-question. Short by default; no
    forced Why/My move/Sources scaffolding."""
    run = pack.get("run")
    if topic == META_GAP:
        missing = pack.get("meta_missing") or []
        if not missing:
            return ("Nothing — the private Instagram Insights connection is in place, so we get "
                    "saves and reach on top of the public numbers.")
        return ("Only private Insights — things like " + ", ".join(missing[:4]) +
                ". The core brain still updates from public Storelli performance data "
                "(views, likes, comments), so learnings and profiles keep moving without it.")

    if not run:
        return _no_run_yet()

    new_i, ext = pack["new_internal"], pack["external_added"]
    pattern = pack.get("top_pattern") or "the pattern we've been tracking"
    src = _sources(pack)

    if topic == NEW_FINDINGS:
        if not new_i and not ext:
            return (f"Nothing new since {pack['when']} — no new Storelli reels and no new "
                    "external references. The evidence base is unchanged.")
        bits = []
        if new_i:
            bits.append(f"{new_i} new Storelli reel{'s' if new_i != 1 else ''}")
        if ext:
            bits.append(f"{ext} new external reference{'s' if ext != 1 else ''}")
        body = f"Yeah — {' and '.join(bits)} came in."
        if new_i:
            body += (f" The interesting part is they reinforce {pattern} rather than opening "
                     "something new.")
            if not pack["profiles_changed"]:
                body += " I wouldn't call it a new pattern yet, but the evidence got stronger."
        elif ext:
            body += " All external, so it's execution reference only — no change to our proof."
        return st.compact_slack_response(body + (("\n\n" + src) if src and new_i else ""), mode)

    if topic == WHAT_MATTERS:
        if not new_i:
            return (f"Honestly, not much — the {ext} new items were all external references, "
                    "so they're useful for execution but they don't move our proof.")
        reels = pack.get("new_reels") or []
        idx = focus_index or 0
        which = reels[idx] if idx < len(reels) else (reels[0] if reels else None)
        ordinal_ask = focus_index is not None and focus_index > 0
        body = (f"The ones that matter are the reels landing on {pattern} — that's where our "
                "internal proof already is, so each one tightens the same case rather than "
                "spreading us thin.")
        if ordinal_ask and idx >= len(reels):
            body += (f" I only have {len(reels)} reel(s) linked as proof from that refresh, "
                     "so I can't single out that one specifically.")
        elif which:
            body += " That's the thread I'd follow."
        return st.compact_slack_response(body + (("\n\n" + src) if src else ""), mode)

    if topic == WHY:
        body = (f"Because {pattern} is the only territory where we have repeat internal "
                "evidence — the new reels sit on it, so they add weight to a proven line "
                "instead of testing something unproven.")
        if not pack["profiles_changed"]:
            body += (" It didn't cross the bar for a new winning profile though, which is why "
                     "I'm not calling it a new pattern.")
        return st.compact_slack_response(body + (("\n\n" + src) if src else ""), mode)

    if topic == ACT_ON_IT:
        body = (f"Shoot another execution of {pattern} — same structure, different pain moment, "
                "so we're compounding evidence rather than starting over.")
        if pack["regen"]:
            body += " The evidence moved enough that regenerating the idea pool is worth it now."
        else:
            body += (" I wouldn't regenerate the whole idea pool yet — the existing rated ideas "
                     "already cover this territory.")
        return st.compact_slack_response(body + (("\n\n" + src) if src else ""), mode)

    if topic == EVIDENCE:
        lines = [f"Here's what the last refresh ({pack['when']}) actually changed:"]
        lines.append(f"• Internal: {new_i} new reel(s), {pack['analyzed']} analyzed"
                     + (", winning profiles updated" if pack["profiles_changed"]
                        else ", no new winning profile"))
        lines.append(f"• External: {ext} added, {pack['quality_80']} high-quality "
                     "(reference only — never proof)")
        lines.append(f"• Pattern with the most support: {pattern}")
        return st.compact_slack_response("\n".join(lines) + (("\n\n" + src) if src else ""),
                                         st.MODE_DEEP)

    # STATUS — short recap, not a table
    fail = f" {pack['failed']} stage(s) failed." if pack.get("failed") else ""
    body = (f"Last refresh {pack['when']} ({pack['status']}).{fail} "
            f"{new_i} new Storelli reel(s), {ext} new external reference(s)"
            + (", winning profiles moved." if pack["profiles_changed"]
               else ", no winning-profile change."))
    if not pack["regen"]:
        body += " Not worth regenerating the idea pool yet."
    return st.compact_slack_response(body, mode)
