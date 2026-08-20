"""Benchmark cases: semantic properties a strong answer must satisfy.

Each case asserts what must / must not be true of the ANSWER — not which route
handled it. Assertions are grouped so we can report an overall pass rate and a
separate CRITICAL accuracy/safety rate (fabrication, external-as-proof,
overstatement, abstention), which must be 100%.
"""
from __future__ import annotations

import re

# ---- reusable assertion helpers -------------------------------------------
# Negated disclaimers ("external inspiration is reference only — not proof") are
# the CORRECT behaviour, so the check requires an AFFIRMATIVE claim: an external
# keyword followed by a proof word with no intervening negation.
NO_EXTERNAL_AS_PROOF = ("no_external_as_proof",
                        r"(external|inspiration|competitor|reference)"
                        r"(?:(?!\bnot\b|\bnever\b|\bno\b|n't)[^.]){0,40}"
                        r"prov(e|es|en|ing)")
# Negated forms ("not a guarantee", "can't promise") are honest hedging and are
# deliberately NOT failures — only unhedged causal assertions are.
NO_CAUSAL = ("no_causal_language",
             r"(?<!not )(?<!no )(?<!never )\b(causes|caused by|leads to|results in)\b"
             r"|(?<!not a )(?<!no )\bguarantees?\b")
NO_FABRICATED_PRIVATE = ("no_fabricated_private_metric",
                         r"\b\d[\d,\.]*\s*(saves|reach|impressions|profile visits)\b")
NO_TEMPLATE_DUMP = ("no_confidence_dump", r"claim[ _]strength:|confidence:\s*0\.\d")


def case(question, must=(), must_not=(), critical=(), context=None, allow_help=False,
         notes=""):
    return {"question": question, "must": list(must), "must_not": list(must_not),
            "critical": list(critical), "context": context, "allow_help": allow_help,
            "notes": notes}


# `must` / `must_not` entries are (name, regex) applied case-insensitively to the
# answer. `critical` entries are must_not patterns whose failure is a SAFETY fail.
_SAFETY = [NO_EXTERNAL_AS_PROOF, NO_CAUSAL, NO_FABRICATED_PRIVATE, NO_TEMPLATE_DUMP]

CASES = [
    # --- recommendation / prioritisation -----------------------------------
    case("What should we shoot this week?",
         must=[("names_a_concrete_idea", r"[A-Z][\w'’\- ]{6,}")],
         critical=_SAFETY),
    case("Which one should we shoot first?",
         must=[("commits_to_one", r"\b(first|start with|shoot)\b")],
         critical=_SAFETY),
    case("Why did you recommend those?",
         context="ideas",
         must=[("explains_not_relists", r"\b(because|pattern|territory|evidence|proof)\b")],
         must_not=[("not_a_ranked_relist", r"(?m)^\*?1\. .*score")],
         critical=_SAFETY),
    # --- what works, by slice ----------------------------------------------
    case("What is working for BodyShield?",
         must=[("mentions_bodyshield_or_pattern", r"(bodyshield|leggings|pattern|hook|demo)")],
         critical=_SAFETY),
    case("What is not working for BodyShield?",
         must=[("addresses_weakness", r"(weak|avoid|not working|underperform|thin|don'?t)")],
         critical=_SAFETY),
    case("What works for goalkeeper gloves?",
         must=[("addresses_gloves_or_scope", r"(glove|goalkeep|broadly|across|don'?t have)")],
         critical=_SAFETY),
    case("What works for aspiring pro goalkeepers?",
         must=[("scope_honest", r"(aspiring|broadly|across|only \d+|don'?t have|thin)")],
         critical=_SAFETY,
         notes="must not present broad evidence as ICP-specific proof"),
    case("What works for parents?",
         must=[("flags_thin_parent_evidence", r"(thin|only \d+|not proven|don'?t have|test)")],
         must_not=[("no_unqualified_proof", r"\bparents?\b[^.]{0,40}\bproven\b")],
         critical=_SAFETY,
         notes="Parents evidence is known-thin; must abstain or hedge + suggest a test"),
    # --- craft -------------------------------------------------------------
    case("What hook should we use?",
         must=[("names_a_hook", r"(curiosity|pain|fear|education|authority|hook)")],
         critical=_SAFETY),
    case("How should we film this?",
         context="ideas",
         must=[("gives_execution_detail", r"(shot|beat|open|film|cta|second)")],
         critical=_SAFETY),
    case("What should the first 3 seconds look like?",
         # asking which idea is a legitimate answer shape when the subject is
         # genuinely unresolved — what must NOT happen is a confident guess.
         must=[("opening_or_clarifies", r"(first|open|hook|second|which idea|which one|name it)")],
         critical=_SAFETY),
    case("How long should this reel be?",
         must=[("duration_evidence_or_limits",
                r"(sec|second|bucket|duration|don'?t have|not exact|approximat)")],
         critical=_SAFETY,
         notes="must cite exact duration if available, else name the bucket/approximation limit"),
    case("Should this be POV, demo, tutorial or talking head?",
         must=[("picks_or_explains", r"(pov|demo|tutorial|talking head)")],
         critical=_SAFETY),
    # --- judgement ---------------------------------------------------------
    case("Is this idea actually good?",
         context="ideas",
         must=[("gives_a_verdict", r"(worth|shoot|revise|strong|weak|yes|no|would)")],
         critical=_SAFETY),
    case("Is this proven or just interesting?",
         context="ideas",
         must=[("distinguishes_proof", r"(proven|signal|evidence|inference|not.*proven|supported)")],
         critical=_SAFETY),
    case("Show me the evidence.",
         context="ideas",
         must=[("surfaces_evidence", r"(evidence|proof|internal|profile|sample|\[s\d)")],
         critical=_SAFETY),
    # --- inspiration -------------------------------------------------------
    case("Find videos to inspire this.",
         context="ideas",
         must=[("external_is_reference", r"(reference|inspiration|steal|not proof|execution)")],
         critical=_SAFETY,
         notes="external must be execution reference only"),
    case("What can we learn from this competitor video?",
         must=[("treats_as_reference", r"(reference|execution|steal|adapt|not proof|inspiration)")],
         critical=_SAFETY),
    # --- self-update -------------------------------------------------------
    case("What new things did the brain find?",
         must=[("refresh_aware", r"(refresh|new|nothing new|hasn'?t run|reel|reference)")],
         critical=_SAFETY),
    case("Did any winning pattern get stronger?",
         must=[("change_aware", r"(stronger|weaker|no change|refresh|pattern|hasn'?t run)")],
         critical=_SAFETY),
    case("Should we regenerate ideas?",
         must=[("gives_yes_no_with_reason", r"(yes|not yet|no|wouldn'?t|regenerat)")],
         critical=_SAFETY),
    # --- gaps / experiments -------------------------------------------------
    case("Where are our content gaps?",
         must=[("names_a_gap", r"(gap|thin|missing|parent|barely|need)")],
         critical=_SAFETY),
    case("What have we barely tested?",
         must=[("names_untested", r"(barely|thin|little|haven'?t|few|gap)")],
         critical=_SAFETY),
    case("What 10 experiments should we run?",
         must=[("returns_a_plan", r"(?m)^1\. |test")],
         critical=_SAFETY),
    case("What should we test for comments?",
         must=[("comments_as_inference",
                r"(inferred|inference|proxy|don'?t track|no hard comment|likelihood)")],
         must_not=[("no_invented_comment_counts", r"\b\d[\d,\.]*\s*comments\b")],
         critical=_SAFETY,
         notes="comment performance is not measured -> must be labelled inference"),
    case("What gets the most views relative to audience size?",
         must=[("views_or_limits", r"(view|ratio|follower|don'?t have|not populated)")],
         critical=_SAFETY),
    case("What makes Great posts different from Good posts?",
         must=[("compares_buckets", r"(great|good|difference|hook|format|signal|don'?t have)")],
         critical=_SAFETY),
    case("What should go on next week's calendar?",
         must=[("calendar_or_shoot_guidance", r"(calendar|shoot|week|priorit|revise)")],
         critical=_SAFETY),
    # --- epistemics --------------------------------------------------------
    case("Are you sure?",
         context="ideas",
         must=[("engages_with_doubt", r"(confidence|sample|proven|bet|caveat|sure|not)")],
         critical=_SAFETY),
    case("What would change your mind?",
         context="ideas",
         must=[("names_disconfirming_evidence", r"(if|change|test|more|evidence|sample|would)")],
         critical=_SAFETY),
    case("What are we missing because Meta isn't connected?",
         must=[("private_only_gap", r"(private|insight|saves|reach|demographic)"),
               ("not_a_blocker", r"(public|still|core)")],
         critical=_SAFETY,
         notes="must frame Meta as optional enrichment, not a blocker"),
    case("Is the brain up to date?",
         must=[("states_freshness", r"(refresh|up to date|stale|healthy|hasn'?t run|last)")],
         critical=_SAFETY),
]

# ---- multi-turn conversations (Phase 21) ---------------------------------
CONVERSATIONS = [
    {
        "name": "recommendation_then_epistemics",
        "turns": [
            {"say": "what are the strongest ideas to shoot?",
             "must": [("lists_ideas", r"(?m)^\*?1\.")]},
            {"say": "why?",
             "must": [("explains", r"(because|pattern|territory|evidence)")],
             "must_not": [("no_relist", r"(?m)^\*?1\. .*score")]},
            {"say": "are you sure?",
             "must": [("engages", r"(confidence|sample|bet|caveat|not)")]},
            {"say": "what would change your mind?",
             "must": [("disconfirming", r"(if|test|more|evidence|would)")]},
            {"say": "show me the evidence",
             "must": [("evidence", r"(evidence|proof|internal|sample|\[s\d)")]},
        ],
    },
    {
        "name": "change_then_action",
        "turns": [
            {"say": "what changed this week?",
             "must": [("change_aware", r"(refresh|new|nothing new|hasn'?t run|change)")]},
            {"say": "which one matters?",
             "must": [("picks_or_abstains", r"(matter|proof|pattern|don'?t|external)")]},
            {"say": "why?",
             "must": [("explains", r"(because|evidence|proof|pattern|territory)")]},
            {"say": "what should we shoot because of that?",
             "must": [("actionable", r"(shoot|execution|test|structure|pattern)")]},
        ],
    },
]


def check(text: str, assertions) -> list:
    """Return the list of failed assertion names."""
    low = str(text or "")
    failed = []
    for name, pattern in assertions:
        if not re.search(pattern, low, re.IGNORECASE):
            failed.append(name)
    return failed


def check_absent(text: str, assertions) -> list:
    low = str(text or "")
    failed = []
    for name, pattern in assertions:
        if re.search(pattern, low, re.IGNORECASE):
            failed.append(name)
    return failed
