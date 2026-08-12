"""Response Planner — choose the answer SHAPE before rendering.

Keeps the bot from forcing the same "Why / My move / Sources" template onto
every turn (Part G/H). Maps a dialogue act (+ resolved referent) to one of a few
shapes; the renderer varies structure accordingly.
"""
from __future__ import annotations

import conversation_resolver as R

# shapes
DIRECT = "direct_reply"
EXPLANATION = "explanation"
COMPARISON = "comparison"
EVIDENCE = "evidence_answer"
QUICK = "quick_followup"
SHOOT_BRIEF = "shoot_brief"
INSPIRATION = "inspiration_answer"
ANALYTICS = "analytics_answer"
CLARIFY = "clarification"
LIST = "structured_list"
COMPRESS = "compress"

_ACT_TO_SHAPE = {
    R.EXPLAIN: EXPLANATION,
    R.COMPARE: COMPARISON,
    R.EVIDENCE: EVIDENCE,
    R.DEEPER: EXPLANATION,
    R.SHORTER: COMPRESS,
    R.CHALLENGE: DIRECT,
    R.MODIFY: EXPLANATION,
    R.OPERATIONALIZE: SHOOT_BRIEF,
    R.INSPIRATION: INSPIRATION,
    R.SUMMARY: DIRECT,
    R.CONFIRM: DIRECT,
}


def plan_shape(dialogue_act: str, resolved: dict) -> str:
    if resolved.get("clarify_needed"):
        return CLARIFY
    return _ACT_TO_SHAPE.get(dialogue_act, DIRECT)
