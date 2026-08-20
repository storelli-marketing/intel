"""Central Evidence & Answer Contract — the epistemic guardrail layer.

Sits between retrieval and the final Slack answer. It constrains TRUTH, not
writing style: what class of evidence backs a claim, how strong that claim may
be, whether the slice is specific enough, how fresh it is, whether the evidence
contradicts itself, and what to do when the answer is genuinely unknown.

It deliberately does NOT format answers. The conversation/response planner still
decides shape and voice; this layer only decides what may be asserted and how
strongly. Callers use `allowed_language()` / `hedge()` to phrase within limits.
"""
from __future__ import annotations

import re
from typing import Optional

import config

# ---------------------------------------------------------------------------
# Phase 5 — source authority
# ---------------------------------------------------------------------------
INTERNAL_STORELLI_METRIC = "INTERNAL_STORELLI_METRIC"
INTERNAL_STORELLI_CONTENT = "INTERNAL_STORELLI_CONTENT"
INTERNAL_DERIVED_PATTERN = "INTERNAL_DERIVED_PATTERN"
INTERNAL_WINNING_PROFILE = "INTERNAL_WINNING_PROFILE"
SEMANTIC_CONNECTION = "SEMANTIC_CONNECTION"
EXTERNAL_INSPIRATION = "EXTERNAL_INSPIRATION"
STRATEGIC_INFERENCE = "STRATEGIC_INFERENCE"

# Ordered by authority for supporting a "this works for Storelli" claim.
SOURCE_AUTHORITY = (INTERNAL_STORELLI_METRIC, INTERNAL_STORELLI_CONTENT,
                    INTERNAL_DERIVED_PATTERN, INTERNAL_WINNING_PROFILE,
                    SEMANTIC_CONNECTION, EXTERNAL_INSPIRATION, STRATEGIC_INFERENCE)

# Classes that may support an internal performance/"it works" claim.
INTERNAL_PROOF_CLASSES = frozenset({INTERNAL_STORELLI_METRIC, INTERNAL_STORELLI_CONTENT,
                                    INTERNAL_DERIVED_PATTERN, INTERNAL_WINNING_PROFILE})
# What external inspiration MAY support.
EXTERNAL_ALLOWED_USES = frozenset({"execution_inspiration", "storytelling_reference",
                                   "visual_reference", "hypothesis_generation"})
# What it may NEVER support.
EXTERNAL_FORBIDDEN_USES = frozenset({"works_for_storelli", "internal_performance",
                                     "winning_profile_evidence", "internal_correlation"})


class ExternalAsProofError(ValueError):
    """Raised when external inspiration is used where internal proof is required."""


def is_internal_proof(source_class: str) -> bool:
    return source_class in INTERNAL_PROOF_CLASSES


def assert_internal_only(evidence, where: str = "internal statistic") -> None:
    """Structural guard: reject external rows from internal-statistics paths.

    `evidence` may be one evidence dict/object or an iterable of them."""
    items = evidence if isinstance(evidence, (list, tuple, set)) else [evidence]
    for e in items:
        sc = e.get("source_class") if isinstance(e, dict) else getattr(e, "source_class", None)
        if sc == EXTERNAL_INSPIRATION:
            raise ExternalAsProofError(
                f"external inspiration cannot support {where}; it is execution "
                "reference only")


def external_use_allowed(use: str) -> bool:
    return use in EXTERNAL_ALLOWED_USES


# ---------------------------------------------------------------------------
# Phase 6 — claim strength
# ---------------------------------------------------------------------------
PROVEN = "PROVEN"
SUPPORTED = "SUPPORTED"
DIRECTIONAL = "DIRECTIONAL"
INFERRED = "INFERRED"
UNKNOWN = "UNKNOWN"

_STRENGTH_ORDER = (UNKNOWN, INFERRED, DIRECTIONAL, SUPPORTED, PROVEN)


def strength_rank(s: str) -> int:
    return _STRENGTH_ORDER.index(s) if s in _STRENGTH_ORDER else 0


def cap_strength(strength: str, ceiling: str) -> str:
    """Never exceed the ceiling."""
    return strength if strength_rank(strength) <= strength_rank(ceiling) else ceiling


# Conservative, configurable thresholds (Phase 7).
def _thr(name: str, default: int) -> int:
    try:
        return int(getattr(config, name, default) or default)
    except (TypeError, ValueError):
        return default


PROVEN_MIN_SAMPLE = _thr("EVIDENCE_PROVEN_MIN_SAMPLE", 8)
PROVEN_MIN_POSITIVE = _thr("EVIDENCE_PROVEN_MIN_POSITIVE", 5)
SUPPORTED_MIN_SAMPLE = _thr("EVIDENCE_SUPPORTED_MIN_SAMPLE", 5)
SUPPORTED_MIN_POSITIVE = _thr("EVIDENCE_SUPPORTED_MIN_POSITIVE", 3)
DIRECTIONAL_MIN_SAMPLE = _thr("EVIDENCE_DIRECTIONAL_MIN_SAMPLE", 2)
MIN_COMPARISON = _thr("EVIDENCE_MIN_COMPARISON", 2)


# ---------------------------------------------------------------------------
# Phase 7 — evidence sufficiency
# ---------------------------------------------------------------------------
def evaluate_sufficiency(sample_size: int = 0, positive_examples: int = 0,
                         comparison_examples: int = 0, effect_signal: Optional[float] = None,
                         source_classes=(), specificity: str = "",
                         freshness_days: Optional[float] = None,
                         contradictions: int = 0,
                         metric_available: bool = True,
                         outlier_share: Optional[float] = None) -> dict:
    """Return the structured sufficiency object that governs claim language.

    Deliberately conservative: n=2 can never become "this works". No fake
    statistical precision — `confidence` is a coarse 0..1 band, not a p-value.
    """
    classes = set(source_classes or ())
    internal = {c for c in classes if c in INTERNAL_PROOF_CLASSES}
    limitations = []

    if not metric_available:
        return _suff(UNKNOWN, 0.0, sample_size, positive_examples, comparison_examples,
                     effect_signal, outlier_share, specificity, freshness_days,
                     contradictions, ["the required metric isn't available"])

    # No internal evidence at all -> at best an inference (never proof).
    if not internal:
        if classes & {EXTERNAL_INSPIRATION, SEMANTIC_CONNECTION, STRATEGIC_INFERENCE}:
            limitations.append("no internal Storelli evidence — external/inference only")
            return _suff(INFERRED, 0.3, sample_size, positive_examples, comparison_examples,
                         effect_signal, outlier_share, specificity, freshness_days,
                         contradictions, limitations)
        return _suff(UNKNOWN, 0.0, sample_size, positive_examples, comparison_examples,
                     effect_signal, outlier_share, specificity, freshness_days,
                     contradictions, ["no supporting evidence found"])

    # Sample-driven ceiling.
    if sample_size >= PROVEN_MIN_SAMPLE and positive_examples >= PROVEN_MIN_POSITIVE:
        strength, conf = PROVEN, 0.85
    elif sample_size >= SUPPORTED_MIN_SAMPLE and positive_examples >= SUPPORTED_MIN_POSITIVE:
        strength, conf = SUPPORTED, 0.7
    elif sample_size >= DIRECTIONAL_MIN_SAMPLE:
        strength, conf = DIRECTIONAL, 0.45
        limitations.append(f"small sample (n={sample_size})")
    else:
        strength, conf = UNKNOWN, 0.1
        limitations.append("not enough internal examples to say anything yet")

    # A claim comparing "what works" needs something to compare against.
    if comparison_examples < MIN_COMPARISON and strength in (PROVEN, SUPPORTED):
        strength = cap_strength(strength, DIRECTIONAL)
        conf = min(conf, 0.5)
        limitations.append("little comparison evidence (few counter-examples)")

    # A flat/absent effect can't be PROVEN.
    if effect_signal is not None and abs(effect_signal) < 0.05:
        strength = cap_strength(strength, DIRECTIONAL)
        conf = min(conf, 0.45)
        limitations.append("the performance gap is small")

    # Outlier concentration: one viral post carrying the pattern.
    if outlier_share is not None and outlier_share >= 0.5 and positive_examples > 0:
        strength = cap_strength(strength, DIRECTIONAL)
        conf = min(conf, 0.45)
        limitations.append("mostly driven by one outlier post")

    # Contradictions cap confidence (and are surfaced, not hidden).
    if contradictions > 0:
        strength = cap_strength(strength, SUPPORTED)
        conf = min(conf, 0.55)
        limitations.append("some evidence points the other way")

    # Phase 12 — staleness reduces allowed strength.
    stale_limit = config.INTELLIGENCE_REFRESH_CADENCE_DAYS + config.INTELLIGENCE_STALE_TOLERANCE_DAYS
    if freshness_days is not None and freshness_days > stale_limit:
        strength = cap_strength(strength, SUPPORTED if strength == PROVEN else strength)
        strength = cap_strength(strength, DIRECTIONAL) if freshness_days > stale_limit * 2 \
            else strength
        conf = min(conf, 0.6)
        limitations.append(f"performance data is {int(freshness_days)} days old")

    return _suff(strength, conf, sample_size, positive_examples, comparison_examples,
                 effect_signal, outlier_share, specificity, freshness_days,
                 contradictions, limitations)


def _suff(strength, conf, n, pos, cmp_, effect, outlier, spec, fresh, contra, lims) -> dict:
    return {"claim_strength": strength, "confidence": round(float(conf), 2),
            "sample_size": int(n or 0), "positive_examples": int(pos or 0),
            "comparison_examples": int(cmp_ or 0), "effect_signal": effect,
            "outlier_risk": outlier, "specificity": spec or "unspecified",
            "freshness_days": fresh, "contradictions": int(contra or 0),
            "limitations": lims}


# ---------------------------------------------------------------------------
# Phase 8 — specificity ladder
# ---------------------------------------------------------------------------
SCOPE_EXACT = "product+icp"
SCOPE_ICP = "icp"
SCOPE_PRODUCT = "product"
SCOPE_BROAD = "all_internal"

_SCOPE_LABEL = {SCOPE_EXACT: "this exact product + audience",
                SCOPE_ICP: "this audience across products",
                SCOPE_PRODUCT: "this product across audiences",
                SCOPE_BROAD: "goalkeeper content more broadly"}


def resolve_scope(counts: dict, min_n: int = DIRECTIONAL_MIN_SAMPLE) -> dict:
    """Walk the specificity ladder and report which scope actually has evidence.

    `counts` maps scope -> sample size. Returns the chosen scope, whether it was
    relaxed, and the honest disclosure to weave into the answer."""
    for scope in (SCOPE_EXACT, SCOPE_ICP, SCOPE_PRODUCT, SCOPE_BROAD):
        n = int(counts.get(scope, 0) or 0)
        if n >= min_n:
            relaxed = scope != SCOPE_EXACT
            exact_n = int(counts.get(SCOPE_EXACT, 0) or 0)
            return {"scope": scope, "sample_size": n, "relaxed": relaxed,
                    "exact_sample": exact_n, "label": _SCOPE_LABEL[scope],
                    "disclosure": (f"only {exact_n} example(s) at the exact slice, so this "
                                   f"reads from {_SCOPE_LABEL[scope]}" if relaxed else "")}
    return {"scope": None, "sample_size": int(counts.get(SCOPE_EXACT, 0) or 0),
            "relaxed": False, "exact_sample": int(counts.get(SCOPE_EXACT, 0) or 0),
            "label": "", "disclosure": "we don't have enough examples at any useful slice"}


# ---------------------------------------------------------------------------
# Phase 14 — contradictions
# ---------------------------------------------------------------------------
def find_contradictions(overall: list, segment: list, key: str = "label") -> list:
    """Where a segment's ranking disagrees with the overall ranking.

    Both lists are [{label, lift}]-shaped. Returns the disagreements so the
    strategist can say "it's mixed" instead of forcing one answer."""
    if not overall or not segment:
        return []
    top_overall = overall[0].get(key)
    top_segment = segment[0].get(key)
    out = []
    if top_overall and top_segment and top_overall != top_segment:
        out.append({"overall": top_overall, "segment": top_segment,
                    "note": f"{top_overall} leads overall, {top_segment} leads in this slice"})
    by_overall = {o.get(key): o.get("lift", 0) for o in overall}
    for s in segment:
        lab, lift = s.get(key), s.get("lift", 0)
        if lab in by_overall and by_overall[lab] * lift < 0:      # opposite signs
            out.append({"overall": lab, "segment": lab,
                        "note": f"{lab} points the other way in this slice"})
    return out[:3]


# ---------------------------------------------------------------------------
# Phase 15 — productive abstention
# ---------------------------------------------------------------------------
# Phrasings that make a decision mandatory. When the human has already accepted
# the constraint ("we only have one shoot day", "if you had to pick one"),
# answering "not enough evidence" is not honesty — it's a non-answer to a
# question about what to do next. The honest form is to COMMIT and label the
# commitment as judgement, with the condition that would change it.
_FORCED_CHOICE = (
    r"\b(?:we can only|can only|only have|we only have|if (?:we|you) had to"
    r"|if (?:we|you) could only|had to (?:pick|choose|double down)"
    r"|force[d]? (?:me|you|us) to|pick (?:one|just one)|which one\b"
    r"|what are we not|what do we (?:cut|drop|kill|not shoot|film first)"
    r"|entirely\b|one shoot day|single session|no more than"
    r"|and what are|which (?:three|two|3|2)\b)")


def forced_choice_requested(text: str) -> bool:
    """True when the question demands a decision rather than an assessment."""
    return bool(re.search(_FORCED_CHOICE, str(text or ""), re.IGNORECASE))


def committed_judgement(pick: str, because: str, sample_note: str = "",
                        runner_up: str = "", would_change: str = "") -> str:
    """A decision made on thin evidence, honestly labelled.

    Deliberately NOT hedge-then-recommend: the call comes first and plainly, the
    epistemic status is stated once, and the reader is told what would overturn
    it. This is the answer shape for a forced choice we cannot prove.
    """
    parts = [f"{pick}."]
    if because:
        parts.append(f"*Why:* {because}.")
    status = "That's a judgement call, not a proven result"
    if sample_note:
        status += f" — {sample_note}"
    parts.append(status + ".")
    if runner_up:
        parts.append(f"*Closest alternative:* {runner_up}.")
    parts.append("*What would change it:* "
                 + (would_change or "a few more posts on either side, so the "
                                    "comparison stops resting on one video"))
    return " ".join(parts)


def abstention(topic: str, missing: str = "", test_hint: str = "") -> dict:
    """UNKNOWN with a useful next move, never a bare 'insufficient data'."""
    test = test_hint or ("the cleanest test would be the same concept shot twice with one "
                         "variable changed, keeping the execution constant")
    return {"claim_strength": UNKNOWN, "confidence": 0.0,
            "gap": missing or f"we don't have enough evidence about {topic}",
            "proposed_test": test,
            "limitations": [missing or f"no usable evidence for {topic}"]}


# ---------------------------------------------------------------------------
# Phase 11/16 — answer + recommendation contract
# ---------------------------------------------------------------------------
def build_answer(direct_answer: str, facts=None, inferences=None, recommendation: str = "",
                 limitations=None, sufficiency: Optional[dict] = None,
                 evidence_refs=None, contradictions=None,
                 could_change_mind: str = "") -> dict:
    """The internal representation every substantive answer reduces to.

    FACT = present in / deterministically derived from validated evidence.
    INFERENCE = strategic interpretation. RECOMMENDATION = what to do.
    Rendering is the response planner's job — these headings are for auditability.
    """
    suff = sufficiency or {}
    return {
        "direct_answer": direct_answer,
        "facts": list(facts or []),
        "inferences": list(inferences or []),
        "recommendation": recommendation,
        "could_change_mind": could_change_mind,
        "limitations": list(limitations or []) + list(suff.get("limitations", [])),
        "claim_strength": suff.get("claim_strength", INFERRED),
        "confidence": suff.get("confidence", 0.3),
        "sample_size": suff.get("sample_size", 0),
        "evidence_refs": list(evidence_refs or []),
        "contradictions": list(contradictions or []),
        "sufficiency": suff,
    }


# ---------------------------------------------------------------------------
# language envelope — how strongly the answer may be phrased
# ---------------------------------------------------------------------------
_ALLOWED = {
    PROVEN: {"max_verb": "clearly outperforms", "may_say_proven": True,
             "hedge": ""},
    SUPPORTED: {"max_verb": "is one of the stronger patterns", "may_say_proven": False,
                "hedge": "one of the stronger patterns we're seeing"},
    DIRECTIONAL: {"max_verb": "shows a signal", "may_say_proven": False,
                  "hedge": "there's a signal, but I wouldn't call it proven yet"},
    INFERRED: {"max_verb": "would likely help", "may_say_proven": False,
               "hedge": "that's an inference, not something we've measured"},
    UNKNOWN: {"max_verb": "isn't answerable yet", "may_say_proven": False,
              "hedge": "we don't have the data to answer that yet"},
}

# Words that assert proof/causation — only legitimate at PROVEN.
# Proof/absolute assertions. Negated forms ("not proven", "never proves", "no
# guarantee") are honest hedging and are deliberately excluded.
_PROOF_WORDS = re.compile(
    r"(?<!not )(?<!never )(?<!n't )(?<!isn't )"
    r"\b(proven|proves|proof that|clearly (?:shows|outperforms|works)|definitely|"
    r"always works|causes|caused by|leads to|results in)\b"
    r"|(?<!not a )(?<!no )(?<!not )\bguarantee[sd]?\b", re.IGNORECASE)
_STRONG_WORDS = re.compile(r"\b(clearly|definitely|certainly|without a doubt|obviously)\b",
                           re.IGNORECASE)


def allowed_language(strength: str) -> dict:
    return dict(_ALLOWED.get(strength, _ALLOWED[INFERRED]))


def hedge(strength: str) -> str:
    return _ALLOWED.get(strength, _ALLOWED[INFERRED])["hedge"]


def overstates(text: str, strength: str) -> bool:
    """True when the wording asserts more than the evidence allows."""
    t = str(text or "")
    if strength == PROVEN:
        # even at PROVEN, causal language is not licensed (correlation only)
        return bool(re.search(
            r"(?<!not )(?<!no )(?<!never )\b(causes|caused by|leads to|results in)\b"
            r"|(?<!not a )(?<!no )\bguarantee[sd]?\b", t, re.IGNORECASE))
    if _PROOF_WORDS.search(t):
        return True
    if strength in (INFERRED, UNKNOWN) and _STRONG_WORDS.search(t):
        return True
    return False
