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


_CONCISE_KW = ("concise", "short", "tl;dr", "tldr", "quick", "top 3")


def _wants_concise(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _CONCISE_KW)


def build_context_pack(user_text: str, conversation_context: list | None = None) -> dict:
    """Compact context pack: the user's question, a short thread summary, the
    already-retrieved (and already-cited) evidence for the resolved topic, the
    canonical source id->description map used for citation validation, the
    Storelli brand/strategy context, and whether the user asked for brevity."""
    import content_context

    context = conversation_context or []
    evidence, sources, mode = _resolve_topic_and_evidence(user_text, context)
    thread_summary = "\n".join(
        f"{m.get('role', 'user')}: {str(m.get('text', ''))[:300]}" for m in context[-6:]
    )
    brand_context = ""
    try:
        brand_context = content_context.gather_context().get("brand_context", "") or ""
    except Exception as e:  # noqa: BLE001 - brand context is a nice-to-have, never fatal
        log.warning("social_strategist: brand context unavailable: %s", e)

    return {
        "question": user_text,
        "mode": mode,
        "thread_summary": thread_summary,
        "evidence": evidence,
        "sources": sources,
        "brand_context": brand_context,
        "concise": _wants_concise(user_text),
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


# Implementation details that must never leak into a user-facing answer —
# catching these is what keeps the bot feeling like a strategist rather than
# a retrieval tool describing its own backend.
_BACKEND_LEAK_PATTERNS = (
    r"notion\s+row", r"\bdatabase\b", r"retrieved\s+context",
    r"\bcontext\s+pack\b", r"\brow\s+\d+\b", r'\{\s*"',
)


def _has_backend_language(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in _BACKEND_LEAK_PATTERNS)


def _has_markdown_table(text: str) -> bool:
    return bool(re.search(r"^\s*\|.+\|.+\|", text, re.MULTILINE))


def _append_sources_if_missing(text: str, sources: dict) -> str:
    """Requirement: if the LLM drops all citations for what's otherwise a
    substantive, evidence-backed answer, append the source list manually
    rather than silently letting an uncited claim stand."""
    if re.search(r"\[S\d+\]", text) or not sources:
        return text
    top = list(sources.items())[:3]
    return text.rstrip() + "\n\nSources: " + ", ".join(f"[{sid}]" for sid, _ in top)


def _cap_sources_line(text: str, max_sources: int = 5) -> str:
    """Never show more than max_sources ids in a Sources line, even if more
    were legitimately cited inline — keeps the tail of the answer scannable."""
    def _cap(m: re.Match) -> str:
        ids = re.findall(r"\[S\d+\]", m.group(0))
        if len(ids) <= max_sources:
            return m.group(0)
        prefix = re.match(r"^(?:_Sources:_|Sources:)\s*", m.group(0)).group(0)
        return prefix + ", ".join(ids[:max_sources])

    return re.sub(r"(?:_Sources:_|Sources:)\s*\[S\d+\](?:\s*[,·]\s*\[S\d+\])*", _cap, text)


def _fix_empty_next_action(text: str) -> str:
    """Safety net: the prompt requires 'What I'd do next:' / 'Next move:' to
    always carry at least one action, but if a header ever slips through
    empty (e.g. the bullet budget left nothing for it), drop the bare header
    line rather than showing a blank section."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^(?:What I'd do next|Next move):\s*$", line.strip()):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            has_bullet = j < len(lines) and re.match(r"^\s*(?:[-•]|\d+\.)\s", lines[j])
            if not has_bullet:
                i += 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _cap_bullets(text: str, max_bullets: int) -> str:
    """Drop excess bullet/numbered-list lines beyond max_bullets, keeping
    every non-bullet line (headers, Sources, Confidence, the follow-up
    question) exactly where it is — a targeted trim, not a hard truncation
    that could cut off citations or the confidence line."""
    out, bullet_count = [], 0
    for line in text.splitlines():
        if re.match(r"^\s*(?:[-•]|\d+\.)\s", line):
            bullet_count += 1
            if bullet_count > max_bullets:
                continue
        out.append(line)
    return "\n".join(out)


# --- public -------------------------------------------------------------------
_PROMPT_TEMPLATE = """\
You are a senior marketing strategist working inside Storelli's internal \
Marketing Brain, a Slack bot. Answer the user's message using ONLY the \
evidence pack below — give real judgment, ranked conclusions, and tradeoffs, \
not a data dump. Never invent a fact, link, metric, or source. Never mention \
implementation details (Notion, databases, "retrieved context", row numbers, \
JSON) — the user only ever sees you as a strategist, not a retrieval tool.

Storelli brand & strategy context (use this to explain WHY something matters \
for Storelli specifically — product/ICP/positioning implications, not just \
signal names):
{brand_context}

Inference requirements:
- Collapse the evidence into at most 3-5 conclusions — rank them by how \
useful they are to act on, most useful first. Don't just list every signal.
- Every conclusion needs a "because" grounded in an actual source from the \
list below.
- Prefer product/ICP/action implications over raw signal names (e.g. "lean \
into BodyShield protection-proof content for parents" beats "Curiosity Gap \
hook, format Demo").
- If the sample is thin, call the conclusion directional, not a rule.
- If the evidence genuinely doesn't support a conclusion the user is asking \
for, say plainly "I don't have enough evidence for that yet" instead of \
stretching a thin signal into a confident claim.

Answer format — pick the one that matches the question:

For "biggest learnings" / "what's working" / "summarize" / general strategy \
questions, use exactly this shape:
My read: [one clear sentence]

Biggest learnings:
1. [Learning] — [why it matters for Storelli]. Source: [Sx]
2. [Learning] — [why it matters for Storelli]. Source: [Sy]
3. [Learning] — [why it matters for Storelli]. Source: [Sz]

What I'd do next:
- [action]
- [action]

Confidence: [Strong / Medium / Directional / Thin data] — [short reason]

"What I'd do next" must NEVER be left empty or header-only — if the bullet \
budget is tight, cut a learning before you cut the next action; there must \
always be at least one concrete action listed.

For an ideas question, use exactly this shape:
My read: [strategy in one sentence]
Ideas:
1. [Title] — Hook: ... Format: ... Why (ONE short sentence): ... Source: [Sx]
2. [Title] — Hook: ... Format: ... Why (ONE short sentence): ... Source: [Sy]
(3 ideas normally, up to 5 only if the user asked for more — numbered \
exactly like "1. Title" so a later "expand #2" keeps working; keep each \
idea to 2-3 lines total, this is a Slack message, not a brief)
Next move: ask me which one to expand.

For a feedback-on-a-specific-video question, use exactly this shape:
Diagnosis: [one sentence]
What worked / didn't:
- ...
What I'd change:
- ...
Confidence: [Strong / Medium / Directional / Thin data]
Sources: [Sx]

For any other follow-up ("why?", "expand #N", "make it for X", "what would \
you do if you were me?", "what are you least sure about?", "show me \
sources", etc.), resolve "that" / "this" / "#N" using the thread context \
below and answer conversationally in the SAME voice, using whichever shape \
above fits best — don't re-explain the whole prior answer from scratch.

Hard limits on every answer:
- Never say a signal "causes" or "leads to" performance — only "associated \
with" / "correlated with".
- Cite ONLY the exact [S#] ids listed in "Available sources" below — never \
invent one, never cite one not listed. Cite the 1-3 STRONGEST sources that \
actually support the answer — do not cite every source available. Prefer, \
in this order: a Great-performing internal video example, Signal Library / \
Marketing Learnings, Product/ICP Learnings, latest_learnings.md, guidelines \
(only when directly relevant).
- Do not invent any number, percentage, or metric not already in the \
evidence below.
- Under ~1500 characters unless the user explicitly asked for depth/detail.
- No markdown tables. Use Slack mrkdwn: *single asterisks* for bold (never \
**double asterisks**).
- At most 5 bullets and at most 5 cited sources, {concise_limit}
- One short natural follow-up question at the end, unless the shape above \
already ends with one ("Next move: ...").

Available sources (cite ONLY these ids):
{source_list}

Evidence pack (already retrieved — this is the ONLY factual basis you may use):
{evidence}

Recent thread context:
{thread_summary}

User's message: {question}

Write only the reply text, no commentary about these instructions."""

_CONCISE_DIRECTIVE = (
    "and the user explicitly asked for brevity — give AT MOST 2 learning/idea "
    "bullets (not 3+), skip long explanation, but the 'What I'd do next' / "
    "'Next move' section still always needs exactly one concrete action — "
    "cut a learning bullet before you ever cut that action."
)
_NORMAL_LIMIT = "whichever is fewer."


def compose_strategic_answer(user_text: str, conversation_context: list | None,
                             retrieved_context: dict) -> str | None:
    """Compose a strategist-voice answer from an already-built context pack.
    Returns None (caller uses the deterministic fallback) when disabled, on
    any failure, or when the output fails citation/number/causal/backend-
    language/table validation."""
    import config
    if not (config.SLACK_STRATEGIST_MODE_ENABLED and config.GEMINI_API_KEY):
        return None

    evidence = (retrieved_context or {}).get("evidence", "") or ""
    if not evidence.strip():
        return None
    sources = (retrieved_context or {}).get("sources", {}) or {}
    thread_summary = (retrieved_context or {}).get("thread_summary", "") or "(none)"
    brand_context = (retrieved_context or {}).get("brand_context", "") or "(none)"
    concise = bool((retrieved_context or {}).get("concise"))
    source_list = "\n".join(f"[{sid}] {desc}" for sid, desc in sources.items()) or "(none)"

    prompt = _PROMPT_TEMPLATE.format(
        brand_context=brand_context, source_list=source_list, evidence=evidence,
        thread_summary=thread_summary, question=user_text,
        concise_limit=_CONCISE_DIRECTIVE if concise else _NORMAL_LIMIT,
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
    if _has_backend_language(answer):
        log.warning("strategist answer leaked backend/implementation language; using deterministic fallback.")
        return None
    if _has_markdown_table(answer):
        log.warning("strategist answer used a markdown table; using deterministic fallback.")
        return None

    answer = _cap_bullets(answer, 3 if concise else 5)
    answer = _fix_empty_next_action(answer)
    answer = _cap_sources_line(answer, 5)
    return _append_sources_if_missing(answer, sources)
