"""Claim -> evidence binding and the source-relevance validator.

The failure this exists to make impossible
------------------------------------------
    Q: "What is the best time to post?"
    A: "We don't have enough evidence to determine it."
    Sources: three Great reels that have nothing to do with posting time.

Those reels were in the retrieved pack because they are the account's strongest
content, not because they said anything about posting time. `social_strategist.
compose_strategic_answer` then attached them anyway: when the model cited nothing,
it fell back to "show the strongest known sources" (`cited_norm or all_norm`).
An abstention has nothing to cite, so the fallback manufactured false support.

The rule this module enforces: a source reaches the reader only when it is BOUND
to a claim the answer actually makes. A source that is merely topically nearby is
dropped, and an answer with no data claims gets NO Sources block at all — the
absence of citations is strictly better than misleading ones (§14).

It also keeps the aggregate/example distinction honest (§16): an individual reel
ILLUSTRATES a pattern, it never establishes the aggregate statistic. Roles are
explicit so a renderer can label an example as an example.

Pure: no I/O, no LLM, no sheet access.
"""
from __future__ import annotations

import re
from typing import Optional

# --- evidence roles (§12) --------------------------------------------------
AGGREGATE_EVIDENCE = "aggregate"          # rates, averages, lift, distribution, sample size
EXAMPLE_CONTENT = "example"               # illustrates a pattern; never proves the aggregate
SCHEMA_EVIDENCE = "schema"                # field unavailable / empty / coverage limits
REFRESH_HISTORY = "history"               # last update, what changed since last run
EXTERNAL_REFERENCE = "external_reference"  # inspiration / execution reference only
STRATEGIC_INFERENCE = "inference"         # judgement; needs no source at all

ROLES = (AGGREGATE_EVIDENCE, EXAMPLE_CONTENT, SCHEMA_EVIDENCE, REFRESH_HISTORY,
         EXTERNAL_REFERENCE, STRATEGIC_INFERENCE)

# Roles that may support a claim on their own. A STRATEGIC_INFERENCE never needs
# a citation, and an EXAMPLE_CONTENT may only ride along with a claim that is
# already carried by aggregate or schema evidence.
_SELF_SUPPORTING = (AGGREGATE_EVIDENCE, SCHEMA_EVIDENCE, REFRESH_HISTORY,
                    EXTERNAL_REFERENCE)


class ClaimLedger:
    """The bindings behind one answer: {claim_id, claim, evidence_refs, role}.

    Deterministic renderers build this as they compute, so "what exact claim does
    this source support?" always has an answer before rendering.
    """

    def __init__(self):
        self.bindings: list[dict] = []

    def add(self, claim: str, evidence_refs=(), role: str = AGGREGATE_EVIDENCE) -> str:
        claim_id = f"C{len(self.bindings) + 1}"
        self.bindings.append({
            "claim_id": claim_id,
            "claim": str(claim or "").strip(),
            "evidence_refs": [str(r) for r in (evidence_refs or []) if str(r).strip()],
            "evidence_role": role if role in ROLES else AGGREGATE_EVIDENCE,
        })
        return claim_id

    def bound_ids(self) -> set:
        """Every source id bound to at least one claim."""
        out: set = set()
        for b in self.bindings:
            out.update(b["evidence_refs"])
        return out

    def ids_for_role(self, role: str) -> set:
        out: set = set()
        for b in self.bindings:
            if b["evidence_role"] == role:
                out.update(b["evidence_refs"])
        return out

    def supporting_ids(self) -> set:
        """Ids bound to a claim by a role that can support it on its own, plus
        examples that ride along with such a claim."""
        strong: set = set()
        for b in self.bindings:
            if b["evidence_role"] in _SELF_SUPPORTING:
                strong.update(b["evidence_refs"])
        if strong:
            strong.update(self.ids_for_role(EXAMPLE_CONTENT))
        return strong

    def orphans(self, source_ids) -> set:
        """Ids that would appear in a Sources block with no binding behind them."""
        return {str(s) for s in (source_ids or [])} - self.bound_ids()

    def as_list(self) -> list:
        return [dict(b) for b in self.bindings]


# ---------------------------------------------------------------------------
# text-level predicates — used for LLM-composed prose, where there is no ledger
# ---------------------------------------------------------------------------
# "We can't", "not enough", "we don't track" ... an answer whose substance is
# the ABSENCE of data. Nothing about our content library supports it, so nothing
# from the content library may be cited under it.
_MISSING_DATA_RE = re.compile(
    r"\b(?:can'?t|cannot|can not)\s+(?:yet\s+)?(?:split|compare|tell|say|call|answer|"
    r"break|separate|report|give|confirm|read)\b"
    r"|\bnot enough\b|\bnot yet enough\b|\btoo few\b|\btoo little\b|\bno hard\b"
    r"|\bdon'?t (?:have|track|carry|record|capture|store)\b"
    r"|\bdoesn'?t (?:exist|carry|track)\b|\bisn'?t (?:tracked|recorded|in the data)\b"
    r"|\bwe have no\b|\bthere'?s no\b|\bthere is no\b|\bnothing (?:here|in the data)\b"
    r"|\bunavailable\b|\bnot available\b|\bno (?:trial|duration|timestamp|posting[- ]time|"
    r"demographic|comment|retention|revenue)\b"
    r"|\bwould need\b|\bnot tracked\b|\bnot in the (?:data|dataset|sheet)\b"
    r"|\bno evidence\b|\binsufficient\b|\bcan'?t call\b",
    re.IGNORECASE)

# A concrete assertion about our numbers: a figure, a percentage, a sample size,
# a rate. Only these need — and may carry — evidence citations.
_DATA_CLAIM_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*%|\b\d+(?:\.\d+)?\s*(?:s|sec|secs|second|seconds)\b"
    r"|\b(?:median|average|mean|rate|distribution|sample|n\s*=\s*\d+)\b"
    r"|\b\d+\s+(?:of|out of|/)\s*\d+\b"
    r"|\b\d+\s+(?:reels?|posts?|videos?|clips?)\b"
    r"|\bassociated with\b|\bcorrelated with\b|\bskews?\b|\blift\b",
    re.IGNORECASE)


def is_missing_data_answer(text: str) -> bool:
    """True when the answer's substance is that the data cannot answer the
    question. Such answers get no content citations (§13)."""
    return bool(_MISSING_DATA_RE.search(str(text or "")))


def makes_data_claim(text: str) -> bool:
    """True when the answer asserts something concrete about our numbers."""
    return bool(_DATA_CLAIM_RE.search(str(text or "")))


def cited_ids(text: str) -> set:
    """Source ids the answer actually cites inline: [S1], [E2], [C3], [N1], [I4]."""
    return {m.group(0).strip("[]")
            for m in re.finditer(r"\[[A-Za-z]{1,2}\d{1,3}\]", str(text or ""))}


def relevant_source_ids(answer_text: str, source_ids,
                        ledger: Optional[ClaimLedger] = None) -> dict:
    """Decide which sources may appear under this answer.

    Returns {"keep": set, "dropped": set, "reason": str}.

    Precedence:
      1. an id the answer CITES inline is bound to the sentence it appears in;
      2. otherwise, a ledger decides — only ids bound to a supporting claim survive;
      3. otherwise (LLM prose with no ledger and no citations) an uncited id
         survives ONLY if the answer makes a data claim and is not a
         missing-data answer.
    """
    available = {str(s) for s in (source_ids or [])}
    if not available:
        return {"keep": set(), "dropped": set(), "reason": "no sources in pack"}

    cited = cited_ids(answer_text) & available
    if cited:
        return {"keep": cited, "dropped": available - cited,
                "reason": "kept only inline-cited sources"}

    if ledger is not None:
        keep = ledger.supporting_ids() & available
        return {"keep": keep, "dropped": available - keep,
                "reason": ("kept only claim-bound sources" if keep
                           else "no source is bound to a claim in this answer")}

    if is_missing_data_answer(answer_text):
        return {"keep": set(), "dropped": available,
                "reason": "missing-data answer: no source supports an absence of data"}
    if not makes_data_claim(answer_text):
        return {"keep": set(), "dropped": available,
                "reason": "no data claim to support"}
    return {"keep": available, "dropped": set(),
            "reason": "uncited but the answer makes a data claim"}


def drop_orphans(source_ids, ledger: ClaimLedger) -> set:
    """Ids with no binding at all — never renderable (§15)."""
    return ledger.orphans(source_ids)


def render_sources(pairs, ledger: Optional[ClaimLedger] = None,
                   header: str = "*Sources:*") -> str:
    """Render a Sources block from [(id, rendered_line)], or '' when empty.

    §14: an empty block is never rendered — no header, no placeholder. §16: an
    EXAMPLE_CONTENT source is labelled as an illustration so a single reel is
    never read as proof of an aggregate.
    """
    items = [(str(i), str(line)) for i, line in (pairs or []) if str(line).strip()]
    if not items:
        return ""
    example_ids = ledger.ids_for_role(EXAMPLE_CONTENT) if ledger else set()
    lines = []
    for sid, line in items:
        suffix = "  _(example, not the aggregate)_" if sid in example_ids else ""
        lines.append(f"  [{sid}] {line}{suffix}")
    return header + "\n" + "\n".join(lines)
