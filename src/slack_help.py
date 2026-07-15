"""Slack capability menu + developer routing diagnostics.

`capability_menu()` is a short, CEO-readable "what can you do?" menu grouped by
user type. `route_debug()` is a developer-only introspection (triggered by the
literal token "route_debug"/"route debug" in a message) that reports which Slack
route WOULD handle a prompt — without running the handler or writing anything.

Read-only: this module never touches Sheets, Notion, or the network.
"""
from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Part A — capability / help menu
# ---------------------------------------------------------------------------
_HELP_PHRASES = (
    "what can you do", "what can you help", "what can marketing brain",
    "what can the marketing brain", "what can the brain do", "how should i use you",
    "how do i use you", "how can you help", "what do you do", "what should the team ask",
    "what should i ask you", "what are your capabilities", "your capabilities",
    "capability menu", "what can u do",
)
_HELP_EXACT = ("help", "help.", "help!", "help?", "menu", "commands", "?")

_PRODUCT_HINT = ("bodyshield", "leggings", "pants", "glove", "slider", "exoshield",
                 "head guard", "jersey")


def is_help_query(text: str) -> bool:
    t = re.sub(r"\s+", " ", str(text or "").lower()).strip()
    if not t:
        return False
    if t.strip("?.! ") in {p.strip("?.! ") for p in _HELP_EXACT}:
        return True
    if any(p in t for p in _HELP_PHRASES):
        return True
    # "show me examples" is the menu ONLY when it's not asking for example *videos*
    # of a specific product (that's the existing example-retrieval path).
    if "show me examples" in t and "video" not in t and not any(p in t for p in _PRODUCT_HINT):
        return True
    return False


def capability_menu() -> str:
    return (
        "*What I can help with* — grouped by who's asking:\n\n"
        "*1. Strategy / CEO*\n"
        "_“What should we shoot this week?” · “What are the most urgent tests and why?” · "
        "“Where is the evidence thin?”_\n\n"
        "*2. Social manager*\n"
        "_“What should we revise in the calendar?” · “What content is most likely to get "
        "comments?” · “What should we post more of?”_\n\n"
        "*3. Creative strategist*\n"
        "_“What storytelling structure should this idea use?” · “Which videos should we take "
        "inspiration from?” · “How do we make this less generic?”_\n\n"
        "*4. Shooter / creator*\n"
        "_“Turn this into a shoot brief.” · “What should Gerald film?” · “What are the shot "
        "beats?”_\n\n"
        "*5. Notion idea evaluator*\n"
        "_“Evaluate this idea: <Notion URL>” · “Dry run this idea: <Notion URL>” · “What "
        "should I tell the team?”_\n\n"
        "Best way to use me: ask one concrete content decision at a time."
    )


# ---------------------------------------------------------------------------
# Part C — routing diagnostics (developer-only)
# ---------------------------------------------------------------------------
def is_route_debug(text: str) -> bool:
    t = str(text or "").lower()
    return "route_debug" in t or "route debug" in t


def _clean(text: str) -> str:
    # Strip the trigger token so detectors see the real question.
    return re.sub(r"route[_ ]debug", " ", str(text or ""), flags=re.IGNORECASE).strip()


def _ctx_family(context: Optional[list]) -> bool:
    try:
        import semantic_connections as sc
    except Exception:  # noqa: BLE001
        return False
    for m in (context or []):
        if sc._family(m.get("text", "")):
            return True
    return False


def route_debug(text: str, context: Optional[list] = None) -> str:
    """Report the route/intent/context/LLM a prompt WOULD take. Never runs a
    handler; never writes. Mirrors social_brain.answer_conversation precedence."""
    q = _clean(text)
    context = context or []
    route = intent = "—"
    ctx_resolved = "n/a"
    llm = "no (deterministic)"
    sources = "resolved at answer time"

    def done(r, i, c="n/a", l="no (deterministic)", s="resolved at answer time"):
        return r, i, c, l, s

    if is_help_query(q):
        route, intent, ctx_resolved, llm, sources = done("help_menu", "capability_menu")
    elif any(k in q.lower() for k in ("source debug", "sources you used", "debug sources")):
        route, intent, ctx_resolved, llm, sources = done("source_debug", "source_debug",
                                                          s="the exact [S#]/[E#]/[N#] last cited")
    else:
        try:
            import adhoc_idea_evaluator as ev
            import notion_idea_ingest as ni
            import semantic_connections as sc
            import social_strategy_skills as strat
            import slack_conversation_orchestrator as orch
            import calendar_retrieval as cret
            import idea_retrieval as ir
        except Exception:  # noqa: BLE001
            return "route_debug unavailable (import error)."

        if ev.is_evaluation_query(q, context):
            has_url = bool(ni.find_notion_url(q))
            route = "adhoc_notion_evaluator"
            intent = "dry_run_evaluate" if ev.is_dry_run(q) else ("evaluate" if has_url else "followup")
            ctx_resolved = "yes" if (has_url or ev._prior_eval_url(context) or ev._EVAL_CACHE) else "no"
            llm = "yes (narrative only; validated)"
            sources = "[N#] idea · [S#] proof · [C#] connection · [E#] reference · [I#] similar idea"
        elif sc.is_inspiration_query(q):
            route = "semantic_inspiration"
            intent = "inspiration_references"
            ctx_resolved = "yes" if (sc._family(q) or _ctx_family(context)) else "no"
            llm = "optional (deterministic when a connection is stored)"
            sources = "[S#] internal proof · [E#] external reference"
        elif strat.is_strategy_query(q, context):
            route = "social_strategy_skill"
            intent = strat.detect_skill(q, context)
            det_skills = {"comment_drivers", "test_hypothesis", "concept_references", "shot_brief"}
            llm = "no (deterministic)" if intent in det_skills else "yes (validated; deterministic fallback)"
            needs_ctx = intent in ("test_hypothesis", "concept_references", "idea_diagnosis", "shot_brief")
            ctx_resolved = ("yes" if (sc._family(q) or _ctx_family(context)) else "no") if needs_ctx else "n/a"
            sources = "[S#] proof · [C#] connection · [E#] reference · [N#] calendar"
        else:
            memory = {"last_recommended_idea_ids": []}
            oi = orch.detect_intent(q, memory)
            if oi in ("urgent_tests", "compare_ideas", "idea_deep_dive"):
                route = "rag_orchestrator"
                intent = oi
                llm = "yes when a reasoning trigger is present (validated; deterministic fallback)"
                ctx_resolved = ("yes" if (_ctx_family(context) or "this idea" in q.lower()
                                          or "you suggested" in q.lower()) else "no") \
                    if oi in ("idea_deep_dive", "compare_ideas") else "n/a"
                sources = "[S#] internal proof · [E#] external reference"
            elif cret.is_calendar_query(q):
                route, intent = "calendar_retrieval", "calendar_lookup"
                sources = "[N#] calendar item"
            elif ir.is_idea_query(q):
                route, intent = "idea_retrieval", "idea_lookup"
                sources = "[S#] internal proof · [E#] external reference"
            else:
                route, intent = "strategist_or_deterministic", "fallback_question"
                llm = "yes if strategist mode + GEMINI key (validated; deterministic fallback)"

    return (
        "*route_debug* (developer view — not shown to users):\n"
        f"• route: `{route}`\n"
        f"• intent: `{intent}`\n"
        f"• context_resolved: {ctx_resolved}\n"
        f"• llm_synthesis: {llm}\n"
        f"• likely_sources: {sources}"
    )
