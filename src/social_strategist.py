"""Strategist synthesis layer for the Slack conversational bot.

Retrieval happens first, always via the existing deterministic modes in
`social_brain.py` (Notion-first, Sheet-fallback, the ideas engine) — this
module never queries Notion/Sheets/Gemini-video-analysis itself and never
writes anywhere. Its only job is to hand Gemini an already-curated, already-
cited evidence pack (never raw/endless data) and ask for a strategist's
judgment instead of a data dump, then validate the result before it's ever
shown to a user.

Public API:
  build_context_pack(user_text, conversation_context) -> dict
  compose_strategic_answer(user_text, conversation_context, retrieved_context) -> str | None

`compose_strategic_answer` returns None whenever strategist mode is disabled,
Gemini isn't configured, the call fails, or the output fails validation
(a cited [S#] id not in the evidence pack, a number not present in the
evidence, or causal language) — callers always have a deterministic answer
to fall back to.
"""
from __future__ import annotations

import re

from logger import get_logger

log = get_logger()

_CAUSAL_WORDS = ("causes", "caused by", "causing", "leads to", "results in", "because of the")


# --- evidence resolution: reuse the existing deterministic modes -----------
def _run_mode(mode: str, text: str) -> str:
    """Delegate to the matching deterministic mode in social_brain.py to
    produce the (already Notion-first, already-cited) evidence text."""
    import social_brain as sb
    dispatch = {
        "ideas": sb._mode_ideas,
        "feedback": sb._mode_feedback,
        "learnings": lambda t: sb._mode_learnings(),
        "tests": lambda t: sb._mode_tests(),
        "signals": sb._mode_signals,
        "examples": sb._mode_examples,
        "summary": lambda t: sb._mode_summary(),
    }
    fn = dispatch.get(mode, sb._mode_ideas)
    try:
        return fn(text)
    except Exception as e:  # noqa: BLE001 - evidence gathering must never crash the turn
        log.warning("social_strategist: mode %s failed (%s); falling back to answer_question", mode, e)
        return sb.answer_question(text)


def _parse_sources(text: str) -> dict:
    """Extract {id: description} from whichever citation style a mode used —
    '_Sources:_ [S1] desc · [S2] desc' (most modes), the multi-line
    '*Sources:*\\n  S1 [type] label' block (ideas mode), or the plain
    'Sources: [S1], [S2]' line a strategist answer renders.

    Finds every [S#] token by position first, then takes the span up to the
    NEXT [S#] token (not a greedy till-separator match) as its description —
    a single greedy regex would swallow "[S2]" into "[S1]"'s capture on a
    comma-joined "Sources: [S1], [S2]" line, silently losing S2 as a valid id.
    """
    out: dict[str, str] = {}
    tokens = list(re.finditer(r"\[S(\d+)\]", text))
    for i, m in enumerate(tokens):
        sid = f"S{m.group(1)}"
        end = tokens[i + 1].start() if i + 1 < len(tokens) else len(text)
        desc = re.split(r"[·\n]", text[m.end():end])[0].strip(" ,:-—")
        out.setdefault(sid, desc or sid)
    for m in re.finditer(r"^\s*S(\d+)\s+(.+)$", text, re.MULTILINE):
        out.setdefault(f"S{m.group(1)}", m.group(2).strip())
    return out


def _resolve_topic_and_evidence(user_text: str, conversation_context: list) -> tuple:
    """Returns (evidence_markdown, sources_map, mode). Follow-ups reuse the
    same topic/evidence as the previous turn (re-derived live, since the
    retrieval itself is deterministic and cheap) rather than trying to recall
    exact prior facts from memory — this is what lets 'why?' or 'what are you
    least sure about?' justify the SAME recommendation instead of drifting."""
    import social_brain as sb

    text = (user_text or "").strip()
    context = conversation_context or []
    last_assistant = next((m["text"] for m in reversed(context) if m.get("role") == "assistant"), "")
    follow_up = sb._classify_followup(text) if last_assistant else "none"

    if follow_up == "sources":
        return last_assistant, _parse_sources(last_assistant), "sources"

    if follow_up in ("expand", "brief", "risky", "shorter"):
        # Gemini reads the previous message directly (no regex needed) to
        # resolve "#2" / "that" — more robust than a rigid numbered-list parse.
        mode = sb._detect_last_mode(last_assistant)
        return last_assistant, _parse_sources(last_assistant), mode

    if follow_up == "make_for":
        evidence = sb._followup_make_for(text, context)
        return evidence, _parse_sources(evidence), "ideas"

    if follow_up == "more":
        evidence = sb._followup_more(context)
        return evidence, _parse_sources(evidence), sb._detect_last_mode(last_assistant)

    if follow_up == "next":
        evidence = sb._mode_tests()
        return evidence, _parse_sources(evidence), "tests"

    if follow_up == "why":
        mode = sb._detect_last_mode(last_assistant)
        evidence = _run_mode(mode, text)
        return evidence, _parse_sources(evidence), mode

    # Fresh question, or a follow-up phrasing our keyword classifier doesn't
    # recognize ("what are you least sure about?", "what would you do if you
    # were me?") — if there's prior context and the text doesn't match a known
    # topic, treat it as a continuation of the same conversation rather than
    # a dead end.
    mode = sb._route(text)
    if mode == "help" and last_assistant:
        mode = sb._detect_last_mode(last_assistant)
    evidence = _run_mode(mode, text)
    return evidence, _parse_sources(evidence), mode


def build_context_pack(user_text: str, conversation_context: list | None = None) -> dict:
    """Compact context pack: the user's question, a short thread summary, the
    already-retrieved (and already-cited) evidence for the resolved topic, and
    the canonical source id->description map used for citation validation."""
    context = conversation_context or []
    evidence, sources, mode = _resolve_topic_and_evidence(user_text, context)
    thread_summary = "\n".join(
        f"{m.get('role', 'user')}: {str(m.get('text', ''))[:300]}" for m in context[-6:]
    )
    return {
        "question": user_text,
        "mode": mode,
        "thread_summary": thread_summary,
        "evidence": evidence,
        "sources": sources,
    }


# --- validation --------------------------------------------------------------
def _has_causal_language(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in _CAUSAL_WORDS)


def _citations_valid(text: str, sources: dict) -> bool:
    cited = {f"S{n}" for n in re.findall(r"\[S(\d+)\]", text)}
    return cited.issubset(sources.keys())


def _numbers_grounded(text: str, evidence: str) -> bool:
    ev_nums = set(re.findall(r"\d+%|\bn=\d+\b", evidence))
    out_nums = set(re.findall(r"\d+%|\bn=\d+\b", text))
    return out_nums.issubset(ev_nums)


def _append_sources_if_missing(text: str, sources: dict) -> str:
    """Requirement: if the LLM drops all citations for what's otherwise a
    substantive, evidence-backed answer, append the source list manually
    rather than silently letting an uncited claim stand."""
    if re.search(r"\[S\d+\]", text) or not sources:
        return text
    top = list(sources.items())[:3]
    return text.rstrip() + "\n\nSources: " + ", ".join(f"[{sid}]" for sid, _ in top)


# --- public -------------------------------------------------------------------
_PROMPT_TEMPLATE = """\
You are a senior marketing strategist working inside Storelli's internal \
Marketing Brain, a Slack bot for a goalkeeper-protective-gear brand. Answer \
the user's message using ONLY the evidence pack below — give real judgment \
and tradeoffs, not a list of database rows. Never invent a fact, link, \
metric, or source.

Rules:
- Never say a signal "causes" or "leads to" performance — only "associated \
with" / "correlated with".
- If the evidence is thin (small sample, low confidence), say so explicitly \
and call the recommendation directional, not a rule.
- Cite ONLY the exact [S#] ids listed in "Available sources" below — never \
invent a new id, never cite one not listed. If you make a substantive claim, \
back it with a citation from that list.
- Do not invent any number, percentage, or metric that isn't already present \
in the evidence below.
- Use Slack mrkdwn: *single asterisks* for bold (never **double asterisks**), \
no markdown tables, no essay-length answers, 2-5 bullets typically.
- For a strategic/recommendation question, structure the answer as: "My \
read: ..." then 2-4 bullets covering what I'd do / why / what to avoid / \
next action, then "Sources: [S#], [S#]", then one short natural follow-up \
question.
- For a feedback-on-a-specific-video question, structure as: "Diagnosis: \
..." then bullets covering what worked/didn't, which signals it matched, \
what to change next time, and confidence level, then sources.
- For an ideas question, give 3-5 ideas max, each numbered exactly like \
"*1. Title*" followed by its hook / format / product-ICP / why-it-maps-to-\
the-evidence (this exact numbering is required so a later "expand #2" keeps \
working), then ask if they want one expanded.
- If the user's message is a follow-up ("why?", "expand #N", "make it for \
X", "what would you do if you were me?", "what are you least sure about?", \
etc.), resolve "that" / "this" / "#N" using the thread context below and \
answer conversationally in the same voice — don't re-explain the whole \
prior answer from scratch.

Available sources (cite ONLY these ids):
{source_list}

Evidence pack (already retrieved — this is the ONLY factual basis you may use):
{evidence}

Recent thread context:
{thread_summary}

User's message: {question}

Write only the reply text, no commentary about these instructions."""


def compose_strategic_answer(user_text: str, conversation_context: list | None,
                             retrieved_context: dict) -> str | None:
    """Compose a strategist-voice answer from an already-built context pack.
    Returns None (caller uses the deterministic fallback) when disabled, on
    any failure, or when the output fails citation/number/causal validation."""
    import config
    if not (config.SLACK_STRATEGIST_MODE_ENABLED and config.GEMINI_API_KEY):
        return None

    evidence = (retrieved_context or {}).get("evidence", "") or ""
    if not evidence.strip():
        return None
    sources = (retrieved_context or {}).get("sources", {}) or {}
    thread_summary = (retrieved_context or {}).get("thread_summary", "") or "(none)"
    source_list = "\n".join(f"[{sid}] {desc}" for sid, desc in sources.items()) or "(none)"

    prompt = _PROMPT_TEMPLATE.format(
        source_list=source_list, evidence=evidence,
        thread_summary=thread_summary, question=user_text,
    )

    try:
        from gemini_client import GeminiClient
        answer = GeminiClient().summarize_findings(prompt).strip()
    except Exception as e:  # noqa: BLE001 - strategist synthesis is optional, never fatal
        log.warning("strategist synthesis failed (%s); using deterministic fallback.", e)
        return None

    if not answer:
        return None
    # Defensive: normalize standard-Markdown bold to Slack's mrkdwn syntax
    # regardless of whether the model followed the prompt's instruction.
    answer = re.sub(r"\*\*(.+?)\*\*", r"*\1*", answer)

    if not _citations_valid(answer, sources):
        log.warning("strategist answer cited an unknown source id; using deterministic fallback.")
        return None
    if not _numbers_grounded(answer, evidence):
        log.warning("strategist answer introduced a number not in the evidence; using deterministic fallback.")
        return None
    if _has_causal_language(answer):
        log.warning("strategist answer used causal language; using deterministic fallback.")
        return None

    return _append_sources_if_missing(answer, sources)
