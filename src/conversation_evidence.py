"""Evidence Pack Builder + conversational renderers for follow-ups.

Given the RESOLVED prior items (not a fresh retrieval), builds a small focused
evidence pack and renders a natural, source-disciplined answer for each dialogue
act. Deterministic (the agent may optionally have the LLM reword the narrative,
validated). Internal Storelli evidence [S] is proof; external inspiration [E] is
reference only; semantic connection [C]; Notion [N]; similar idea [I].
"""
from __future__ import annotations

import re
from typing import Optional

import slack_response_style as st
from idea_retrieval import _display_risk, _field, _first_sentence, _split
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"


def _title(idea: dict) -> str:
    return _field(idea, "REFINED_IDEA_TITLE", "IDEA_TITLE") or "Untitled"


def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url or "") or \
        re.search(r"instagram\.com/([\w.\-]+)/", url or "")
    return "@" + m.group(1) if m else "reference"


class _Src:
    """Ordered [S]/[C]/[E]/[N]/[I] collector; renders url-optional Slack links."""

    def __init__(self):
        self.rows = []

    def add(self, kind: str, url: str, label: str) -> str:
        n = sum(1 for (t, _, _) in self.rows if t[0] == kind) + 1
        tag = f"{kind}{n}"
        self.rows.append((tag, str(url or "").strip(), label))
        return tag

    @property
    def ids(self) -> set:
        return {t for (t, _, _) in self.rows}

    @property
    def has_external(self) -> bool:
        return any(t[0] == "E" for (t, _, _) in self.rows)

    def block(self, used: Optional[set] = None) -> str:
        lines = ["*Sources:*"]
        for tag, url, label in self.rows:
            if used is not None and tag not in used:
                continue
            lines.append(f"  [{tag}] <{url}|{label}>" if url else f"  [{tag}] {label}")
        if len(lines) == 1:
            return ""
        if self.has_external:
            lines.append(_NOT_PROOF)
        return "\n".join(lines)


def _tail(src: _Src) -> str:
    b = src.block()
    return ("\n\n" + b) if b else ""


# ---------------------------------------------------------------------------
# semantic characterization of an idea (for natural differentiation)
# ---------------------------------------------------------------------------
def _angle(idea: dict) -> str:
    low = (_field(idea, "REFINED_CONCEPT", "CONCEPT") + " " +
           _field(idea, "REFINED_HOOK", "HOOK") + " " + _title(idea)).lower()
    if any(k in low for k in ("pain", "sting", "wince", "burn", "fear", "hurt", "bare")):
        return "emotional/pain-led"
    if any(k in low for k in ("demo", "product", "protection", "proof", "test")):
        return "product/proof-led"
    if any(k in low for k in ("confidence", "aspiration", "dive", "pro", "full")):
        return "confidence/aspiration"
    return "a different execution"


def _pain_phrase(idea: dict) -> str:
    low = (_field(idea, "REFINED_CONCEPT", "CONCEPT") + " " + _title(idea)).lower()
    for kw, phrase in (("wince", "the wince before the dive"), ("sting", "the turf sting"),
                       ("turf burn", "the turf burn"), ("bare", "bare knees on turf"),
                       ("scrape", "the scrape")):
        if kw in low:
            return phrase
    return "the pain moment upfront"


def _hook_phrase(idea: dict) -> str:
    tags = str(idea.get("HOOK_TAGS", "")).lower()
    if any(k in tags for k in ("fear", "risk", "curiosity")):
        return "pain/fear"
    first = str(idea.get("HOOK_TAGS", "")).split(",")[0].strip()
    return first.lower() or "a strong hook"


def _idea_sources(idea: dict, src: _Src) -> tuple:
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "winning profile"
    s = [src.add("S", u, f"Storelli internal proof — {prof[:38]}")
         for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))[:1]]
    e = [src.add("E", u, f"External execution reference — {_handle(u)}")
         for u in _split(idea.get("EXTERNAL_REFERENCE_URLS"))[:2]]
    return s, e


# ---------------------------------------------------------------------------
# explain a SET of prior ideas (the production-example fix)
# ---------------------------------------------------------------------------
def explain_idea_set(records: list, product: str, connection: Optional[dict],
                     mode: str) -> dict:
    src = _Src()
    top = records[0]
    prof = str(top.get("SOURCE_PROFILE_NAME", "")).strip()
    urls = _split(top.get("INTERNAL_EVIDENCE_URLS"))
    s_tag = src.add("S", urls[0] if urls else "",
                    f"Storelli internal proof — {(prof or 'winning profile')[:38]}")
    c_tag = ""
    if connection:
        struct = str(connection.get("STORYTELLING_STRUCTURE", "")
                     or connection.get("CONCEPT_NAME", "")).strip()
        c_tag = src.add("C", "", f"Semantic connection — {(struct or 'pain → protection')[:44]}")

    prod = product or str(top.get("PRODUCT", "")).strip() or "this"
    hook = _hook_phrase(top)
    parts = [f"Mostly because they all sit on the strongest {prod} pattern we've seen "
             f"internally: {hook} upfront, then a simple protection demo. [{s_tag}]"]
    # #1 reasoning
    if _angle(top) == "emotional/pain-led":
        parts.append(f"*{_title(top)}* is the strongest — {_pain_phrase(top)} gives an immediate "
                     "emotional hook.")
    else:
        parts.append(f"*{_title(top)}* is the strongest — it's the cleanest execution of that pattern.")
    # differences for the rest
    if len(records) >= 2:
        diffs = []
        for r in records[1:3]:
            diffs.append(f"*{_title(r)}* leans more {_angle(r)}")
        if diffs:
            parts.append((" and ".join(diffs)) + ".")
    parts.append("So I'm not betting on three unrelated ideas — it's three executions of the "
                 f"same proven territory{(' [' + c_tag + ']') if c_tag else ''}.")
    body = " ".join(parts)
    text = st.compact_slack_response(body + _tail(src), mode)
    facts = _facts_set(records, prof, connection)
    return {"text": st.format_trace_answer(text), "facts": facts, "source_ids": src.ids,
            "src": src}


def _facts_set(records, prof, connection) -> str:
    lines = ["Question: explain WHY these specific prior ideas were recommended "
             "(reason from evidence; do not re-list them)."]
    lines.append(f"Shared internal winning profile (PROOF the format works): {prof or 'n/a'}.")
    for i, r in enumerate(records[:3], 1):
        hook = str(r.get("HOOK_TAGS", ""))[:40]
        lines.append(f"- Idea {i}: {_title(r)} — angle {_angle(r)}, product {r.get('PRODUCT','')}"
                     f"/{r.get('ICP','')}, idea_score {r.get('IDEA_SCORE','?')} "
                     f"(NOT a performance metric), hook {hook}.")
    if connection:
        lines.append(f"Semantic connection (bridge): {connection.get('STORYTELLING_STRUCTURE','')}.")
    lines.append("The first is preferred (top idea_score / top of the prior list). Internal "
                 "evidence is proof; external inspiration is reference only.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# explain / deep-dive a SINGLE prior idea
# ---------------------------------------------------------------------------
def explain_single(idea: dict, mode: str, deep: bool = False) -> dict:
    src = _Src()
    s, e = _idea_sources(idea, src)
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "our winning profile"
    concept = _first_sentence(_field(idea, "REFINED_CONCEPT", "CONCEPT"), 18)
    refs = " ".join(f"[{x}]" for x in s)
    lead = (f"*{_title(idea)}* wins because it maps straight onto {prof} — {_pain_phrase(idea)}, "
            f"then the {idea.get('PRODUCT','product')} protection is the payoff. {refs}")
    body = lead
    if deep or mode == st.MODE_DEEP:
        body += (f" The concept — {concept} — keeps one clear beat, so it's shootable now"
                 f" (risk: {_display_risk(idea)}).")
    text = st.compact_slack_response(body + _tail(src), st.MODE_DEEP if deep else mode)
    return {"text": st.format_trace_answer(text), "source_ids": src.ids, "src": src,
            "facts": f"Explain why {_title(idea)} specifically: profile {prof}, concept {concept}, "
                     f"risk {_display_risk(idea)}."}


# ---------------------------------------------------------------------------
# evidence answer — expand proof/trace for the resolved item(s)
# ---------------------------------------------------------------------------
def evidence_answer(records: list, mode: str) -> dict:
    src = _Src()
    idea = records[0]
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "winning profile"
    s = [src.add("S", u, f"Storelli internal proof — {prof[:38]}")
         for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))[:2]]
    e = [src.add("E", u, f"External execution reference — {_handle(u)}")
         for u in _split(idea.get("EXTERNAL_REFERENCE_URLS"))[:2]]
    conf = str(idea.get("CONFIDENCE", "?"))
    why = [
        f"Anchored to internal winning profile *{prof}* (confidence {conf}) — that's the proof "
        f"it works for Storelli: {' '.join('[' + x + ']' for x in s) or '(profile evidence)'}.",
        f"External inspiration is execution reference only, not proof: "
        f"{' '.join('[' + x + ']' for x in e) or '(none)'}.",
    ]
    text = st.render_ceo_summary(f"Evidence behind *{_title(idea)}*:", why=why, move="",
                                 sources=src.block(), mode=st.MODE_DEEP)
    return {"text": st.format_trace_answer(text), "source_ids": src.ids, "src": src,
            "facts": f"Show the evidence chain for {_title(idea)} (profile {prof}, confidence {conf})."}


# ---------------------------------------------------------------------------
# challenge — engage, restate confidence + caveat honestly
# ---------------------------------------------------------------------------
def challenge_answer(records: list, mode: str) -> dict:
    src = _Src()
    idea = records[0]
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "our winning profile"
    conf = str(idea.get("CONFIDENCE", "Medium")).strip().title()
    s = [src.add("S", u, f"Storelli internal proof — {prof[:38]}")
         for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))[:1]]
    refs = " ".join(f"[{x}]" for x in s)
    honest = ("high" if conf.lower() == "high" else "real but not bulletproof")
    body = (f"Fair to push. My confidence is *{conf}* — the pattern behind *{_title(idea)}* is "
            f"{honest}: it's anchored to {prof} {refs}. What I *can't* promise is the outcome — "
            "KPIs aren't tracked, so it's a bet on a proven format, not a guarantee. If you want, "
            "we shoot it as a test and let the numbers settle it.")
    text = st.compact_slack_response(body + _tail(src), mode)
    return {"text": st.format_trace_answer(text), "source_ids": src.ids, "src": src,
            "facts": f"Challenge on {_title(idea)}: confidence {conf}, profile {prof}."}


# ---------------------------------------------------------------------------
# modify — carry the concept forward to a new ICP (honest about thin proof)
# ---------------------------------------------------------------------------
def modify_answer(records: list, new_icp: str, mode: str) -> dict:
    src = _Src()
    idea = records[0]
    prof = str(idea.get("SOURCE_PROFILE_NAME", "")).strip() or "our winning profile"
    s = [src.add("S", u, f"Storelli internal proof — {prof[:38]}")
         for u in _split(idea.get("INTERNAL_EVIDENCE_URLS"))[:1]]
    refs = " ".join(f"[{x}]" for x in s)
    thin = new_icp.lower().startswith("parent")
    body = (f"We can point *{_title(idea)}* at {new_icp} — keep the same {_pain_phrase(idea)} → "
            f"protection beat, just reframe the hook around a parent watching their kid take the hit. "
            f"{refs} ")
    if thin:
        body += ("Honest caveat: our internal *proof* for Parents is thin — a real signal, not a "
                 "proven winner — so treat this as an evidence-building *test*, not a sure thing.")
    else:
        body += f"The underlying proof still comes from {prof}; the shift is framing, not evidence."
    text = st.compact_slack_response(body + _tail(src), mode)
    return {"text": st.format_trace_answer(text), "source_ids": src.ids, "src": src,
            "facts": f"Reframe {_title(idea)} for {new_icp}; internal proof {prof}; "
                     f"Parents proof thin={thin}."}
