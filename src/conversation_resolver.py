"""Context / Referent Resolver + Dialogue-Act classifier.

Turns a follow-up message + the prior turn into (a) what the user wants to DO
conversationally (dialogue act) and (b) which prior thing they mean (referent).
Resolution uses the prior ASSISTANT output (the exact items shown), not just the
prior user text — the core fix for "why you proposed me these ideas?".
"""
from __future__ import annotations

import re
from typing import Optional

# Dialogue acts (Part D).
ASK_NEW = "ask_new_question"
EXPLAIN = "explain_previous_answer"
CHALLENGE = "challenge_previous_answer"
EVIDENCE = "request_evidence"
DEEPER = "request_more_depth"
SHORTER = "request_less_detail"
COMPARE = "compare_previous_items"
MODIFY = "modify_previous_idea"
OPERATIONALIZE = "operationalize_previous_answer"
EXAMPLES = "request_examples"
INSPIRATION = "request_inspiration"
CONFIRM = "confirm_understanding"
FOLLOWUP = "ask_followup"
CORRECT = "correct_agent"
SUMMARY = "request_summary"
RESET = "reset_topic"

_ORDINAL = {"first": 0, "1st": 0, "#1": 0, "one": 0, "second": 1, "2nd": 1, "#2": 1,
            "third": 2, "3rd": 2, "#3": 2}
_REF_PLURAL = ("these", "those", "them", "the ideas", "these ideas", "those ideas",
               "the three", "all three", "all of them")
_REF_SINGULAR = ("it", "that one", "this one", "that idea", "this idea", "the one",
                 "the first", "the second", "the third")


def _t(text: str) -> str:
    return " " + (text or "").lower().strip() + " "


# ---------------------------------------------------------------------------
# dialogue act
# ---------------------------------------------------------------------------
def classify_dialogue_act(text: str) -> str:
    t = _t(text)
    # reset first — an explicit topic change dominates.
    if is_reset(text):
        return RESET
    # compare before explain (a 'why X over Y' is a comparison, not a plain why).
    if any(k in t for k in ("compare", " vs ", " versus ", "which is better", "better idea")) \
            or (("#1" in t or "#2" in t or "first" in t or "second" in t)
                and any(k in t for k in (" over ", " vs ", " versus ", " instead of ",
                                         " rather than ", " ahead of ", " better "))) \
            or ("why" in t and any(k in t for k in (" over ", " before ", " instead of ",
                                                    " rather than ", " ahead of "))):
        return COMPARE
    if any(k in t for k in ("shoot brief", "shot brief", "shoot-brief", "content brief",
                            "into a brief", "how do we film", "how do we shoot",
                            "shot list", "shot beats")):
        return OPERATIONALIZE
    if any(k in t for k in ("what videos", "which videos", "inspiration for", "what should we watch",
                            "references for", "videos should we use", "videos to watch",
                            "take inspiration")):
        return INSPIRATION
    # Narrow, explicit proof-requests only — NOT a broad "the evidence" (which
    # would swallow content-gap questions like "where is the evidence thin?").
    if any(k in t for k in ("show me proof", "show me the evidence", "show the evidence",
                            "show evidence", "give me the evidence", "the proof",
                            "is that proven", "is that actually proven", "prove it",
                            "back that up", "what's the proof", "where's the proof",
                            "is this proven", "proven or just", "just interesting",
                            "proven or interesting")):
        return EVIDENCE
    if any(k in t for k in ("are you sure", "you sure", "really?", "is that right", "i doubt",
                            "not convinced", "is that true", "sure about that",
                            "change your mind", "would change my mind", "what would change")):
        return CHALLENGE
    if any(k in t for k in ("shorter", "tldr", "tl;dr", "too long", "make it short", "quick version",
                            "in a sentence", "just the gist", "keep it short", "one line")):
        return SHORTER
    if any(k in t for k in ("go deeper", "more depth", "why exactly", "tell me more", "expand",
                            "elaborate", "dig in", "more detail")):
        return DEEPER
    if any(k in t for k in ("make it for", "for parents", "for aspiring", "for adult",
                            "make it more", "what if we make it", "what about parents",
                            "more parent", "parent-facing", "parent facing")):
        return MODIFY
    if any(k in t for k in ("show me examples", "give me examples", "examples of")):
        return EXAMPLES
    if any(k in t for k in ("summarize", "recap", "tl;dr the", "sum up")):
        return SUMMARY
    if any(k in t for k in ("that's wrong", "thats wrong", "incorrect", "not what i", "you misunderstood")):
        return CORRECT
    if t.strip() in ("got it", "thanks", "thank you", "ok", "okay", "cool", "makes sense"):
        return CONFIRM
    # a bare/anchored "why" that references the prior turn -> explain
    if "why" in t and (any(w in t for w in _REF_PLURAL + _REF_SINGULAR)
                       or any(w in t for w in ("you propose", "you suggest", "you recommend",
                                               "did you", "these", "those", "them"))
                       or t.strip() in ("why", "why?", "why though", "why though?")):
        return EXPLAIN
    return ASK_NEW


_RESET_RE = re.compile(r"\b(forget (those|that|it|them)|never ?mind|nvm|scratch that|"
                       r"start over|reset|new topic|different (topic|question)|"
                       r"let'?s move on)\b", re.IGNORECASE)


def is_reset(text: str) -> bool:
    return bool(_RESET_RE.search(text or ""))


# ---------------------------------------------------------------------------
# referent resolution — uses prior assistant items (idea ids/titles in memory)
# ---------------------------------------------------------------------------
def resolve_referents(text: str, memory: dict, ideas: list) -> dict:
    """Resolve 'these ideas'/'the first one'/'it' to the prior recommended items.

    Returns {referent_type, idea_ids, idea_records, index, confidence,
    clarify_needed}. referent_type in {idea_set, single_idea, none}.
    """
    t = _t(text)
    ids = memory.get("last_recommended_idea_ids") or []
    titles = memory.get("last_recommended_idea_titles") or []
    byid = {i.get("IDEA_ID"): i for i in ideas}
    records = [byid[i] for i in ids if i in byid]

    out = {"referent_type": "none", "idea_ids": [], "idea_records": [], "index": None,
           "confidence": 0.0, "clarify_needed": False, "by_ordinal": False, "titles": titles}
    if not records:
        return out

    # an explicit ordinal reference -> that exact item
    m = re.search(r"(#[123]\b|\b(?:first|second|third|1st|2nd|3rd)\b)", t)
    if m:
        idx = _ORDINAL.get(m.group(1).strip())
        if idx is not None and idx < len(records):
            out.update(referent_type="single_idea", idea_ids=[ids[idx]],
                       idea_records=[records[idx]], index=idx, confidence=0.95,
                       by_ordinal=True)
            return out
    if any(w in t for w in _REF_SINGULAR) and len(records) == 1:
        out.update(referent_type="single_idea", idea_ids=[ids[0]],
                   idea_records=[records[0]], index=0, confidence=0.8)
        return out
    if any(w in t for w in _REF_PLURAL) or "ideas" in t:
        out.update(referent_type="idea_set", idea_ids=list(ids),
                   idea_records=records, confidence=0.9)
        return out
    if any(w in t for w in _REF_SINGULAR):
        # ambiguous 'it' with multiple prior items -> default to the top one, low conf
        out.update(referent_type="single_idea", idea_ids=[ids[0]], idea_records=[records[0]],
                   index=0, confidence=0.55)
        return out
    # a plain "why?" after a recommendation -> explain the whole set
    out.update(referent_type="idea_set", idea_ids=list(ids), idea_records=records,
               confidence=0.7)
    return out


def clarify(memory: dict) -> str:
    titles = memory.get("last_recommended_idea_titles") or []
    if len(titles) >= 2:
        return (f"Do you mean the ideas I just recommended — *{titles[0]}*, *{titles[1]}*"
                + (f", *{titles[2]}*" if len(titles) >= 3 else "") + "? Say which and I'll dig in.")
    if titles:
        return f"Do you mean *{titles[0]}*? Say the word and I'll break it down."
    return ""
