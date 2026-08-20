"""Claim validation (Phase 17) — last gate before Slack text is returned.

Checks the *claims* in a drafted answer against the evidence that backs it, and
prefers SAFER WORDING over failing the whole response. It is intentionally
surgical: it rewrites the specific overstatement and leaves the rest of the
conversational answer alone (Phase 18 — the contract constrains truth, not style).

Rejections/rewrites happen when:
  * a metric the text cites isn't available
  * the sample is too thin for the language used
  * external inspiration is presented as internal proof
  * the slice is broader than the wording implies
  * evidence is stale beyond the allowed strength
  * causal wording is used for a correlation
  * stated confidence exceeds the evidence
"""
from __future__ import annotations

import re
from typing import Optional

import evidence_contract as EC
import metric_registry as MR
from logger import get_logger

log = get_logger()

# metric words that appear in answers -> registry names
_METRIC_MENTIONS = {
    "saves": "SAVES", "save rate": "SAVES", "reach": "REACH", "impressions": "IMPRESSIONS",
    "demographics": "AGE_SPLIT", "age split": "AGE_SPLIT", "gender split": "GENDER_SPLIT",
    "profile visits": "PROFILE_VISITS", "website clicks": "WEBSITE_CLICKS",
}
# External-as-proof detection. Regex lookbehind is unreliable here (the gap
# between the keyword and the proof word is variable), so we find the proof word
# and inspect the text immediately before it: a NEGATED statement ("external
# inspiration is reference only — not proof it works") is the CORRECT disclaimer
# and must never be rewritten; only an affirmative claim is a violation.
_PROOF_WORD = re.compile(r"\b(prov(?:e|es|en|ing)|works for us|works for storelli|"
                         r"shows it works)\b", re.IGNORECASE)
_EXTERNAL_WORD = re.compile(r"\b(external|inspiration|competitor|reference|\[e\d+\])\b",
                            re.IGNORECASE)
_NEGATION_BEFORE = re.compile(r"\b(not|never|no|n'?t|isn'?t|aren'?t|without|rather than)\b"
                              r"[^.]{0,12}$", re.IGNORECASE)


def _external_proof_spans(text: str) -> list:
    """Spans of the proof word in AFFIRMATIVE external-as-proof claims only."""
    spans = []
    for m in _PROOF_WORD.finditer(text or ""):
        before = text[max(0, m.start() - 70):m.start()]
        if not _EXTERNAL_WORD.search(before):
            continue
        if _NEGATION_BEFORE.search(before):
            continue                        # negated -> honest disclaimer, leave alone
        spans.append((m.start(), m.end()))
    return spans


_CAUSAL = re.compile(
    r"(?<!not )(?<!no )(?<!never )(?<!n't )"
    r"\b(cause|causes|causing|caused by|leads to|lead to|results in|result in)\b"
    r"|(?<!not a )(?<!no )(?<!not )\bguarantee[sd]?\b", re.IGNORECASE)
# A hard engagement number for a metric we may not have.
_HARD_NUMBER = re.compile(r"\b\d[\d,\.]*\s*(saves|reach|impressions|profile visits|website clicks)\b",
                          re.IGNORECASE)
_CONFIDENCE_CLAIM = re.compile(r"\b(\d{1,3})\s*%\s*(confident|confidence|sure)\b", re.IGNORECASE)


class ValidationResult:
    def __init__(self, text: str, ok: bool, issues: list, rewritten: bool):
        self.text = text
        self.ok = ok
        self.issues = issues
        self.rewritten = rewritten

    def as_dict(self) -> dict:
        return {"ok": self.ok, "issues": list(self.issues), "rewritten": self.rewritten}


def _soften_proof_words(text: str, strength: str) -> str:
    """Downgrade proof/causal wording to something the evidence supports."""
    repl = [
        (r"\b[Ii]t'?s proven that\b", "the evidence points to"),
        (r"\bproven\b", "well-supported" if strength == EC.SUPPORTED else "showing a signal"),
        (r"\bproves\b", "supports"),
        (r"\bproof that\b", "evidence that"),
        (r"\bclearly outperforms\b",
         "looks stronger" if strength != EC.PROVEN else "clearly outperforms"),
        (r"\bclearly (shows|works)\b", r"suggests"),
        (r"\bdefinitely\b", "likely"),
        (r"\bcertainly\b", "probably"),
        (r"\balways works\b", "has worked so far"),
        (r"\bguarantees?\b", "should help"),
        (r"\bcauses\b", "is associated with"),
        (r"\bcause\b", "is associated with"),
        (r"\bcausing\b", "associated with"),
        (r"\blead to\b", "are associated with"),
        (r"\bresult in\b", "are associated with"),
        (r"\bcaused by\b", "associated with"),
        (r"\bleads to\b", "is associated with"),
        (r"\bresults in\b", "is associated with"),
    ]
    out = text
    for pat, sub in repl:
        out = re.sub(pat, sub, out)
    return out


_PROXY_MARKER = re.compile(
    r"\b(proxy|inferred|inference|likelihood|estimate|bet|not tracked|unavailable|"
    r"don'?t have|do not have|no |not |without|lack|isn'?t|aren'?t)\b", re.IGNORECASE)


def _strip_unavailable_metric_claims(text: str, available: Optional[list]) -> tuple:
    """Flag (and only in the clearest case, remove) claims about metrics we lack.

    Operates LINE BY LINE and preserves newlines, so structured answers (shot
    lists, numbered beats) are never collapsed. A mention is only *removed* when
    it asserts a hard NUMBER for an unavailable metric — the unambiguous
    fabrication. Mentions explicitly labelled proxy/inferred, or phrased as a
    denial ("we don't have saves"), are honest and left untouched.
    """
    issues = []
    avail_names = None if available is None else {str(a).upper() for a in available}
    kept = []
    for line in (text or "").splitlines():
        low = line.lower()
        drop = False
        for word, canon in _METRIC_MENTIONS.items():
            if word not in low:
                continue
            ok = (canon in avail_names) if avail_names is not None else MR.is_available(canon)
            if ok:
                continue
            if _PROXY_MARKER.search(low):
                continue                      # labelled as proxy/inference/denial -> fine
            if re.search(r"\b\d[\d,\.]*\s*" + re.escape(word), low):
                issues.append(f"hard {canon} figure without that metric")
                drop = True                   # fabricated number -> remove the line
                break
            issues.append(f"mentions unavailable metric {canon}")
        if not drop:
            kept.append(line)
    # Dropping a line can orphan the heading that introduced it. A bare
    # "What I'd do next:" with nothing under it reads as a truncation bug.
    if issues:
        pruned = []
        for i, line in enumerate(kept):
            bare = re.sub(r"[*_`#>\s]", "", line)
            if bare.endswith(":") and len(bare) <= 40:
                rest = [x for x in kept[i + 1:] if x.strip()]
                nxt = rest[0].strip() if rest else ""
                if not nxt or (nxt.startswith("*Sources") or nxt.startswith("Sources")
                               or re.sub(r"[*_`\s]", "", nxt).endswith(":")):
                    continue
            pruned.append(line)
        kept = pruned
    out = "\n".join(kept).strip()
    if not out and issues:
        # everything asserted an unavailable metric — say so instead of restoring
        # the fabricated text.
        return ("I don't have those metrics, so I can't give you numbers there.", issues)
    return (out or text), issues


# Language that already discloses weak evidence. Deliberately broad: adding a
# second caveat to an answer that already hedged reads as nervous, not honest.
_HEDGED = re.compile(
    r"\b(?:sample|small|thin|few|only \d+|n=\d+|wouldn'?t call|not proven|not measured"
    r"|inference|inferred|judg(?:e)?ment|my read|hypothes|directional|signal"
    r"|too close to call|can'?t tell|don'?t have|not enough|treat it as|guess"
    r"|early|tentative|indicativ|suggests?\b|would need)\b", re.IGNORECASE)


# "significant"/"statistically significant" applied to an effect. Plain English
# uses of the word ("a significant investment") are not the target, so the match
# requires an effect noun or a percentage nearby.
_SIGNIFICANCE = re.compile(
    r"\b(?:statistically\s+)?significant(?:ly)?\b(?=[^.]{0,40}"
    r"(?:\d+\s?%|lift|increase|decrease|difference|better|higher|improvement|uplift))"
    r"|(?<=\d%\s)\b(?:statistically\s+)?significant\b"
    r"|\b(?:a|the)\s+(?:statistically\s+)?significant\s+"
    r"(?:\+?\d+\s?%|lift|increase|difference|uplift)", re.IGNORECASE)
# An aggregate effect size — a property of many posts, not of one.
_AGGREGATE_CLAIM = re.compile(
    r"[+\-]?\d+(?:\.\d+)?\s?%\s*(?:lift|higher|better|increase|uplift|great rate)"
    r"|\blift\s+(?:of\s+)?[+\-]?\d+(?:\.\d+)?\s?%"
    r"|\bgreat rate\b[^.]{0,30}\d+(?:\.\d+)?\s?%", re.IGNORECASE)
_EXAMPLE_NOTE = re.compile(r"example[s]? of the pattern|illustrat|not the measurement",
                           re.IGNORECASE)


def _designify(phrase: str) -> str:
    """Replace an unearned significance claim with sample-scoped wording."""
    out = re.sub(r"\bstatistically\s+significantly?\b", "measurably", phrase,
                 flags=re.IGNORECASE)
    out = re.sub(r"\bsignificantly\b", "measurably", out, flags=re.IGNORECASE)
    out = re.sub(r"\bsignificant\b", "clear", out, flags=re.IGNORECASE)
    return out


def validate(text: str, answer: Optional[dict] = None, available_metrics: Optional[list] = None,
             scope: Optional[dict] = None) -> ValidationResult:
    """Validate a drafted answer. Returns safer text rather than nothing.

    `answer` is an evidence_contract.build_answer() dict (claim_strength etc.).
    `available_metrics` overrides live metric availability (tests / cached packs).
    `scope` is a resolve_scope() result, used to require slice disclosure.
    """
    original = text or ""
    issues: list = []
    strength = (answer or {}).get("claim_strength", EC.INFERRED)
    out = original

    # 1) external inspiration framed as internal proof — always a hard issue.
    #    Rewrite at SENTENCE level: splicing individual words produced garbled
    #    text when one sentence carried several proof phrases.
    if _external_proof_spans(out):
        issues.append("external inspiration framed as internal proof")
        rebuilt = []
        for sentence in re.split(r"(?<=[.!?])\s+", out):
            if _external_proof_spans(sentence):
                rebuilt.append("External references are execution inspiration only — "
                               "not proof for Storelli.")
            else:
                rebuilt.append(sentence)
        out = " ".join(rebuilt)

    # 2) hard numbers for metrics we may not have.
    for m in _HARD_NUMBER.finditer(original):
        canon = _METRIC_MENTIONS.get(m.group(1).lower(), m.group(1).upper())
        ok = (canon in {str(a).upper() for a in available_metrics}) if available_metrics is not None \
            else MR.is_available(canon)
        if not ok:
            issues.append(f"hard {canon} figure without that metric")

    # 3) unavailable-metric claims.
    out, metric_issues = _strip_unavailable_metric_claims(out, available_metrics)
    issues.extend(metric_issues)

    # 4) language stronger than the evidence allows (incl. causal wording).
    if EC.overstates(out, strength):
        issues.append(f"language overstates {strength} evidence")
        out = _soften_proof_words(out, strength)
    if _CAUSAL.search(out):
        issues.append("causal wording for a correlation")
        out = _soften_proof_words(out, strength)

    # 5) weak evidence + confident wording -> add the honest caveat once.
    #    A MISSING sample_size is not evidence of a large sample. Composed
    #    (LLM) answers frequently arrive without one, and skipping the check
    #    there is how a thin slice gets described in flat, confident prose —
    #    so an unhedged answer on weak evidence is caveated either way, and the
    #    count is only cited when we actually know it.
    n = (answer or {}).get("sample_size", 0)
    weak = strength in (EC.DIRECTIONAL, EC.INFERRED, EC.UNKNOWN)
    thin = weak and (not n or n < EC.SUPPORTED_MIN_SAMPLE)
    if thin and not _HEDGED.search(out):
        issues.append("thin sample not disclosed" if n else "weak evidence not hedged")
        out = out.rstrip()
        out += (f" Worth noting the sample is still small (n={n})." if n else
                " Worth flagging that this rests on thin evidence — treat it as a "
                "read, not a proven result.")

    # 5b) statistical-significance language that was never computed.
    #     Nothing in this pipeline runs a significance test, so "a significant
    #     +19% lift" claims a property of the data we have not established. The
    #     honest form scopes the number to the sample it came from.
    if _SIGNIFICANCE.search(out):
        issues.append("significance language without a significance test")
        out = _SIGNIFICANCE.sub(lambda m: _designify(m.group(0)), out)

    # 5c) an aggregate effect illustrated by individual reels.
    #     A lift is a property of the PATTERN across many posts; one reel cannot
    #     demonstrate it. When the only citations are individual posts, say what
    #     they are — examples of the pattern, not the measurement behind it.
    if _AGGREGATE_CLAIM.search(out) and re.search(r"\[S\d+\]", out) \
            and not _EXAMPLE_NOTE.search(out):
        issues.append("aggregate effect illustrated by individual reels")
        out = out.rstrip() + ("\n_The linked reels are examples of the pattern, "
                              "not the measurement behind the lift._")

    # 6) broadened slice presented as narrow.
    if scope and scope.get("relaxed") and scope.get("disclosure"):
        if not re.search(r"\b(broadly|more broadly|across|overall|only \d+|exact)\b",
                         out, re.IGNORECASE):
            issues.append("relaxed scope not disclosed")
            out = out.rstrip() + f" ({scope['disclosure']}.)"

    # 7) stated numeric confidence that the evidence doesn't support.
    conf = (answer or {}).get("confidence", 0.0)
    for m in _CONFIDENCE_CLAIM.finditer(out):
        try:
            stated = int(m.group(1)) / 100.0
        except ValueError:
            continue
        if stated > conf + 0.15:
            issues.append("stated confidence exceeds evidence")
            out = out.replace(m.group(0), "reasonably confident")

    # 8) UNKNOWN must not assert an answer.
    if strength == EC.UNKNOWN and not re.search(
            r"\b(don'?t have|not enough|no .{0,20}evidence|can'?t say|unknown|would need)\b",
            out, re.IGNORECASE):
        issues.append("UNKNOWN answer asserts a conclusion")
        out = ("We don't have the data to answer that yet. " + out).strip()

    rewritten = out.strip() != original.strip()
    if issues:
        log.info("claim validation: %d issue(s) -> %s", len(issues),
                 "rewritten" if rewritten else "flagged only")
    return ValidationResult(out.strip(), not issues, issues, rewritten)


# ---------------------------------------------------------------------------
# Phase 18 — evidence metadata is exposed ONLY when asked
# ---------------------------------------------------------------------------
_ASKS_FOR_EVIDENCE = re.compile(
    r"\b(show me the evidence|show the evidence|how confident|what'?s the sample|"
    r"what is the sample|sample size|why are you saying|how do you know|"
    r"what'?s your confidence|is that proven|show me proof)\b", re.IGNORECASE)


def wants_evidence_detail(text: str) -> bool:
    return bool(_ASKS_FOR_EVIDENCE.search(str(text or "")))


def render_evidence_detail(answer: dict) -> str:
    """A compact, human evidence note — only for when the user actually asks."""
    s = answer.get("sufficiency", {}) or {}
    bits = []
    strength = answer.get("claim_strength", EC.INFERRED)
    bits.append(f"Strength: {strength.lower()}")
    if s.get("sample_size"):
        bits.append(f"{s['sample_size']} internal example(s)"
                    + (f", {s['positive_examples']} strong" if s.get("positive_examples") else ""))
    if s.get("specificity") and s["specificity"] != "unspecified":
        bits.append(f"scope: {s['specificity']}")
    if s.get("freshness_days") is not None:
        bits.append(f"data ~{int(s['freshness_days'])}d old")
    line = " · ".join(bits)
    lims = answer.get("limitations") or []
    if lims:
        line += "\nCaveats: " + "; ".join(dict.fromkeys(lims))[:220]
    return line


# ---------------------------------------------------------------------------
# shared safety net — applied to EVERY conversational answer
# ---------------------------------------------------------------------------
def validate_response(text: str, user_text: str = "",
                      available_metrics: Optional[list] = None) -> ValidationResult:
    """The always-on subset that needs no evidence object: it catches the failure
    modes that are never acceptable regardless of which handler answered —
    external-as-proof, causal wording, and asserting metrics we don't have.

    Deliberately conservative about rewriting: ordinary confident-but-fair
    strategist language is left untouched (Phase 18), because this is a truth
    guard, not a style filter.
    """
    if not str(text or "").strip():
        return ValidationResult(text or "", True, [], False)
    if available_metrics is None:
        available_metrics = [n for n in MR.REGISTRY if MR.is_available(n)]
    # SUPPORTED ceiling: "proven/definitely/causes" get softened; "stronger",
    # "worth testing", "I'd prioritize" survive.
    answer = {"claim_strength": EC.SUPPORTED, "confidence": 0.7, "sample_size": 0}
    return validate(text, answer=answer, available_metrics=available_metrics)
