"""Active decision frame — "what problem are we currently solving?".

The brain already remembered ENTITIES across turns (last product, last idea ids).
What it did not keep was the *decision being made*, so a follow-up phrased as a
new question ("what should we shoot next week based on what you just shared?")
was classified as a fresh ask, disowned by the contextual agent, and then claimed
by the generic `shoot` keyword handler — which re-ranked the global idea pool and
silently swapped in a different product and a different optimisation objective.

This module is that missing piece and nothing more. It is deliberately NOT a
transcript store and NOT a general memory layer: it holds one compact record of
the current decision, derives it from the answer that established it, and answers
three questions for the routing layer:

  * is a decision frame active?
  * does this turn continue it, or replace it?
  * what evidence is in scope, and what are we optimising for?

Layering (unchanged elsewhere): the frame decides WHAT EVIDENCE TO CONSIDER.
The Evidence Contract still decides WHAT MAY BE CLAIMED about it.
"""
from __future__ import annotations

import re
from typing import Optional

import conversation_state as CS
from logger import get_logger

log = get_logger()

# ---------------------------------------------------------------------------
# vocabulary used to read a frame back out of an answer
# ---------------------------------------------------------------------------
# Multi-word first: "BodyShield GK Leggings" must win over "Leggings", or the
# frame records a broader scope than the answer actually established.
_PRODUCT_VOCAB = (
    "BodyShield GK 3/4 Leggings", "BodyShield NoBurn GK Leggings",
    "BodyShield GK Leggings", "BodyShield GK Undershirt", "BodyShield Leg Guard",
    "BodyShield Leggings", "ExoShield Gladiator Jersey", "ExoShield Head Guard",
    "Gladiator Pro 3 Gloves", "Gladiator Pro Gloves", "BodyShield", "ExoShield",
    "Head Guard", "Undershirt", "Leggings", "Sliders", "Gloves", "Shorts",
    "Pants", "Bundles",
)
_ICP_VOCAB = ("Aspiring Pro", "Adult Amateur", "Parents", "Youth", "Coach", "General")

# How a human signals "keep the current decision going". These are the phrases
# that were previously misread as brand-new questions.
_INHERIT_CUES = (
    "based on what you just", "based on the latest data you just", "based on that",
    "based on this", "based on the above", "based on what we", "given that",
    "given this", "given what", "because of that", "because of this",
    "in that case", "with that in mind", "on that basis", "off the back of that",
    "you just shared", "you just said", "we just discussed", "we just found",
    "we just established", "that data", "those findings", "that finding",
    "the latest data", "same thing", "from that",
)
# Bare continuations: only inherit when a frame is already active.
_BARE_CONTINUATIONS = (
    "what should we shoot", "what do we shoot", "what should we make",
    "what should we film", "what should we post", "what next", "what's next",
    "whats next", "so what now", "what now", "which one", "which of those",
    "so what should we", "then what should we", "what should we do about",
    "why", "why?", "are you sure", "how sure", "what would change your mind",
    "what am i missing", "what's the argument against", "whats the argument against",
    "why not the other", "what about next week", "and next week",
)
# An explicit scope change: the user is deliberately leaving the frame.
_RESET_CUES = (
    "across all products", "all products", "regardless of product", "any product",
    "whole calendar", "entire calendar", "across the board", "globally",
    "forget that", "forget the", "never mind", "nevermind", "different topic",
    "new question", "unrelated", "instead of that", "actually, strongest",
    "strongest idea across", "best idea across", "start over", "ignore that",
)

# ---------------------------------------------------------------------------
# optimisation objectives — what "best" means for this decision
# ---------------------------------------------------------------------------
EXPLOIT_LEARNING = "exploit the latest learning"
EVIDENCE_BACKED = "maximize evidence-backed performance potential"
LEARN_NEW = "learn something new"
MAX_COMMENTS = "maximize comments"
PRACTICALITY = "maximize production practicality"
DIVERSIFY = "diversify the calendar"
BALANCE = "balance risk and proof"

_OBJECTIVE_CUES = (
    (PRACTICALITY, ("easiest production", "easiest to produce", "quickest to shoot",
                    "fastest to shoot", "cheapest to produce", "practical",
                    "practicality", "least effort", "one shoot day",
                    "production efficiency", "easiest")),
    (MAX_COMMENTS, ("comments", "conversation", "replies")),
    (LEARN_NEW, ("learn something new", "untested", "never tried", "explore",
                 "new territory", "diversify what we learn")),
    (DIVERSIFY, ("diversify", "spread", "variety", "mix it up", "different products")),
    (EXPLOIT_LEARNING, ("based on what you just", "based on the latest data",
                        "based on that", "you just shared", "we just found",
                        "we just discussed", "latest learning", "because of that",
                        "exploit")),
    (BALANCE, ("balance", "risk and proof", "hedge")),
    (EVIDENCE_BACKED, ("best performing", "highest performing", "most likely to work",
                       "strongest evidence", "best bet", "evidence-backed")),
)


def detect_objective(text: str) -> str:
    """The optimisation goal this turn asks for, or '' to inherit/default.

    Order matters: an explicit production/comment/learning ask wins over the
    generic "exploit what we just found", because the user naming a different
    objective is a deliberate change of what 'best' means.
    """
    t = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    for goal, cues in _OBJECTIVE_CUES:
        if any(c and c in t for c in cues):
            return goal
    return ""


# ---------------------------------------------------------------------------
# frame shape
# ---------------------------------------------------------------------------
def new_frame() -> dict:
    return {"topic": "", "objective": "",
            "scope": {"product": [], "icp": [], "format": [], "hook": [],
                      "concept_ids": []},
            "prior_findings": [], "prior_recommendation": None,
            "optimization_goal": None, "constraints": [], "evidence_refs": [],
            "confidence": None, "open_question": None}


def is_active(frame: Optional[dict]) -> bool:
    """A frame is only usable if it actually narrows something."""
    if not frame:
        return False
    sc = frame.get("scope") or {}
    return bool(any(sc.get(k) for k in ("product", "icp", "format", "hook",
                                        "concept_ids"))
                or frame.get("prior_recommendation"))


def _find_vocab(text: str, vocab) -> list:
    """Longest-match-first vocabulary scan, de-duplicated, order preserved.
    A longer match suppresses any shorter match contained inside it, so
    'BodyShield GK Leggings' does not also register as 'Leggings'."""
    low = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    hits, consumed = [], []
    for term in sorted(vocab, key=len, reverse=True):
        t = term.lower()
        if not _contains_term(low, t):
            continue
        if any(t in c for c in consumed):
            continue                      # already covered by a longer term
        consumed.append(t)
        hits.append(term)
    return hits


def _contains_term(haystack: str, term: str) -> bool:
    """Whole-term containment. Naive substring matching made 'Demo' match inside
    'demonstrates' and 'Story' inside 'history', so an answer that merely used the
    word 'demonstrates' registered the Demo format as an established pattern."""
    t = re.escape(str(term).strip())
    if not t:
        return False
    return re.search(rf"(?<![A-Za-z0-9]){t}(?![A-Za-z0-9])", haystack) is not None


def _taxonomy_hits(text: str, layer: str) -> list:
    import taxonomy
    low = " " + re.sub(r"\s*/\s*", "/", str(text or "").lower()) + " "
    out = []
    for opt in taxonomy.LAYERS.get(layer, []):
        norm = re.sub(r"\s*/\s*", "/", str(opt).lower())
        if len(norm) >= 3 and _contains_term(low, norm):
            out.append(opt)
    return out


# Sentences that assert something about performance are the findings worth
# carrying; ordinary prose is not.
_FINDING_SIGNAL = re.compile(
    r"\b(work(?:s|ed|ing)?|perform(?:s|ed|ing)?|strong(?:est|er)?|weak(?:est|er)?"
    r"|lift|great rate|best|highest|effective|associated|outperform|landed"
    r"|underperform|thin|territory)\b", re.IGNORECASE)


def _findings(text: str, limit: int = 4) -> list:
    """Compact list of the pattern conclusions an answer asserted."""
    out = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")):
        s = re.sub(r"<[^>]*>", "", raw)                 # drop slack links
        s = re.sub(r"\[[SECNI]\d+\]", "", s)
        s = re.sub(r"[*_`]", "", s).strip(" -•\t")
        s = re.sub(r"^(?:my read|read|takeaway|short answer|bottom line|in short|"
                   r"tl;?dr)\s*[:\-–]\s*", "", s, flags=re.IGNORECASE)
        if len(s) < 25 or len(s) > 240:
            continue
        if not _FINDING_SIGNAL.search(s):
            continue
        if s.lower().startswith(("shoot these", "sources", "my move")):
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


_TITLE_RE = re.compile(r"\*(?:\d+\.\s*)?([A-Z][^*\n]{6,70})\*")


def _is_vocab_term(cand: str) -> bool:
    """A bolded taxonomy option or product name is the SUBJECT of an analysis,
    not a concept being recommended. Treating '*Curiosity Gap*' as the prior
    recommendation would make an analytical turn look like a decision."""
    import taxonomy
    c = re.sub(r"\s*/\s*", "/", cand.strip().lower())
    for options in taxonomy.LAYERS.values():
        if any(c == re.sub(r"\s*/\s*", "/", str(o).lower()) for o in options):
            return True
    return any(c == p.lower() for p in _PRODUCT_VOCAB) or \
        any(c == i.lower() for i in _ICP_VOCAB)


def _recommended_title(text: str) -> str:
    """The concept an answer actually recommended, if it named one."""
    for m in _TITLE_RE.finditer(str(text or "")):
        cand = m.group(1).strip(" :–—-")
        if cand.lower().startswith(("why", "sources", "my move", "what", "proof",
                                    "closest", "format", "hook")):
            continue
        if _is_vocab_term(cand):
            continue
        if len(cand.split()) >= 2:
            return cand
    return ""


# Hedging the ESTABLISHING answer applied to itself. Carrying scope without
# carrying this is how a thin-evidence finding turned into a confident-sounding
# recommendation one turn later.
_THIN_LANGUAGE = re.compile(
    r"\b(?:thin|only \d+|just \d+ post|too few|not proven|wouldn'?t call|"
    r"small sample|barely|hypothes|inference|inferred|not enough|"
    r"can'?t call|treat it as)\b", re.IGNORECASE)


def pattern_strength(anchor_text: str, evidence_refs=None) -> str:
    """The claim strength the establishing answer itself supported.

    Read from the answer rather than recomputed, so the frame propagates the
    caveat the analysis actually made instead of inventing a new one. The weaker
    of (what it said about itself) and (how much it cited) wins.
    """
    import evidence_contract as EC
    refs = list(evidence_refs or [])
    by_refs = (EC.SUPPORTED if len(refs) >= 3 else
               EC.DIRECTIONAL if len(refs) == 2 else
               EC.INFERRED if refs else EC.UNKNOWN)
    if _THIN_LANGUAGE.search(str(anchor_text or "")):
        by_language = EC.INFERRED
    else:
        by_language = EC.SUPPORTED
    order = [EC.UNKNOWN, EC.INFERRED, EC.DIRECTIONAL, EC.SUPPORTED, EC.PROVEN]
    return order[min(order.index(by_refs), order.index(by_language))]


def derive(context: Optional[list], user_text: str = "",
           state: Optional[dict] = None) -> dict:
    """Read the active decision frame out of the conversation.

    Derivation (rather than only explicit recording) is deliberate: the answer
    that establishes a frame often comes from the LLM strategist path, which
    records no structured output. Reading the frame back out of the answer text
    means inheritance works for every route, not just the ones we instrumented.
    """
    ctx = context or []
    stored = ((state or {}).get("decision_frame") or {}) if state else {}
    frame = new_frame()
    frame.update({k: v for k, v in stored.items() if k in frame})
    if isinstance(stored.get("scope"), dict):
        frame["scope"] = {**frame["scope"], **stored["scope"]}

    assistants = [m.get("text", "") for m in ctx if m.get("role") == "assistant"]
    if not assistants:
        return frame

    # A frame is ESTABLISHED ONCE and then CARRIED, and the anchor is the EARLIEST
    # answer that established it — not the most recent one.
    #
    # Anchoring on the latest answer made the frame drift under its own output: a
    # recommendation given inside the frame cites its own sources, so the ICP
    # silently moved from the one we agreed on to whatever appeared in a citation
    # label, and `prior_findings` became a quote of our own recommendation rather
    # than the analysis it rests on. Thread history is the primary state here (the
    # in-memory cache is cold on a fresh process), so the anchor has to be
    # recoverable from the transcript alone.
    turns = [(m.get("role"), m.get("text", "")) for m in ctx]
    start = 0
    for i, (role, txt) in enumerate(turns):
        if role != "user":
            continue
        # a deliberate reset or a newly-named scope starts a NEW frame from here
        if wants_reset(txt) or (is_active(frame) and names_new_scope(txt, frame)):
            start = i
            frame = new_frame()
    if is_active(frame) and not names_new_scope(user_text, frame):
        frame["prior_recommendation"] = (_recommended_title(assistants[-1])
                                         or frame.get("prior_recommendation"))
        return frame

    anchor = anchor_asked = ""
    for i in range(start, len(turns)):
        role, txt = turns[i]
        if role != "assistant" or not txt.strip():
            continue
        probe = new_frame()
        for key, vocab in (("product", _PRODUCT_VOCAB), ("icp", _ICP_VOCAB)):
            found = _find_vocab(txt, vocab)
            if found:
                probe["scope"][key] = found[:3]
        if is_active(probe):
            anchor = txt
            anchor_asked = next((t for r, t in reversed(turns[start:i]) if r == "user"), "")
            break
    if not anchor:
        anchor = assistants[-1]
        anchor_asked = next((t for r, t in reversed(turns) if r == "user"), "")

    # Scope comes from the ANCHOR answer (what was established), widened by the
    # question that prompted it — a product named only in the question still
    # scopes the decision.
    for key, vocab in (("product", _PRODUCT_VOCAB), ("icp", _ICP_VOCAB)):
        found = _find_vocab(anchor, vocab) or _find_vocab(anchor_asked, vocab)
        if found:
            frame["scope"][key] = found[:3]
    for key, layer in (("format", "format"), ("hook", "hook")):
        found = _taxonomy_hits(anchor, layer)
        if found:
            frame["scope"][key] = found[:3]

    findings = _findings(anchor)
    if findings:
        frame["prior_findings"] = findings
    refs = CS._source_tags(anchor)
    if refs:
        frame["evidence_refs"] = refs
    frame["confidence"] = pattern_strength(anchor, refs)
    # what was recommended is the freshest fact in the thread, not the anchor's
    rec = _recommended_title(assistants[-1]) or _recommended_title(anchor)
    if rec:
        frame["prior_recommendation"] = rec

    prods = frame["scope"]["product"]
    if prods and not frame.get("topic"):
        frame["topic"] = f"{prods[0]} performance"
    elif not frame.get("topic"):
        icps = frame["scope"]["icp"]
        frame["topic"] = f"{icps[0]} performance" if icps else "current decision"
    if not frame.get("objective"):
        frame["objective"] = "understand what is currently working"
    return frame


# ---------------------------------------------------------------------------
# inheritance / precedence
# ---------------------------------------------------------------------------
def wants_reset(text: str) -> bool:
    """The user is explicitly leaving the current frame."""
    t = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    return any(c in t for c in _RESET_CUES)


def inherits(text: str, frame: Optional[dict] = None) -> bool:
    """Does this turn continue the active decision?

    An explicit cue ("based on that") inherits on its own. A bare continuation
    ("what should we shoot") only inherits when a frame is actually active —
    otherwise it is a genuine opening question and must keep its existing route.
    """
    if wants_reset(text):
        return False
    t = " " + re.sub(r"\s+", " ", str(text or "").lower()).strip(" ?.!") + " "
    if any(c in t for c in _INHERIT_CUES):
        return True
    if is_active(frame) and any(c in t for c in _BARE_CONTINUATIONS):
        return True
    # "optimize for easiest production instead" — a new CRITERION for the same
    # decision. Same evidence universe, different ranking; treating it as a fresh
    # question would silently drop the scope we just agreed on.
    if is_active(frame) and detect_objective(text) and not names_new_scope(text, frame):
        return True
    return False


def names_new_scope(text: str, frame: Optional[dict]) -> bool:
    """True when the turn names a product/ICP outside the frame — an explicit new
    objective rather than a continuation."""
    if not is_active(frame):
        return False
    sc = frame.get("scope") or {}
    for key, vocab in (("product", _PRODUCT_VOCAB), ("icp", _ICP_VOCAB)):
        found = _find_vocab(text, vocab)
        current = [str(v).lower() for v in (sc.get(key) or [])]
        for f in found:
            fl = f.lower()
            if not any(fl in c or c in fl for c in current):
                return True
    return False


def resolve_objective(text: str, frame: Optional[dict]) -> tuple:
    """(objective, requested_explicitly).

    Explicit request wins; otherwise inherit the frame's; otherwise the sensible
    default for a frame-constrained decision is to exploit the finding we just
    established, NOT some criterion the user never asked for.

    EXPLOIT_LEARNING is only ever INFERRED — its cues are inheritance phrases
    ("based on that"), not a named criterion — so it never counts as explicitly
    requested. Reporting it as requested would put words in the user's mouth.
    """
    explicit = detect_objective(text)
    if explicit:
        return explicit, explicit != EXPLOIT_LEARNING
    inherited = (frame or {}).get("optimization_goal")
    if inherited:
        return inherited, False
    return (EXPLOIT_LEARNING if is_active(frame) else EVIDENCE_BACKED), False


# ---------------------------------------------------------------------------
# persistence helper (kept next to the shape it writes)
# ---------------------------------------------------------------------------
def remember(state: Optional[dict], frame: dict, objective: str = "",
             recommendation: str = "") -> dict:
    """Store the frame on the conversation state (compact, no payloads)."""
    if state is None:
        return frame
    stored = dict(frame)
    if objective:
        stored["optimization_goal"] = objective
    if recommendation:
        stored["prior_recommendation"] = recommendation
    stored["prior_findings"] = list(stored.get("prior_findings") or [])[:4]
    state["decision_frame"] = stored
    return stored


def describe_scope(frame: Optional[dict]) -> str:
    """Short human phrase for the frame's scope ('BodyShield GK Leggings for
    Aspiring Pro'), used when an answer needs to say what it stayed inside."""
    sc = (frame or {}).get("scope") or {}
    prods = sc.get("product") or []
    icps = sc.get("icp") or []
    if prods and icps:
        return f"{prods[0]} for {icps[0]}"
    if prods:
        return prods[0]
    if icps:
        return icps[0]
    pattern = (sc.get("hook") or []) + (sc.get("format") or [])
    return pattern[0] if pattern else "the current thread"
