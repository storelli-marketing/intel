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
from dataclasses import dataclass

from logger import get_logger

log = get_logger()

_CAUSAL_WORDS = ("causes", "caused by", "causing", "leads to", "results in", "because of the")


# --- normalized proof-link sources ------------------------------------------
@dataclass
class Source:
    """One piece of evidence, normalized for clean Slack citation regardless
    of where it came from (a Notion Signal Library row, a Sheet video example,
    a guideline file, ...). best_url() implements the priority order: a
    direct video/post URL, else a Notion page URL, else a fallback, else
    title-only (no fake links, ever)."""
    source_id: str
    display_title: str
    source_type: str = "learning"  # video|learning|signal|test|product_learning|icp_learning|guideline|backend_file
    source_url: str = ""
    notion_page_url: str = ""
    fallback_url: str = ""
    product: str = ""
    icp: str = ""
    confidence: str = ""
    short_evidence_summary: str = ""

    def best_url(self) -> str:
        return self.source_url or self.notion_page_url or self.fallback_url or ""

    def render(self) -> str:
        """Slack mrkdwn: '[S1] <url|title>' when a URL exists, else '[S1] title'
        — never a fake link."""
        title = (self.display_title or self.source_id)[:120]
        url = self.best_url()
        return f"[{self.source_id}] <{url}|{title}>" if url else f"[{self.source_id}] {title}"

    def debug_line(self) -> str:
        why = ("has a direct source URL" if self.source_url else
              "no direct URL — used the Notion page link" if self.notion_page_url else
              "no URL available — cited by title only")
        return (f"• {self.source_id} — {self.display_title}\n"
                f"   type: {self.source_type} | chosen URL: {self.best_url() or '(none)'} | "
                f"Notion page: {self.notion_page_url or '(none)'}\n"
                f"   why: {why}")


# Notion property names (priority order) that would hold a direct video/post
# URL if a database schema ever adds one — none of the 6 Marketing Brain
# databases currently do (see notion_brain.py's SCHEMAS), so today this only
# ever matches Sheet-sourced citations (which already embed the real IG link)
# or a future schema change; kept explicit so it Just Works if one is added.
_DIRECT_URL_PROPS = ("url", "source url", "video url", "link", "ig link",
                    "instagram url", "post url", "creative url", "posted url")

_TYPE_MAP = {
    "sheet_row": "video", "generated social ideas": "video",
    "learnings": "learning", "guideline": "guideline",
    "signal library": "signal", "marketing learnings": "learning",
    "next creative tests": "test", "product learnings": "product_learning",
    "icp learnings": "icp_learning",
}


def _classify_source_type(desc: str) -> str:
    d = desc.lower()
    for key, t in _TYPE_MAP.items():
        if key in d:
            return t
    return "learning"


_SLACK_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")


def _extract_slack_link(desc: str) -> tuple:
    """Recognize our OWN Slack mrkdwn '<url|label>' link syntax (what
    render_proof_section itself produces) so re-parsing a previous
    strategist answer on a later turn — e.g. for 'why?' or 'source debug' —
    round-trips cleanly instead of mangling the url/title via the generic
    URL regex, which doesn't know '|label>' isn't part of the URL."""
    m = _SLACK_LINK_RE.search(desc)
    return (m.group(1), m.group(2).strip()) if m else ("", "")


def _extract_url(desc: str) -> str:
    m = re.search(r"https?://\S+", desc)
    return m.group(0).rstrip(").,]") if m else ""


def _clean_title(desc: str) -> str:
    """Strip the URL and a leading bracketed type tag (e.g. '[sheet_row]')
    from a raw citation description, leaving a clean human title. A plain
    .strip() of bracket characters alone would only trim the outer edges and
    leave a stray ']' behind once the URL in front of it is gone — this
    removes the whole tag as a unit first."""
    title = re.sub(r"https?://\S+", "", desc)
    title = re.sub(r"^\s*\[\w+\]\s*", "", title)
    return title.strip(" —-()[],") or desc


def normalize_sources(sources: dict) -> list:
    """Turn the {id: description} map (parsed from already-rendered,
    already-cited evidence text — see _parse_sources) into structured Source
    objects. A description with an embedded Instagram/Sheet link becomes a
    direct source_url; one with an embedded notion.com link becomes a
    notion_page_url instead — priority order 1 (direct) then 3 (Notion page)
    from a single parse, since that's what the underlying evidence actually
    carries today (see _DIRECT_URL_PROPS docstring above)."""
    out = []
    for sid, desc in sources.items():
        slack_url, slack_title = _extract_slack_link(desc)
        if slack_url:
            url, title = slack_url, slack_title
        else:
            url, title = _extract_url(desc), _clean_title(desc)
        is_notion_page = "notion.com" in url
        out.append(Source(
            source_id=sid,
            display_title=title[:120],
            source_type=_classify_source_type(desc),
            source_url="" if is_notion_page else url,
            notion_page_url=url if is_notion_page else "",
            short_evidence_summary=desc[:200],
        ))
    return out


def select_strongest_sources(sources: list, want_more: bool = False) -> list:
    """Cite the 1-3 strongest by default (5 if the user asked for more) —
    "strongest" meaning most useful as proof: a direct video/post link first,
    then a Notion page link, then title-only. Stable otherwise (preserves the
    order sources were first cited in)."""
    limit = 5 if want_more else 3
    ranked = sorted(enumerate(sources),
                    key=lambda p: (0 if p[1].source_url else 1 if p[1].notion_page_url else 2, p[0]))
    return [s for _, s in ranked[:limit]]


def render_proof_section(sources: list) -> str:
    if not sources:
        return ""
    return "Proof:\n" + "\n".join(f"- {s.render()}" for s in sources)


_MORE_SOURCES_KW = ("more sources", "all sources", "show more sources", "more proof", "top 5")


def _wants_more_sources(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _MORE_SOURCES_KW)


def _strip_existing_sources_line(text: str) -> str:
    """Remove whatever trailing citation line the model rendered (any style)
    — the caller replaces it with a deterministically-built Proof: block with
    real, verified links, so the model's own rendering of it is discarded
    rather than left duplicated alongside the real one."""
    text = re.sub(r"\n*_Sources:_.*", "", text)
    text = re.sub(r"\n*Sources:\s*\[S\d+\](?:\s*[,·]\s*\[S\d+\])*\s*$", "", text)
    return text.rstrip()


def _restrict_inline_citations(text: str, allowed_ids: set) -> str:
    """Keep only the ids in allowed_ids within each inline 'Proof: [Sx], [Sy]'
    marker, dropping the marker entirely if none of its ids survive. Without
    this, capping the trailing Proof: block to the top 1-3/5 sources could
    leave an inline citation (e.g. "Proof: [S4]") with no matching entry at
    the bottom — a dangling reference the user can't resolve."""
    def _filter(m: re.Match) -> str:
        kept = [i for i in re.findall(r"\[S\d+\]", m.group(0))
               if i.strip("[]") in allowed_ids]
        return "Proof: " + ", ".join(kept) if kept else ""

    return re.sub(r"Proof:\s*\[S\d+\](?:\s*,\s*\[S\d+\])*", _filter, text)


def render_source_debug(user_text: str, conversation_context: list | None) -> str:
    """'source debug' / 'show me the sources you used' — raw normalized
    source detail (id, title, chosen URL, Notion page URL, why selected) for
    whatever was actually cited in the PREVIOUS answer. Deliberately does NOT
    go through build_context_pack/_resolve_topic_and_evidence — that machinery
    re-derives fresh evidence for a (possibly different) topic when asked a
    phrase it doesn't recognize as a specific follow-up, which would debug the
    wrong thing entirely. This reads directly off the prior message instead.
    Operator debugging only; normal answers never show this level of detail."""
    context = conversation_context or []
    last_assistant = next((m["text"] for m in reversed(context) if m.get("role") == "assistant"), "")
    if not last_assistant:
        return "I don't have a previous answer to debug sources from yet."
    sources = _parse_sources(last_assistant)
    if not sources:
        return "I didn't cite any sources in my last message to debug."
    norm = normalize_sources(sources)
    lines = ["*Source debug* (for operators — not shown in normal answers):"]
    lines.extend(s.debug_line() for s in norm)
    return "\n".join(lines)


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

    A given id can appear more than once — once as a bare inline "Proof: [Sx]"
    citation (no description, right before a newline) and again in the
    trailing Proof:/Sources: block with the real title/link. Keeps whichever
    occurrence has the longest (most substantive) description rather than the
    first one found, so a bare inline mention never shadows the real one.
    """
    out: dict[str, str] = {}
    tokens = list(re.finditer(r"\[S(\d+)\]", text))
    for i, m in enumerate(tokens):
        sid = f"S{m.group(1)}"
        end = tokens[i + 1].start() if i + 1 < len(tokens) else len(text)
        desc = re.split(r"[·\n]", text[m.end():end])[0].strip(" ,:-—")
        if desc and len(desc) > len(out.get(sid, "")):
            out[sid] = desc
        else:
            out.setdefault(sid, sid)
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


def build_context_pack(user_text: str, conversation_context: list | None = None,
                       progress_cb=None) -> dict:
    """Compact context pack: the user's question, a short thread summary, the
    already-retrieved (and already-cited) evidence for the resolved topic, the
    canonical source id->description map used for citation validation, the
    Storelli brand/strategy context, and whether the user asked for brevity.
    progress_cb(str), if given, is called with a short public stage name
    before the retrieval work — never private chain-of-thought."""
    import content_context

    if progress_cb:
        progress_cb("notion")

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


def _fix_empty_next_action(text: str) -> str:
    """Safety net: the prompt requires 'What I'd do next:' / 'Next move:' to
    always carry at least one action, but if a header ever slips through
    empty (e.g. the bullet budget left nothing for it), drop the bare header
    line rather than showing a blank section. The action itself may be a
    bullet OR a plain sentence (the contract's own example shows a plain
    one-line action, no bullet marker) — any non-blank next line counts as
    content; only truly nothing following the header counts as empty."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^(?:What I'd do next|Next move):\s*$", line.strip()):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            has_content = (j < len(lines) and lines[j].strip() != "" and
                          not lines[j].strip().startswith(("Confidence:", "Sources:", "Proof:")))
            if not has_content:
                i += 1
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _ensure_next_action_header(text: str) -> str:
    """Safety net: the prompt requires the literal 'What I'd do next:' header
    on its own line before the action, but if the model skips straight to an
    unlabeled action paragraph anyway, insert the header rather than leaving
    an orphaned line between the learnings and Confidence."""
    if re.search(r"(?:What I'd do next|Next move):", text):
        return text  # header already present somewhere
    lines = text.splitlines()
    conf_idx = next((i for i, l in enumerate(lines) if l.strip().startswith("Confidence:")), None)
    if conf_idx is None:
        return text
    j = conf_idx - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j < 0:
        return text
    candidate = lines[j].strip()
    if not candidate or re.match(r"^\d+\.\s|^My read:", candidate, re.IGNORECASE):
        return text  # nothing to label, or it's a learning bullet / opener, not a lone action
    lines[j] = "What I'd do next:\n" + lines[j]
    return "\n".join(lines)


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

1. [Learning] — because [specific evidence]. Proof: [Sx]
2. [Learning] — because [specific evidence]. Proof: [Sy]
3. [Learning] — because [specific evidence]. Proof: [Sz]

What I'd do next:
[one concrete next action]

Confidence: [Strong / Medium / Directional / Thin data] — [short reason]

The literal line "What I'd do next:" must always appear on its own line \
immediately before the action, even in concise mode — never skip straight \
to the action text without it. It must NEVER be left empty or header-only \
either — if the bullet budget is tight, cut a learning before you cut the \
next action; there must always be at least one concrete action listed under it.

For an ideas question, use exactly this shape:
My read: [strategy in one sentence]
Ideas:
1. [Title] — Hook: ... Format: ... Why (ONE short sentence): ... Proof: [Sx]
2. [Title] — Hook: ... Format: ... Why (ONE short sentence): ... Proof: [Sy]
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
actually support the answer inline as "Proof: [Sx]" next to the claim it \
backs — do not cite every source available. Prefer, in this order: a \
Great-performing internal video example, Signal Library / Marketing \
Learnings, Product/ICP Learnings, latest_learnings.md, guidelines (only \
when directly relevant).
- Do NOT write a trailing "Sources:" or "Proof:" list at the end of your \
answer — the system appends a verified, clickable proof-links block \
automatically after your inline "Proof: [Sx]" citations. Only cite inline.
- Do not invent any number, percentage, or metric not already in the \
evidence below.
- Under ~1500 characters unless the user explicitly asked for depth/detail.
- No markdown tables. Use Slack mrkdwn: *single asterisks* for bold (never \
**double asterisks**).
- At most 5 bullets, {concise_limit}
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
    "bullets (not 3+), EACH bullet ONE short sentence (not two or three), "
    "target well under 800 characters total, skip long explanation, but the "
    "'What I'd do next' / 'Next move' section still always needs exactly one "
    "concrete action — cut a learning bullet before you ever cut that action."
)
_NORMAL_LIMIT = "whichever is fewer."


def compose_strategic_answer(user_text: str, conversation_context: list | None,
                             retrieved_context: dict, progress_cb=None) -> str | None:
    """Compose a strategist-voice answer from an already-built context pack.
    Returns None (caller uses the deterministic fallback) when disabled, on
    any failure, or when the output fails citation/number/causal/backend-
    language/table validation. progress_cb(str), if given, is called with
    short public stage names ("evidence", "writing") — never private
    chain-of-thought — right before each real phase of work."""
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

    if progress_cb:
        progress_cb("evidence")

    prompt = _PROMPT_TEMPLATE.format(
        brand_context=brand_context, source_list=source_list, evidence=evidence,
        thread_summary=thread_summary, question=user_text,
        concise_limit=_CONCISE_DIRECTIVE if concise else _NORMAL_LIMIT,
    )

    if progress_cb:
        progress_cb("writing")

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
    answer = _ensure_next_action_header(answer)
    answer = _fix_empty_next_action(answer)

    # Deterministically rebuild the trailing citation block as verified,
    # clickable proof links — whatever the model wrote for citations stays
    # only as inline "Proof: [Sx]" markers; the actual link/label text is
    # never left to the model, so it can never be a fake link. If the model
    # dropped all inline citations for what's otherwise a substantive,
    # evidence-backed answer, still show the strongest known sources rather
    # than leaving the claim uncited.
    cited_ids = {f"S{n}" for n in re.findall(r"\[S(\d+)\]", answer)}
    answer = _strip_existing_sources_line(answer)
    if sources:
        all_norm = normalize_sources(sources)
        cited_norm = [s for s in all_norm if s.source_id in cited_ids]
        chosen = select_strongest_sources(cited_norm or all_norm, _wants_more_sources(user_text))
        chosen_ids = {s.source_id for s in chosen}
        # Keep inline "Proof: [Sx]" markers in sync with what's actually
        # resolved below — no dangling citation to a source that got cut.
        answer = _restrict_inline_citations(answer, chosen_ids)
        proof = render_proof_section(chosen)
        if proof:
            answer = answer.rstrip() + "\n\n" + proof
    return answer
