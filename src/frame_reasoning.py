"""Reasoning inside an active decision frame.

Two behaviours the frame makes possible, both of which the brain previously got
wrong:

1. CONSTRAINED RECOMMENDATION — "what should we shoot?" asked inside a frame is
   answered from the frame's evidence territory first, broadening only when the
   constrained pool is genuinely too weak, and SAYING SO when it broadens. The
   old path re-ranked the global pool and silently changed both the product and
   the optimisation criterion.

2. EPISTEMIC CHALLENGE — "are you sure? what would change your mind?" is answered
   about the recommendation actually on the table, separating how confident we
   are in the TERRITORY from how confident we are in THIS EXECUTION, naming the
   strongest counterargument and the concrete evidence that would reverse the
   call. The old path rendered one fixed confidence paragraph.

Nothing here changes scoring or evidence rules: it selects and compares what the
existing ranker already produced, and every claim still passes the Evidence
Contract and the claim validator on the way out.
"""
from __future__ import annotations

import re
from typing import Optional

import decision_frame as DF
import evidence_contract as EC
import idea_retrieval as IR
from logger import get_logger

log = get_logger()

# Tiers, weakest constraint last. Reported so an answer can be honest about how
# far it had to travel from the frame to find something worth shooting.
TIER_IN_SCOPE = "in_scope"            # product + pattern both match the frame
TIER_SAME_TERRITORY = "same_territory"  # product matches, pattern is adjacent
TIER_ADJACENT = "adjacent_product"    # related product, same evidence territory
TIER_GLOBAL = "global"                # outside the frame entirely


def _norm(v) -> str:
    return re.sub(r"\s*/\s*", "/", re.sub(r"\s+", " ", str(v or "").strip().lower()))


def _idea_text(idea: dict) -> str:
    return " ".join(str(idea.get(k, "")) for k in (
        "IDEA_TITLE", "REFINED_IDEA_TITLE", "HOOK", "REFINED_HOOK", "FORMAT",
        "CONCEPT", "REFINED_CONCEPT", "PRODUCT", "ICP", "SOURCE_PROFILE_NAME"))


def _matches_any(idea: dict, values, fields) -> bool:
    if not values:
        return False
    hay = _norm(" ".join(str(idea.get(f, "")) for f in fields))
    text = _norm(_idea_text(idea))
    for v in values:
        n = _norm(v)
        if not n:
            continue
        pat = rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])"
        if re.search(pat, hay) or re.search(pat, text):
            return True
        # a multi-word product matches on its distinctive head word too
        head = n.split()[0]
        if len(head) >= 5 and re.search(
                rf"(?<![A-Za-z0-9]){re.escape(head)}(?![A-Za-z0-9])", hay):
            return True
    return False


def classify_tier(idea: dict, frame: dict) -> str:
    sc = (frame or {}).get("scope") or {}
    prod_hit = _matches_any(idea, sc.get("product"), ("PRODUCT",))
    pattern = (sc.get("hook") or []) + (sc.get("format") or [])
    pat_hit = _matches_any(idea, pattern, ("HOOK", "REFINED_HOOK", "FORMAT"))
    if prod_hit and pat_hit:
        return TIER_IN_SCOPE
    if prod_hit:
        return TIER_SAME_TERRITORY
    if pat_hit:
        return TIER_ADJACENT
    return TIER_GLOBAL


# ---------------------------------------------------------------------------
# objective-aware ordering (does NOT change any score)
# ---------------------------------------------------------------------------
def _key_for(objective: str):
    """Sort key per optimisation goal. These reorder existing scores — no score
    is recomputed, and no new scoring formula is introduced."""
    n = IR._num
    if objective == DF.PRACTICALITY:
        return IR._shoot_priority_key
    if objective == DF.LEARN_NEW:
        return lambda i: (n(i.get("NOVELTY_SCORE")), n(i.get("IDEA_SCORE")))
    if objective == DF.MAX_COMMENTS:
        return lambda i: (n(i.get("STRATEGIC_PRIORITY_SCORE")), n(i.get("IDEA_SCORE")))
    if objective == DF.EXPLOIT_LEARNING:
        # closeness to the established evidence, then overall quality
        return lambda i: (n(i.get("EVIDENCE_FIT_SCORE")), n(i.get("IDEA_SCORE")))
    return lambda i: (n(i.get("IDEA_SCORE")), n(i.get("STRATEGIC_PRIORITY_SCORE")))


_MIN_IN_SCOPE = 1        # one genuinely in-frame concept is enough to answer


def constrained_ideas(ideas: list, frame: dict, objective: str) -> dict:
    """Tiered retrieval inside the frame.

    Returns {picked, tier, broadened, pool_by_tier, alternative}. Broadening is
    only allowed when the tighter tiers are empty — never because a global idea
    happens to score higher, which is exactly how the old path drifted off topic.
    """
    pool = [i for i in (ideas or []) if IR.eligible(i)]
    by_tier: dict = {TIER_IN_SCOPE: [], TIER_SAME_TERRITORY: [],
                     TIER_ADJACENT: [], TIER_GLOBAL: []}
    for idea in pool:
        by_tier[classify_tier(idea, frame)].append(idea)
    key = _key_for(objective)
    for tier in by_tier:
        by_tier[tier].sort(key=key, reverse=True)

    for tier in (TIER_IN_SCOPE, TIER_SAME_TERRITORY, TIER_ADJACENT):
        if len(by_tier[tier]) >= _MIN_IN_SCOPE:
            chosen = by_tier[tier]
            # the strongest thing OUTSIDE the frame, so the answer can say what it
            # is passing up rather than pretending the frame is the whole world
            outside = by_tier[TIER_GLOBAL][:1]
            return {"picked": chosen, "tier": tier, "broadened": False,
                    "pool_by_tier": by_tier,
                    "alternative": (chosen[1:2] or outside or [None])[0]}
    return {"picked": by_tier[TIER_GLOBAL], "tier": TIER_GLOBAL, "broadened": True,
            "pool_by_tier": by_tier,
            "alternative": (by_tier[TIER_GLOBAL][1:2] or [None])[0]}


# ---------------------------------------------------------------------------
# comparative reasoning — why this one over the next best
# ---------------------------------------------------------------------------
def _title(idea: dict) -> str:
    return IR._field(idea, "REFINED_IDEA_TITLE", "IDEA_TITLE") or "Untitled"


def _territory_phrase(frame: dict) -> str:
    sc = (frame or {}).get("scope") or {}
    hooks = sc.get("hook") or []
    fmts = sc.get("format") or []
    if hooks and fmts:
        return f"{hooks[0]} + {fmts[0]}"
    return (hooks or fmts or ["the current"])[0]


def compare_reason(pick: dict, alt: Optional[dict], frame: dict,
                   objective: str) -> str:
    """Why the pick beats the closest alternative, in evidence terms.

    Comparative rather than numeric: a score may support the reasoning but must
    not stand in for it.
    """
    n = IR._num
    if not alt:
        return (f"Nothing else in the {_territory_phrase(frame)} territory is close "
                f"enough to argue for instead.")
    bits = []
    ev_p, ev_a = n(pick.get("EVIDENCE_FIT_SCORE")), n(alt.get("EVIDENCE_FIT_SCORE"))
    if ev_p > ev_a + 4:
        bits.append("it sits closer to the evidence we just looked at")
    elif ev_a > ev_p + 4:
        bits.append(f"{_title(alt)} has the tighter evidence fit, but")
    cl_p, cl_a = n(pick.get("EXECUTION_CLARITY_SCORE")), n(alt.get("EXECUTION_CLARITY_SCORE"))
    if cl_p > cl_a + 4:
        bits.append("the execution is more obvious to shoot")
    nv_p, nv_a = n(pick.get("NOVELTY_SCORE")), n(alt.get("NOVELTY_SCORE"))
    if nv_a > nv_p + 8:
        bits.append(f"{_title(alt)} is the fresher idea, though less anchored")
    if objective == DF.PRACTICALITY:
        bits.append("and it's the easier build of the two")
    if not bits:
        pick_tier, alt_tier = classify_tier(pick, frame), classify_tier(alt, frame)
        rank = {TIER_IN_SCOPE: 0, TIER_SAME_TERRITORY: 1, TIER_ADJACENT: 2,
                TIER_GLOBAL: 3}
        if rank[pick_tier] < rank[alt_tier]:
            bits.append(f"it sits squarely in the {_territory_phrase(frame)} territory "
                        f"we just established, while {_title(alt)} is one step out")
        else:
            hooks = ((frame or {}).get("scope") or {}).get("hook") or []
            same = hooks and _matches_any(pick, hooks[:1], ("HOOK", "REFINED_HOOK"))
            bits.append(
                f"both sit in the same territory and score alike, so the tiebreak is "
                f"execution: {_title(pick)} carries the {hooks[0]} opening we have "
                f"evidence for" if same else
                f"the scores are level, so I'd pick on which opening you can shoot "
                f"most cleanly — and {_title(pick)} has the more concrete first beat")
    return f"I'd take it over *{_title(alt)}* because " + ", ".join(bits) + "."


# ---------------------------------------------------------------------------
# confidence separation (pattern vs execution vs recommendation)
# ---------------------------------------------------------------------------
_STRENGTH_WORD = {EC.PROVEN: "strong", EC.SUPPORTED: "reasonably strong",
                  EC.DIRECTIONAL: "directional", EC.INFERRED: "thin",
                  EC.UNKNOWN: "not established"}


def confidence_split(idea: Optional[dict], frame: dict,
                     pattern_strength: str = "") -> dict:
    """Three separate confidences, because they are genuinely different claims.

    Evidence that `Gloves + Education` works is not evidence that one specific
    glove-care script is the best expression of it. Collapsing them into a single
    "medium" is what made the old answer uninformative.
    """
    findings = (frame or {}).get("prior_findings") or []
    refs = (frame or {}).get("evidence_refs") or []
    pattern = (pattern_strength or (frame or {}).get("confidence")
               or (EC.SUPPORTED if len(refs) >= 3 else
                   EC.DIRECTIONAL if refs else EC.INFERRED))
    n = IR._num
    ev_fit = n((idea or {}).get("EVIDENCE_FIT_SCORE"))
    clarity = n((idea or {}).get("EXECUTION_CLARITY_SCORE"))
    if not idea:
        execution = EC.UNKNOWN
    elif ev_fit >= 85 and clarity >= 85:
        execution = EC.DIRECTIONAL
    elif ev_fit >= 70:
        execution = EC.INFERRED
    else:
        execution = EC.UNKNOWN
    # A recommendation can never be more certain than its weaker leg.
    order = [EC.UNKNOWN, EC.INFERRED, EC.DIRECTIONAL, EC.SUPPORTED, EC.PROVEN]
    overall = order[min(order.index(pattern), order.index(execution))]
    return {"pattern": pattern, "execution": execution, "recommendation": overall,
            "pattern_word": _STRENGTH_WORD.get(pattern, "unclear"),
            "execution_word": _STRENGTH_WORD.get(execution, "unclear"),
            "recommendation_word": _STRENGTH_WORD.get(overall, "unclear"),
            "supporting_findings": findings[:2], "evidence_refs": refs[:3]}


# ---------------------------------------------------------------------------
# falsification contract
# ---------------------------------------------------------------------------
def falsification_conditions(frame: dict, idea: Optional[dict],
                             alt: Optional[dict], conf: dict) -> list:
    """Concrete evidence that would REVERSE the recommendation — not generic
    caution. Each item names something observable."""
    sc = (frame or {}).get("scope") or {}
    scope_phrase = DF.describe_scope(frame)
    out = []
    if conf["pattern"] in (EC.INFERRED, EC.DIRECTIONAL, EC.UNKNOWN):
        settled = ("still unestablished" if conf["pattern"] == EC.UNKNOWN
                   else f"{conf['pattern_word']} rather than settled")
        out.append(f"the {scope_phrase} slice turning out to be too thin once we "
                   f"count the posts behind it — the pattern is {settled}")
    if alt is not None:
        out.append(f"*{_title(alt)}* turning out to have materially stronger internal "
                   f"support once we compare them directly")
    if sc.get("icp"):
        out.append(f"the {sc['icp'][0]} slice behaving differently from the "
                   f"account average once it has enough posts to read")
    out.append("a first test landing flat — one weak result on this exact "
               "execution would move me off it quickly, even if the territory holds")
    if (frame or {}).get("prior_findings"):
        out.append("the next refresh weakening the pattern this rests on")
    out.append("a metric we currently proxy becoming available and disagreeing "
               "with the read")
    return out[:5]


def challenge_pack(frame: dict, idea: Optional[dict], alt: Optional[dict],
                   objective: str = "", pattern_strength: str = "",
                   freshness: str = "", contradictions: int = 0) -> dict:
    """Everything needed to reason about a challenge, assembled once.

    Deliberately a PACK rather than prose: the renderer varies its wording, and
    the LLM path may compose from it, but neither can invent a support, an
    alternative, or a falsification condition that isn't here.
    """
    conf = confidence_split(idea, frame, pattern_strength)
    support = list(conf["supporting_findings"])
    against = []
    _order = [EC.UNKNOWN, EC.INFERRED, EC.DIRECTIONAL, EC.SUPPORTED, EC.PROVEN]
    if _order.index(conf["execution"]) < _order.index(conf["pattern"]):
        against.append("the evidence backs the territory, not this specific script — "
                       "we're extrapolating from the pattern to this execution")
    if contradictions:
        against.append(f"{contradictions} contradicting signal(s) sit in the same slice")
    if not (frame or {}).get("evidence_refs"):
        against.append("no cited internal rows are attached to this frame yet")
    if conf["pattern"] in (EC.INFERRED, EC.DIRECTIONAL):
        against.append(f"the pattern underneath it is {conf['pattern_word']} rather "
                       f"than settled, so the foundation could move")
    if alt is not None:
        against.append(f"*{_title(alt)}* is a live alternative rather than an also-ran")
    return {
        "current_recommendation": _title(idea) if idea else None,
        "scope": DF.describe_scope(frame),
        "objective": objective or (frame or {}).get("optimization_goal") or "",
        "confidence": conf,
        "strongest_support": support,
        "strongest_counterargument": (against[0] if against else
                                      "nothing substantive argues against it yet"),
        "other_counterarguments": against[1:3],
        "alternative": _title(alt) if alt else None,
        "falsification_conditions": falsification_conditions(frame, idea, alt, conf),
        "freshness": freshness,
        "unknowns": ["outcome KPIs are not tracked per reel, so this is a bet on a "
                     "pattern rather than a forecast"],
    }


# ---------------------------------------------------------------------------
# renderers — natural prose, deliberately varied, never a fixed template
# ---------------------------------------------------------------------------
def _pick_variant(seed_text: str, variants: list) -> str:
    """Deterministic but text-dependent phrasing choice, so two different turns
    don't come back in the same shape. (Math.random-free by design: the same
    question always renders the same way, different questions vary.)"""
    return variants[sum(ord(c) for c in str(seed_text or "x")) % len(variants)]


def _first_beat(idea: dict, limit: int = 110) -> str:
    shot = IR._field(idea, "REFINED_SHOT_LIST", "SHOT_LIST") or \
        IR._field(idea, "REFINED_CONCEPT", "CONCEPT")
    beat = re.split(r"(?<=[.!?])\s+|\n|·", str(shot))[0].strip(" -•\t")
    beat = re.sub(r"\s+", " ", beat)
    return (beat[:limit].rstrip() + "…") if len(beat) > limit else beat


def render_constrained_recommendation(sel: dict, frame: dict, objective: str,
                                      objective_explicit: bool, mode: str) -> str:
    """The Turn-2 answer: a decision made inside the frame, saying what it stayed
    inside and what it is passing up."""
    import slack_response_style as st
    picked = sel["picked"]
    if not picked:
        return ""
    pick = picked[0]
    alt = sel.get("alternative")
    reg = IR.SourceRegistry()
    s_txt, _e = IR._cite_idea(pick, reg)
    scope = DF.describe_scope(frame)
    territory = _territory_phrase(frame)

    if sel["broadened"]:
        lead = (f"Nothing in the {scope} territory we just looked at is strong enough "
                f"to build next week on, so this steps outside it: I'd shoot "
                f"*{_title(pick)}*.")
    else:
        opener = _pick_variant(_title(pick), [
            f"Staying with the {scope} signal we just established, I'd shoot "
            f"*{_title(pick)}* first.",
            f"Off the back of that {scope} read, *{_title(pick)}* is the one I'd shoot.",
            f"Inside the {scope} evidence we just went through, *{_title(pick)}* is "
            f"the strongest next shoot.",
        ])
        lead = opener
    body = [lead]
    beat = _first_beat(pick)
    if beat:
        body.append(f"It keeps the {territory} territory intact and opens on: {beat}")
    body.append(compare_reason(pick, alt, frame, objective))
    strength = (frame or {}).get("confidence")
    if strength in (EC.INFERRED, EC.UNKNOWN):
        body.append(f"Worth carrying forward that the {scope} evidence behind this is "
                    f"thin — a small sample, so treat the pick as a test rather than "
                    f"a safe bet.")
    elif strength == EC.DIRECTIONAL:
        body.append(f"The {scope} signal is directional rather than settled, so this "
                    f"is a considered bet, not a proven call.")
    if objective_explicit:
        body.append(f"Ranked for *{objective}*, as you asked.")
    elif objective == DF.EXPLOIT_LEARNING:
        body.append(f"I'm reading \"best\" here as best use of the {scope} learning we "
                    f"just found rather than the highest-scoring idea overall — say "
                    f"the word if you meant the whole calendar instead.")
    if not sel["broadened"] and sel["pool_by_tier"].get(TIER_GLOBAL):
        outside = sel["pool_by_tier"][TIER_GLOBAL][0]
        if IR._num(outside.get("IDEA_SCORE")) > IR._num(pick.get("IDEA_SCORE")) + 2:
            body.append(f"For the record *{_title(outside)}* scores higher across the "
                        f"whole calendar, but it's a separate bet rather than "
                        f"exploiting what we just learned.")
    src = reg.render()
    return st.render_ceo_summary(" ".join(body),
                                 move=f"Block the shoot day for *{_title(pick)}*.",
                                 sources=(f"{src}\n{IR._NOT_PROOF}" if src else ""),
                                 mode=mode)


def render_challenge(pack: dict, mode: str) -> str:
    """The Turn-3 answer: reasoning about THIS recommendation, with the pattern
    and the execution held apart, and concrete falsification.

    Rendered as a few short lines rather than one long paragraph — the length
    cap trims on line boundaries, so a long answer loses a whole thought instead
    of stopping mid-sentence. Still prose, still no mandatory headings; the
    opening and the falsification lead-in both vary with the subject.
    """
    import slack_response_style as st
    conf = pack["confidence"]
    rec = pack["current_recommendation"]
    subject = f"*{rec}*" if rec else "that call"
    lines = []

    lines.append(_pick_variant(rec or pack["scope"], [
        "Moderately confident, not certain — and the two halves aren't equally solid.",
        "Fair to push. Two things worth separating before I answer.",
        "Confident enough to act on, not confident enough to stop checking.",
    ]))
    # Which leg is actually weaker decides the sentence. Asserting "more
    # confident in the territory" when the territory is the thinner of the two
    # states the comparison backwards.
    _rank = [EC.UNKNOWN, EC.INFERRED, EC.DIRECTIONAL, EC.SUPPORTED, EC.PROVEN]
    pat_i, exe_i = _rank.index(conf["pattern"]), _rank.index(conf["execution"])
    if pat_i > exe_i:
        lines.append(f"I'm more confident in the {pack['scope']} territory "
                     f"({conf['pattern_word']} on the evidence we cited) than I am "
                     f"that {subject} is the best expression of it — that part is "
                     f"{conf['execution_word']}.")
    elif exe_i > pat_i:
        lines.append(f"{subject} is a {conf['execution_word']} execution, but the "
                     f"{pack['scope']} pattern underneath it is the weaker leg — "
                     f"{conf['pattern_word']} on the evidence we cited. The idea is "
                     f"cleaner than the case for it.")
    else:
        lines.append(f"Both halves sit at the same level: the {pack['scope']} "
                     f"pattern and {subject} as an execution of it are each "
                     f"{conf['pattern_word']} on what we have.")
    if pack["strongest_support"]:
        support = pack["strongest_support"][0].rstrip(". ")
        if len(support) > 140:
            support = support[:140].rsplit(" ", 1)[0] + "…"
        lines.append(f"What holds it up: {support}"
                     + ("" if support.endswith("…") else "."))

    conds = pack["falsification_conditions"][:3]
    counter = pack["strongest_counterargument"]
    alt_name = pack.get("alternative")
    against = f"The strongest argument against it: {counter}."
    # Name the alternative once. If a falsification condition already names it,
    # that mention carries more information, so drop the bare "weigh it against".
    named_in_counter = bool(alt_name) and alt_name.lower() in counter.lower()
    named_in_conds = bool(alt_name) and any(alt_name.lower() in c.lower() for c in conds)
    if alt_name and not named_in_counter and not named_in_conds:
        against += f" *{alt_name}* is what I'd weigh it against."
    lines.append(against)

    if conds:
        head = _pick_variant(rec or "x", [
            "What would actually change my mind:",
            "Concretely, what would move me off it:",
            "I'd reverse this on any of:",
        ])
        lines.append(head)
        lines.extend(f"  • {c[:1].upper() + c[1:]}" for c in conds)
    if pack.get("unknowns"):
        u = pack["unknowns"][0]
        lines.append(u[:1].upper() + u[1:] + ".")
    return st.compact_slack_response("\n".join(lines), mode)
