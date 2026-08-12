"""Stateful Slack strategist agent — the contextual follow-up entrypoint.

Ties the pieces together:
  Slack event -> Conversation State -> Referent Resolver -> Dialogue-Act
  -> Evidence Pack -> (optional validated LLM reasoning) -> Response Planner
  -> Natural Renderer -> Memory Update.

Scope discipline: this layer ONLY handles genuine follow-ups to a prior IDEA
recommendation. For anything it doesn't own — a fresh question, a reset, an
inspiration/shoot-brief ask already handled elsewhere, or an eval follow-up — it
returns None so the existing (deterministic) routing handles the turn unchanged.
Any failure inside also returns None (fallback to the proven path).
"""
from __future__ import annotations

import re
from typing import Optional

import conversation_evidence as CE
import conversation_resolver as R
import conversation_response_planner as P
import conversation_state as CS
import slack_response_style as st
from logger import get_logger

log = get_logger()

# Debug snapshot of the last routing decision (for route_debug; never shown).
LAST_DEBUG: dict = {}


def route_info(text: str, context: Optional[list] = None, sheets=None) -> dict:
    """Read-only routing introspection for route_debug (Part Q). Runs classify +
    resolve WITHOUT rendering or loading anything heavy beyond the idea list."""
    ctx = context or []
    info = {"dialogue_act": R.classify_dialogue_act(text), "contextual_followup": "no"}
    la = CS.last_assistant_text(ctx)
    act = info["dialogue_act"]
    if not la:
        return info
    if act == R.RESET:
        info["contextual_followup"] = "reset"
        return info
    if act in (R.ASK_NEW, R.OPERATIONALIZE, R.INSPIRATION, R.EXAMPLES, R.SUMMARY,
               R.CONFIRM, R.CORRECT):
        return info
    try:
        ideas = _load_ideas(sheets)
        from slack_conversation_orchestrator import build_memory
        memory = _recover_memory(ctx, ideas)
    except Exception:  # noqa: BLE001
        return info
    if not memory.get("last_recommended_idea_ids"):
        return info
    resolved = R.resolve_referents(text, memory, ideas)
    records = resolved.get("idea_records") or []
    owned = True
    if act == R.DEEPER:
        owned = False
    if act == R.EXPLAIN and resolved["referent_type"] == "single_idea" \
            and not resolved.get("by_ordinal"):
        owned = False
    if act == R.EXPLAIN and resolved["referent_type"] == "idea_set" and len(records) < 2:
        owned = False
    if not owned:
        return info
    info.update(contextual_followup="yes",
                resolved_referents=[CE._title(r) for r in records],
                conversation_topic=memory.get("last_product", ""),
                evidence_pack_type=resolved["referent_type"],
                response_shape=P.plan_shape(act, resolved),
                LLM_used="optional (validated; deterministic fallback)", fallback_used="no")
    return info


def _load_ideas(sheets):
    if sheets is None:
        from inspiration_sheets import InspirationSheets
        sheets = InspirationSheets()
    return sheets.read_ideas()


def _recover_memory(ctx: list, ideas: list) -> dict:
    """Find the most recent assistant turn that was an idea RECOMMENDATION, even
    if it's a few turns back (the immediately-previous turn may be an explanation
    that doesn't re-list titles). This is what keeps context alive across a
    multi-turn thread."""
    from slack_conversation_orchestrator import build_memory
    assistant_idx = [i for i, m in enumerate(ctx) if m.get("role") == "assistant"]
    for i in reversed(assistant_idx):
        mem = build_memory(ctx[:i + 1], ideas)
        if mem.get("last_recommended_idea_ids"):
            return mem
    return build_memory(ctx, ideas)


def _connection_for(product: str, sheets):
    try:
        if sheets is None:
            from inspiration_sheets import InspirationSheets
            sheets = InspirationSheets()
        conns = sheets.read_semantic_connections()
    except Exception:  # noqa: BLE001
        return None
    fam = _family(product)
    for c in conns:
        if not fam or _family(c.get("PRODUCT", "")) == fam:
            return c
    return conns[0] if conns else None


def _family(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("bodyshield", "leggings", "pants", "slider", " leg")):
        return "leggings"
    if "glove" in t:
        return "gloves"
    if any(k in t for k in ("head guard", "exoshield")):
        return "head"
    return ""


def answer(text: str, context: Optional[list] = None, sheets=None, gemini="auto",
           key: str = "") -> Optional[str]:
    """Return a contextual follow-up answer, or None to defer to existing routing."""
    LAST_DEBUG.clear()
    ctx = context or []
    last_assistant = CS.last_assistant_text(ctx)
    if not last_assistant:
        return None                          # no prior turn -> not a follow-up

    act = R.classify_dialogue_act(text)
    LAST_DEBUG.update(dialogue_act=act, contextual_followup="no")

    # RESET: an explicit topic switch. Clear memory; if the new message names a
    # product, answer it FRESH (a new retrieval, not a follow-up); else defer.
    if act == R.RESET:
        CS.clear(key)
        prod = CS.detect_product(text)
        if prod:
            try:
                import idea_retrieval
                return idea_retrieval.answer_ideas(f"ideas for {prod}", sheets=sheets)
            except Exception:  # noqa: BLE001
                return None
        return None
    if act in (R.ASK_NEW, R.OPERATIONALIZE, R.INSPIRATION, R.EXAMPLES,
               R.SUMMARY, R.CONFIRM, R.CORRECT):
        # not owned here (new -> fresh routing; shoot-brief -> strategy skills;
        # inspiration -> semantic layer; examples/summary -> existing modes)
        return None

    try:
        ideas = _load_ideas(sheets)
    except Exception as e:  # noqa: BLE001
        log.warning("conversation_agent: idea load failed: %s", e)
        return None
    if not ideas:
        return None

    from slack_conversation_orchestrator import build_memory
    memory = _recover_memory(ctx, ideas)
    if not memory.get("last_recommended_idea_ids"):
        return None                          # prior turn wasn't an idea recommendation -> defer

    resolved = R.resolve_referents(text, memory, ideas)
    byid = {i.get("IDEA_ID"): i for i in ideas}
    all_records = [byid[i] for i in memory["last_recommended_idea_ids"] if i in byid]
    records = resolved.get("idea_records") or []
    if not records:
        # a follow-up that references ideas we can't pin down -> clarify (never guess)
        cq = R.clarify(memory)
        return cq or None
    shape = P.plan_shape(act, resolved)
    mode = st.detect_response_mode(text)
    product = memory.get("last_product") or str(records[0].get("PRODUCT", "")).strip()

    LAST_DEBUG.update(contextual_followup="yes",
                      resolved_referents=[CE._title(r) for r in records],
                      conversation_topic=product, response_shape=shape,
                      evidence_pack_type=resolved["referent_type"], LLM_used="no",
                      fallback_used="no")

    try:
        pack = _dispatch(act, resolved, records, all_records, product, mode, sheets, text,
                         last_assistant)
    except Exception as e:  # noqa: BLE001
        log.warning("conversation_agent dispatch failed: %s", e)
        return None
    if pack is None:
        return None
    if isinstance(pack, str):
        answer_text = pack
    else:
        answer_text = _maybe_llm(pack, act, mode, gemini) or pack["text"]

    _update_state(key, ctx, act, resolved, product, memory)
    return answer_text


def _dispatch(act, resolved, records, all_records, product, mode, sheets, text, last_assistant):
    rtype = resolved["referent_type"]
    # DEEPER on a single item is already handled well by the existing
    # orchestrator's deep-dive (numbered trace) — defer so we don't regress it.
    if act == R.DEEPER:
        return None
    if act == R.SHORTER:
        # compress the PRIOR answer (no new retrieval -> nothing new invented)
        import social_brain
        return social_brain._followup_shorter(last_assistant)
    if act == R.COMPARE:
        return _compare(all_records or records, text, mode)
    if act == R.EVIDENCE:
        return CE.evidence_answer(records, mode)
    if act == R.CHALLENGE:
        return CE.challenge_answer(records, mode)
    if act == R.MODIFY:
        new_icp = CS.detect_icp(text) or "Parents"
        return CE.modify_answer(records, new_icp, mode)
    # EXPLAIN:
    #  - an idea SET (the production-bug fix) -> the new set explanation;
    #  - a single item picked by an explicit ORDINAL ("why the first one") -> explain it;
    #  - a bare pronoun ("why should we shoot it") -> defer to the orchestrator
    #    deep-dive (keeps its established numbered-trace answer).
    if rtype == "single_idea":
        if resolved.get("by_ordinal"):
            return CE.explain_single(records[0], mode, deep=(mode == st.MODE_DEEP))
        return None
    if len(records) < 2:
        return None
    connection = _connection_for(product, sheets)
    return CE.explain_idea_set(records, product, connection, mode)


def _compare(records, text, mode):
    """Compare the two prior items named by ordinals ('why #1 over #2'), else the
    top two of the prior set."""
    if not records:
        return None
    idxs = []
    for m in re.finditer(r"(#[123]\b|\b(?:first|second|third|1st|2nd|3rd)\b)", " " + text.lower()):
        i = R._ORDINAL.get(m.group(1).strip())
        if i is not None and i < len(records) and i not in idxs:
            idxs.append(i)
    if len(idxs) >= 2:
        a, b = records[idxs[0]], records[idxs[1]]
    elif len(records) >= 2:
        a, b = records[0], records[1]
    else:
        return CE.explain_single(records[0], mode)
    from slack_conversation_orchestrator import _pack_compare, _finalize
    pack = _pack_compare(a, b, mode)
    return _finalize(pack, mode, None, mode == st.MODE_DEEP, use_llm=False)


# ---------------------------------------------------------------------------
# optional validated LLM narrative (Part F) — deterministic on any failure
# ---------------------------------------------------------------------------
_SYS = (
    "You are Storelli's senior social and creative strategist talking with a teammate in Slack. "
    "You have a validated evidence pack. Answer the teammate's actual conversational question in "
    "1-4 short sentences. Do NOT repeat a numbered list they already saw. Use ONLY the supplied "
    "evidence for facts. Internal Storelli evidence is proof; external inspiration is execution "
    "reference only (never say it proves anything). If proof is thin, say so. Sound like a sharp "
    "teammate, not a report.\n\nEvidence pack:\n<<FACTS>>\n\nYou may cite ONLY these source ids: "
    "<<IDS>>.\n\nReturn ONLY strict JSON: {\"answer\": str, \"sources_used\": [str]}")

_CAUSAL = re.compile(r"\b(causes|caused|leads to|results in|guarantees?)\b", re.IGNORECASE)
_EXT_PROOF = re.compile(r"(external|inspiration|reference)[^.]{0,40}\bprov(e|es|en|ing)\b", re.IGNORECASE)


def _maybe_llm(pack: dict, act: str, mode: str, gemini) -> Optional[str]:
    import config
    if not (config.SLACK_STRATEGIST_MODE_ENABLED and config.GEMINI_API_KEY):
        return None
    if act not in (R.EXPLAIN, R.CHALLENGE, R.MODIFY):
        return None
    try:
        from gemini_client import GeminiClient
        from analyzer import parse_model_json
        gem = GeminiClient() if gemini in ("auto", None) else gemini
        prompt = _SYS.replace("<<FACTS>>", pack.get("facts", "")).replace(
            "<<IDS>>", ", ".join(sorted(pack.get("source_ids", set()))) or "(none)")
        obj = parse_model_json(gem.summarize_findings(prompt))
    except Exception as e:  # noqa: BLE001
        log.info("conversation LLM synth failed -> deterministic: %s", e)
        return None
    if not isinstance(obj, dict):
        return None
    ans = str(obj.get("answer", "")).strip()
    used = obj.get("sources_used", [])
    if not ans or not isinstance(used, list):
        return None
    allowed = pack.get("source_ids", set())
    if any(str(u).strip() not in allowed for u in used):
        return None
    if _CAUSAL.search(ans) or _EXT_PROOF.search(ans.lower()):
        return None
    if len(ans.split()) > st.WORD_CAP[st.MODE_DEEP]:
        return None
    LAST_DEBUG["LLM_used"] = "yes"
    src = pack.get("src")
    tail = ("\n\n" + src.block()) if src and src.block() else ""
    return st.format_trace_answer(st.compact_slack_response(ans + tail, mode))


def _update_state(key, ctx, act, resolved, product, memory):
    if not key:
        return
    try:
        state = CS.build_state(ctx, key=key)
        state["last_dialogue_act"] = act
        state["last_recommended_idea_ids"] = memory.get("last_recommended_idea_ids", [])
        state["last_recommended_idea_titles"] = memory.get("last_recommended_idea_titles", [])
        state["last_product"] = product or state.get("last_product", "")
        state["conversation_topic"] = product or state.get("conversation_topic", "")
        CS.put(key, state)
    except Exception:  # noqa: BLE001
        pass
