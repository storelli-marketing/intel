"""Slack Decision Trace / Provenance Layer.

Turns an evidence pack into a hyper-concise, source-linked "Why" trace: each step
is a ≤3-word label + a short value + the source ids that back it. No reasoning,
no chain-of-thought, no model scratchpad — just the evidence path.

Language rules enforced here:
- IDEA_SCORE is a *model/evidence idea score*, never a "performance score".
- Internal performance proof comes ONLY from Storelli internal analyzed videos /
  winning profiles ([S]). External inspiration ([E]) is execution reference only,
  never proof.
- KPI outcomes are NOT tracked, so KPI steps are always labelled inferred / proxy;
  comments are "comment-likelihood inferred" unless real comment data exists.
"""
from __future__ import annotations

import re
from typing import Optional

EVIDENCE_TYPES = {"internal", "connection", "external", "inference", "risk", "score",
                  "notion", "calendar", "similar", "topic", "format", "fix", "verdict"}
CONFIDENCE = {"High", "Medium", "Thin"}

_MAX_LABEL_WORDS = 3
_MAX_VALUE_WORDS = 12


# ---------------------------------------------------------------------------
# step construction
# ---------------------------------------------------------------------------
def _trim_label(label: str) -> str:
    return " ".join(str(label or "").split()[:_MAX_LABEL_WORDS])


def _trim_value(value: str, n: int = _MAX_VALUE_WORDS) -> str:
    v = re.split(r"[.\n|]", str(value or "").strip())[0].strip().strip("—-· ")
    w = v.split()
    if not w:
        return ""
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def step(label: str, value: str, refs=None, evidence_type: str = "inference",
         confidence: str = "Medium") -> dict:
    return {
        "label": _trim_label(label),
        "value": _trim_value(value),
        "refs": [str(r).strip() for r in (refs or []) if str(r).strip()],
        "evidence_type": evidence_type if evidence_type in EVIDENCE_TYPES else "inference",
        "confidence": confidence if confidence in CONFIDENCE else "Medium",
    }


def label_ok(label: str) -> bool:
    n = len(str(label or "").split())
    return 1 <= n <= _MAX_LABEL_WORDS


# ---------------------------------------------------------------------------
# KPI inference (Part C) — never a proven metric; tied to storytelling structure
# ---------------------------------------------------------------------------
def kpi_value(structure_or_hook: str) -> str:
    """A proxy/inferred KPI bet keyed to the storytelling structure/hook. We do
    NOT track saves/comments/views, so every bet is explicitly a proxy."""
    s = str(structure_or_hook or "").lower()
    if any(k in s for k in ("pain", "confession", "wince", "sting", "fear", "risk")):
        return "comment-likelihood inferred; saves proxy"
    if "curiosity" in s:
        return "retention/rewatch proxy; comments inferred"
    if "before" in s and "after" in s:
        return "conversion-fit / clarity (proxy)"
    if any(k in s for k in ("pov", "story", "aspiration")):
        return "engagement proxy"
    if any(k in s for k in ("demo", "proof", "protection", "reveal", "product")):
        return "saves / conversion-fit (proxy)"
    if any(k in s for k in ("education", "tutorial", "explain", "mistake", "correction")):
        return "saves / shareability proxy"
    return "engagement proxy; not tracked yet"


def kpi_step(structure_or_hook: str) -> dict:
    return step("KPI bet", kpi_value(structure_or_hook), [], "inference", "Thin")


def risk_step(text: str) -> dict:
    return step("Risk", text or "avoid overclaiming protection", [], "risk", "Medium")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------
def bullets(steps: list[dict], max_n: int = 5) -> list[str]:
    out = []
    for s in steps:
        if not s or not str(s.get("value", "")).strip():
            continue
        refs = "".join(f"[{r}]" for r in s.get("refs", []))
        out.append(f"*{s['label']}:* {s['value']}{(' ' + refs) if refs else ''}".rstrip())
        if len(out) >= max_n:
            break
    return out


def render(lead: str, steps: list[dict], move: str = "", sources: str = "",
          mode: str = "default", max_bullets: int = 5) -> str:
    """CEO-shaped answer: lead → Why (trace bullets) → My move → Sources."""
    import slack_response_style as st
    return st.render_ceo_summary(lead, why=bullets(steps, max_bullets), move=move,
                                 sources=sources, mode=mode)


# ---------------------------------------------------------------------------
# validation (Part E) — used when an LLM supplies or reorders a trace
# ---------------------------------------------------------------------------
_HARD_KPI_RE = re.compile(r"\b\d[\d,]*\s*(saves|comments|replies|likes|views|shares|followers)\b",
                          re.IGNORECASE)
_PROXY_TERMS = ("inferred", "proxy", "not tracked", "likelihood", "-fit", " fit", "bet", "estimate")
_EXTERNAL_PROOF_RE = re.compile(r"(external|inspiration|their video|reference|\[e\d+\])"
                                r"[^.]{0,40}\bprov(e|es|en|ing)\b", re.IGNORECASE)


def claims_hard_kpi(text: str) -> bool:
    """True if the text asserts a concrete engagement metric we don't track."""
    return bool(_HARD_KPI_RE.search(str(text or "")))


def external_as_proof(text: str) -> bool:
    return bool(_EXTERNAL_PROOF_RE.search(str(text or "")))


def validate_trace(steps, allowed_ids: set) -> tuple[bool, str]:
    """A trace is valid when every label is ≤3 words, every ref exists in the
    source map, no external ref is used to back an internal/score proof step, and
    any KPI step is marked inferred/proxy (never a hard metric)."""
    if not isinstance(steps, list) or not steps:
        return False, "empty trace"
    for s in steps:
        if not isinstance(s, dict):
            return False, "step not a dict"
        if not label_ok(s.get("label", "")):
            return False, f"label too long: {s.get('label')!r}"
        refs = s.get("refs", []) or []
        for r in refs:
            if str(r).strip() not in allowed_ids:
                return False, f"invented source id {r!r}"
        et = s.get("evidence_type")
        if et in ("internal", "score") and any(str(r).startswith("E") for r in refs):
            return False, "external ref backing internal proof"
        val = str(s.get("value", ""))
        if claims_hard_kpi(val):
            return False, "hard KPI metric claimed"
        if et == "inference" and _looks_like_kpi(s.get("label", "")) \
                and not any(t in val.lower() for t in _PROXY_TERMS):
            return False, "KPI not marked inferred/proxy"
        if external_as_proof(val):
            return False, "external framed as proof"
    return True, ""


def _looks_like_kpi(label: str) -> bool:
    return "kpi" in str(label or "").lower()
