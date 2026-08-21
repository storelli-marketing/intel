"""Structured analytics query contract — the deterministic front door for
EXPLICIT factual questions about our own numbers.

Why this exists
---------------
The brain already had a keyword-driven analytics router (`social_analytics.
is_social_analytics_query`) and a decision-frame layer that carries "the decision
we are currently making" across turns. In production they collided:

    T1  "trial vs standard reels, in terms of demographic views?"
        -> correctly: we can't split that.
    T2  "ok sounds good... how many seconds long are our highest performing reels?"
        -> WRONG: answered with a creative recommendation ("Dive Without The Sting").

The phrase "highest performing" is an *optimisation objective* cue in
`decision_frame.detect_objective`, so the frame layer — which runs before the
analytics router — claimed the turn and re-ranked ideas. The user had asked for a
number and got a shoot recommendation.

This module is the missing distinction: it decides, deterministically and with no
retrieval, whether a turn is an EXPLICIT analytics question (a metric or
dimension the user named, in a factual interrogative shape) as opposed to a
prescriptive/creative ask that the decision frame should own. `parse()` returns a
structured contract so the strategist model *interprets results* rather than
inventing the query.

Routing principle it implements (highest precedence first):
  1. explicit hard-data / analytics question       <- this module
  2. explicit topic/scope change
  3. contextual decision-frame continuation
  4. ambiguous follow-up resolution
  5. generic strategy / recommendation routes

The frame is still available here, but only as an OPTIONAL scope filter and only
when the turn explicitly refers back to it ("within the BodyShield stuff we just
discussed, how long are the best reels?"). A bare "how long are our
highest-performing reels?" is global by construction.

Pure: no sheet access, no network, no LLM. Metric names are the canonical
`metric_registry` names so availability can be resolved downstream.
"""
from __future__ import annotations

import re
from typing import Optional

# --- question types --------------------------------------------------------
DESCRIPTIVE = "descriptive"      # "what is our median reel length?"
COMPARISON = "comparison"        # "are Great reels shorter than weak ones?"
DISTRIBUTION = "distribution"    # "how many seconds long are our best reels?"
RANKING = "ranking"              # "which reels have the most comments?"
AVAILABILITY = "availability"    # "do we have enough data to know the best time?"

# --- canonical metric names (aligned with metric_registry) -----------------
M_DURATION = "DURATION_SECONDS"
M_VIEWS = "VIEWS"
M_LIKES = "LIKES"
M_COMMENTS = "COMMENTS"
M_SHARES = "SHARES"
M_ENGAGEMENT = "ENGAGEMENT_RATE"
M_PERFORMANCE = "PERFORMANCE"
M_REEL_TYPE = "REEL_TYPE"
M_POST_DAY = "POST_DAY_OF_WEEK"
M_POST_HOUR = "POST_HOUR"
M_POST_AGE = "POST_AGE_DAYS"
M_ROW_COUNT = "ROW_COUNT"
M_AGE_SPLIT = "AGE_SPLIT"
M_GENDER_SPLIT = "GENDER_SPLIT"

# Metrics only private Instagram Insights can provide. Kept here so the contract
# can flag `requires_private_data` before any retrieval runs.
_PRIVATE_METRICS = {M_AGE_SPLIT, M_GENDER_SPLIT, "SAVES", "REACH", "IMPRESSIONS",
                    "PROFILE_VISITS", "WEBSITE_CLICKS", "LOCATION_SPLIT"}

# --- aggregations ----------------------------------------------------------
A_COUNT = "count"
A_MEDIAN = "median"
A_MEAN = "mean"
A_RANGE = "range"
A_DISTRIBUTION = "distribution"
A_PERCENTAGE = "percentage"
A_TOP_N = "top_n"
A_LATEST = "latest"

# ---------------------------------------------------------------------------
# metric vocabulary. Order matters: the first metric whose cue appears wins as
# the PRIMARY metric, so "how many seconds long are our highest performing
# reels?" resolves to DURATION_SECONDS (asked about) rather than PERFORMANCE
# (the cohort selector).
# ---------------------------------------------------------------------------
_METRIC_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # duration first: "how many seconds", "how long", "reel length".
    # NOTE the absence of a bare singular "second": "tell me more about the
    # second one" is an ordinal reference to a prior list item, not a question
    # about seconds, and matching it here hijacked a real follow-up turn.
    (M_DURATION, ("seconds", "how long", "duration", "reel length",
                  "video length", "length of our", "runtime", "sec long",
                  "shorter", "longer", "short or long")),
    # posting time / day
    (M_POST_HOUR, ("what time", "best time to post", "time to post", "time of day",
                   "posting time", "post time", "which hour", "what hour")),
    (M_POST_DAY, ("what day", "which day", "day of the week", "day of week",
                  "weekday", "best day to post", "day to post", "day are our",
                  "day do our")),
    (M_REEL_TYPE, ("trial reel", "trial reels", "trial vs", "trial versus",
                   "trial and standard", "standard reel", "standard reels",
                   "trial/standard", "trial or standard")),
    (M_AGE_SPLIT, ("age split", "age range", "age breakdown", "how old are the people",
                   "demographic", "demographics", "age and gender")),
    (M_GENDER_SPLIT, ("gender split", "gender breakdown", "male or female",
                      "men or women")),
    (M_COMMENTS, ("comment", "comments", "replies")),
    (M_SHARES, ("share", "shares", "sends")),
    (M_ENGAGEMENT, ("engagement rate", "engagement %", "engagement percent")),
    (M_VIEWS, ("views", "view count", "plays", "play count")),
    (M_LIKES, ("likes", "like count")),
    (M_POST_AGE, ("how old is", "how recent", "latest reel", "most recent reel",
                  "newest reel", "how long ago")),
    # A bare factual "what performs better: POV or tutorial?" asks about the
    # PERFORMANCE label itself, sliced by whatever dimension the user named.
    (M_PERFORMANCE, ("performs better", "perform better", "performs best",
                     "perform best", "great rate", "hit rate", "success rate",
                     "normalized performance", "normalised performance")),
)

# Metric cues that are only ever meaningful about our own posts. Without this a
# generic "how long" in a creative brief ("how long should the hook be") could
# look like a duration analytics ask.
_SUBJECT_CUES = ("reel", "reels", "video", "videos", "post", "posts", "content",
                 "clip", "clips", "library", "we have", "we've", "we got", "our")

# ...or an unmistakably quantitative shape, which carries its own subject:
# "what is the median views count?" names no reel but can only be about ours.
_QUANT_RE = re.compile(
    r"\bmedian\b|\baverage\b|\bmean\b|\bdistribution\b|\bbreakdown\b|\brange\b"
    r"|\bpercentage\b|\bpercent\b|\bcount\b|\bmost\b|\bmore\b|\bleast\b|\bfewest\b"
    r"|\bhighest\b|\blowest\b|\btop\s*\d*\b|\bvs\.?\b|\bversus\b|\bcompare\b"
    r"|\bdifference between\b|\bshorter\b|\blonger\b|\bperforms?\s+(?:better|best)\b",
    re.IGNORECASE)

# ---------------------------------------------------------------------------
# PRESCRIPTIVE guard — the single most important negative rule in this module.
#
# "How long SHOULD the BodyShield concept we just discussed BE?" is not an
# analytics question; it is a creative recommendation that duration analytics
# should INFORM. The decision frame must keep it (it resolves "this one"), so
# `parse()` returns None and precedence falls through untouched.
# ---------------------------------------------------------------------------
_PRESCRIPTIVE_RE = re.compile(
    r"\bshould\s+(?:it|this|that|we|i|the|our|they|he|she|[a-z]+\s+)?\w*\s*be\b"
    r"|\bhow\s+long\s+should\b|\bhow\s+many\s+\w+\s+should\b"
    r"|\bshould\s+(?:we|i)\b|\bwhat\s+should\b|\bwhich\s+should\b"
    r"|\brecommend\b|\bideas?\s+for\b|\bmake\s+it\b|\bturn\s+(?:this|that|it)\s+into\b"
    r"|\bwhat\s+do\s+you\s+(?:suggest|recommend)\b",
    re.IGNORECASE)

# Predictive / inferential phrasing. "What content is MOST LIKELY to get
# comments?" asks for a judgement about future content, which the strategy skill
# pack answers as an explicit inference. "What gets the most comments?" asks for
# a fact about posts we already have. Only the second is analytics.
_PREDICTIVE_RE = re.compile(
    r"\bmost likely\b|\blikely to\b|\bwould\s+(?:get|drive|perform)\b"
    r"|\bwill\s+(?:get|drive|perform)\b|\bhow do we get more\b|\bhow can we get\b"
    r"|\binvite (?:replies|comments)\b|\bdrive (?:more )?comments\b",
    re.IGNORECASE)

# An ordinal / positional reference to something in the PREVIOUS answer is an
# ambiguous follow-up by definition — the referent resolver owns it, and it can
# only be understood in context. "Tell me more about the second one" is not a
# question about seconds, and "expand #2" is not a metric lookup.
_ORDINAL_REF_RE = re.compile(
    r"\b(?:the\s+)?(?:first|second|third|fourth|fifth|last|next)\s+"
    r"(?:one|idea|option|item|concept|reel|video|post)\b"
    r"|#\s*\d\b|\bnumber\s+\d\b"
    r"|\b(?:tell me more|more|expand)\s+(?:about\s+)?(?:that|this|it|those|them)\b"
    r"|\b(?:that|this)\s+one\b",
    re.IGNORECASE)

# A bare transform command is a follow-up instruction about the PREVIOUS answer,
# not a question about our data. "shorter" carries a duration word and a
# comparative, so without this it parses as a reel-length question.
_TRANSFORM_ONLY_RE = re.compile(
    r"^(?:shorter|longer|shorten(?:\s+it)?|make\s+it\s+shorter|concise|tl;?dr|"
    r"why|sources|expand|more|deeper|again|risky(?:\s+version)?|"
    r"the\s+risky\s+version)$",
    re.IGNORECASE)

# Factual interrogative shapes. An explicit analytics question needs one of
# these AND a named metric — that pairing is what makes it unambiguous.
_FACTUAL_RE = re.compile(
    r"\bhow many\b|\bhow much\b|\bhow long\b|\bhow old\b"
    r"|\bwhat(?:'s| is| are)?\s+(?:our|the|their)\b|\bwhat\s+day\b|\bwhat\s+time\b"
    r"|\bwhich\b|\bmedian\b|\baverage\b|\bmean\b|\bdistribution\b|\bbreakdown\b"
    r"|\bpercentage\b|\bpercent\b|\bwhat\s+%|\bhow\s+often\b"
    r"|\bcompare\b|\bcomparison\b|\bdifference between\b|\bvs\.?\b|\bversus\b"
    r"|\bshorter\b|\blonger\b|\bmore\b|\bmost\b|\bleast\b|\bfewest\b|\bhighest\b"
    r"|\blowest\b|\btop\s*\d*\b|\brange\b|\bspread\b|\bskew\b"
    r"|\bdo we have\b|\bdo we track\b|\bcan we\b|\benough data\b|\bshow (?:me|the)\b"
    r"|\bhow are\b|\bwhat performs better\b|\bperforms better\b",
    re.IGNORECASE)

# --- cohort vocabulary ----------------------------------------------------
# "highest performing" and friends select a COHORT; they never redefine the
# metric being asked about. §4: one definition, stated, never silently swapped.
_GREAT_COHORT_CUES = ("highest performing", "highest-performing", "best performing",
                      "best-performing", "top performing", "top-performing",
                      "strongest", "best reels", "best videos", "best posts",
                      "our best", "winners", "winning reels", "great reels",
                      "highest performers", "top performers", "best content")
_WEAK_COHORT_CUES = ("worst performing", "worst-performing", "weakest", "weak reels",
                     "underdog", "underperforming", "worst reels", "lowest performing")

# An explicit raw-metric cohort ("highest views") overrides the Great default —
# the user named the yardstick, so we use theirs.
_METRIC_COHORT_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (M_VIEWS, ("highest views", "most views", "top views", "most viewed",
               "highest view count", "by views")),
    (M_COMMENTS, ("most comments", "highest comments", "most commented",
                  "most-commented", "by comments")),
    (M_LIKES, ("most likes", "highest likes", "most liked", "by likes")),
    (M_SHARES, ("most shares", "most shared", "highest shares", "by shares")),
    (M_ENGAGEMENT, ("highest engagement", "best engagement", "by engagement")),
)

# "normalized performance" is the project's views/followers ratio.
_NORMALIZED_CUES = ("normalized", "normalised", "views/follower", "view/follower",
                    "views per follower", "ratio")

# --- scope ---------------------------------------------------------------
# Explicit references back to the live decision. Only these let the frame act as
# a scope filter; anything else stays global.
_BACKREF_CUES = ("we just discussed", "we just talked about", "we just looked at",
                 "you just shared", "we were discussing", "that we discussed",
                 "we just covered", "from that", "within that", "in that",
                 "for that one", "the one we", "we just found", "just mentioned",
                 "we've been discussing", "we have been discussing")

_PRODUCT_VOCAB = ("bodyshield gk leggings", "bodyshield leggings", "bodyshield",
                  "coolcore", "exoshield head guard", "exoshield", "head guard",
                  "head guards", "gk gloves", "goalkeeper gloves", "gloves",
                  "sliders", "slider", "leggings", "undershirt", "jersey")
_ICP_VOCAB = ("parents", "parent", "aspiring pro", "adult amateur", "youth",
              "keepers", "goalkeepers", "general")

_TAXONOMY_TERMS = ("pov", "tutorial", "demo", "reaction", "story", "comparison",
                   "talking head", "do / don't", "do/don't", "voiceover",
                   "curiosity gap", "fear / risk", "education", "humor", "humour",
                   "authority", "social proof", "aspiration")


# Filters are matched case-folded against hand-typed sheet cells, so the vocab
# above is lowercase. These are how the same terms are SPOKEN back to the user —
# "BodyShield", never "Bodyshield" (str.title() mangles internal capitals).
_DISPLAY = {
    "bodyshield gk leggings": "BodyShield GK Leggings",
    "bodyshield leggings": "BodyShield Leggings", "bodyshield": "BodyShield",
    "coolcore": "CoolCore", "exoshield head guard": "ExoShield Head Guard",
    "exoshield": "ExoShield", "head guard": "head guards",
    "head guards": "head guards", "gk gloves": "GK Gloves",
    "goalkeeper gloves": "goalkeeper gloves", "gloves": "gloves",
    "sliders": "sliders", "slider": "sliders", "leggings": "leggings",
    "undershirt": "undershirts", "jersey": "jerseys",
    "parents": "Parents", "parent": "Parents", "aspiring pro": "Aspiring Pro",
    "adult amateur": "Adult Amateur", "youth": "youth", "keepers": "keepers",
    "goalkeepers": "goalkeepers", "general": "General",
}


def display(term: str) -> str:
    """How to write a vocabulary term back to the user."""
    key = str(term or "").strip().lower()
    return _DISPLAY.get(key, str(term or "").strip())


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", str(text or "").lower()).strip() + " "


def _find_all(haystack: str, vocab) -> list:
    """Longest-match-first vocabulary hits, de-duplicated by containment so
    "BodyShield GK Leggings" doesn't also report "Leggings"."""
    hits: list[str] = []
    for term in sorted(vocab, key=len, reverse=True):
        if term in haystack and not any(term in h for h in hits):
            hits.append(term)
    return hits


def detect_metrics(text: str) -> list:
    """Every canonical metric the text names, primary first."""
    t = _norm(text)
    found: list[str] = []
    for metric, cues in _METRIC_CUES:
        if any(c in t for c in cues) and metric not in found:
            found.append(metric)
    return found


def _cohort(text: str) -> dict:
    """The cohort the question restricts to.

    §4: "highest performing" always means the SAME thing — reels currently
    classified Great by the established performance methodology — unless the user
    names a raw yardstick ("highest views"), in which case theirs wins. We never
    silently drift between views / normalized views / Great rate / engagement.
    """
    t = _norm(text)
    for metric, cues in _METRIC_COHORT_CUES:
        if any(c in t for c in cues):
            return {"basis": "metric", "metric": metric,
                    "label": f"ranked by {metric.lower().replace('_', ' ')}",
                    "stated": True}
    if any(c in t for c in _NORMALIZED_CUES) and any(
            c in t for c in ("top", "best", "highest", "strongest", "performance")):
        return {"basis": "normalized", "metric": M_VIEWS,
                "label": "ranked by views against followers at measurement",
                "stated": True}
    if any(c in t for c in _GREAT_COHORT_CUES):
        return {"basis": "performance_label", "performance": "Great",
                "label": "reels currently classified Great", "stated": True}
    if any(c in t for c in _WEAK_COHORT_CUES):
        return {"basis": "performance_label", "performance": "Underdog",
                "label": "reels currently classified Underdog", "stated": True}
    return {"basis": "all", "label": "all analyzed internal reels", "stated": False}


def _question_type(text: str, metric: str, cohort: dict) -> str:
    t = _norm(text)
    if any(c in t for c in ("enough data", "do we have enough", "can we tell",
                            "do we track", "do we have", "is there enough")):
        return AVAILABILITY
    if any(c in t for c in ("vs", "versus", "compare", "comparison",
                            "difference between", "shorter than", "longer than",
                            "performs better", "better:", "or ")) and \
            any(c in t for c in ("vs", "versus", "compare", "comparison",
                                 "difference between", "shorter than",
                                 "longer than", "performs better")):
        return COMPARISON
    if metric == M_ROW_COUNT:
        return DESCRIPTIVE
    if any(c in t for c in ("which reel", "which reels", "which post", "which video",
                            "top 10", "top ten", "top 5", "top five", "list the",
                            "most comments", "most views", "most likes", "most shares",
                            "which of our")):
        return RANKING
    if any(c in t for c in ("median", "average", "mean", "what is our", "what's our",
                            "how old is")):
        return DESCRIPTIVE
    if any(c in t for c in ("percentage", "percent", "distribution", "breakdown",
                            "range", "how many seconds", "how long are", "spread",
                            "which duration", "what duration")):
        return DISTRIBUTION
    return DISTRIBUTION if metric == M_DURATION else DESCRIPTIVE


def _aggregations(text: str, qtype: str, metric: str) -> list:
    t = _norm(text)
    agg: list[str] = []
    if "median" in t:
        agg.append(A_MEDIAN)
    if "average" in t or "mean" in t:
        agg.append(A_MEAN)
    if "percentage" in t or "percent" in t or "what %" in t:
        agg.append(A_PERCENTAGE)
    if "how many" in t or "count" in t:
        agg.append(A_COUNT)
    if "range" in t or "spread" in t:
        agg.append(A_RANGE)
    if "distribution" in t or "breakdown" in t or "buckets" in t:
        agg.append(A_DISTRIBUTION)
    if qtype == RANKING:
        agg.append(A_TOP_N)
    if metric == M_POST_AGE:
        agg.append(A_LATEST)
    # A distribution/comparison question about a continuous metric deserves the
    # full picture regardless of which single word the user happened to use —
    # "how many seconds long are our best reels?" reads as a count cue but wants
    # count + median + mean + range + buckets (§5).
    if qtype in (DISTRIBUTION, COMPARISON) and metric == M_DURATION:
        agg += [A_COUNT, A_MEDIAN, A_MEAN, A_RANGE, A_DISTRIBUTION]
    elif not agg:
        agg = [A_COUNT, A_MEDIAN, A_MEAN] if qtype == COMPARISON else [A_COUNT]
    # Deduplicate, preserving order.
    return list(dict.fromkeys(agg))


def _top_n(text: str, default: int = 5) -> int:
    m = re.search(r"\btop\s*(\d{1,3})\b", _norm(text))
    if m:
        return max(1, min(50, int(m.group(1))))
    return default


def _explicit_filters(text: str) -> dict:
    t = _norm(text)
    products = _find_all(t, _PRODUCT_VOCAB)
    icps = _find_all(t, _ICP_VOCAB)
    out: dict = {}
    if products:
        out["product"] = products
    if icps:
        out["icp"] = icps
    terms = _find_all(t, _TAXONOMY_TERMS)
    if terms:
        out["taxonomy_terms"] = terms
    return out


def _frame_scope(frame: Optional[dict]) -> dict:
    """The frame's product/ICP scope, or {} when there is nothing to inherit."""
    sc = (frame or {}).get("scope") or {}
    out: dict = {}
    for key in ("product", "icp"):
        vals = [str(v) for v in (sc.get(key) or []) if str(v).strip()]
        if vals:
            out[key] = vals
    return out


def is_counting_question(text: str) -> bool:
    """"how many BodyShield reels do we have?" — a question about our SAMPLE, not
    about a metric's value."""
    t = _norm(text)
    if "how many" not in t:
        return False
    if any(c in t for c in ("second", "seconds", "minute")):
        return False           # "how many seconds long" is a duration question
    return any(c in t for c in ("reel", "reels", "video", "videos", "post", "posts",
                                "do we have", "have we", "we got", "in the library"))


def parse(text: str, frame: Optional[dict] = None,
          context: Optional[list] = None) -> Optional[dict]:
    """Return the analytics contract for an EXPLICIT analytics question, else None.

    None means "not an explicit analytics question" — the caller's existing
    routing (decision frame, strategy skills, strategist) is untouched. This is
    deliberately conservative: a metric must be named AND the shape must be
    factual AND it must not be prescriptive/predictive.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    if _TRANSFORM_ONLY_RE.match(raw.strip(" ?.!,")):
        return None
    t = _norm(raw)

    # Prescriptive / predictive asks belong to the frame and the strategy skills;
    # an ordinal back-reference belongs to the referent resolver.
    if _PRESCRIPTIVE_RE.search(t) or _PREDICTIVE_RE.search(t) or _ORDINAL_REF_RE.search(t):
        return None

    counting = is_counting_question(raw)
    cohort = _cohort(raw)
    metrics = detect_metrics(raw)
    # A sample-size clause is only the PRIMARY subject when nothing else was
    # named. "Which hook has the highest Great rate, and how many posts is that
    # based on?" asks about hooks and asks for the denominator — treating the
    # denominator as the question turns it into a library head-count.
    if counting and not metrics:
        metrics = [M_ROW_COUNT]
    elif counting:
        metrics = metrics + [M_ROW_COUNT]
    if not metrics and cohort.get("basis") in ("metric", "normalized"):
        # "our top 10 reels by normalized performance" names the yardstick only
        # in the cohort — that IS the metric being asked about.
        metrics = [cohort["metric"]]
    if not metrics:
        return None

    metric = metrics[0]
    # "our top 10 reels by normalized performance" reads as a PERFORMANCE cue, but
    # the cohort names the actual yardstick — use it, so the answer ranks by the
    # thing the user asked to rank by (§4: never silently swap the definition).
    if metric == M_PERFORMANCE and cohort.get("basis") in ("metric", "normalized"):
        metric = cohort["metric"]
        metrics = [metric] + [m for m in metrics if m != metric]

    # A metric word alone isn't enough — it must be asked ABOUT our content in a
    # factual shape. "views" inside "what should we shoot to get views" is not an
    # analytics question (and is caught by the prescriptive guard anyway).
    if not _FACTUAL_RE.search(t):
        return None
    if metric != M_ROW_COUNT and not any(c in t for c in _SUBJECT_CUES) \
            and not _QUANT_RE.search(t):
        return None
    # "Which hook / format has the highest Great rate?" names a taxonomy LAYER,
    # not a metric value. The correlation and signal routes already answer those
    # with lift and sample size; this layer only owns a PERFORMANCE comparison
    # between specifically NAMED options ("POV or tutorial?").
    if metric == M_PERFORMANCE and not _find_all(t, _TAXONOMY_TERMS):
        return None

    qtype = _question_type(raw, metric, cohort)
    aggregation = _aggregations(raw, qtype, metric)

    # --- scope: explicit > inherited (only on an explicit back-reference) > global
    filters = _explicit_filters(raw)
    scope_source = "global"
    if filters.get("product") or filters.get("icp"):
        scope_source = "explicit"
    elif any(c in t for c in _BACKREF_CUES):
        inherited = _frame_scope(frame)
        if inherited:
            filters.update(inherited)
            scope_source = "inherited"

    dimensions = [m for m in metrics[1:] if m != metric]
    if cohort.get("basis") == "performance_label":
        dimensions.append(M_PERFORMANCE)
    if metric in (M_POST_DAY, M_POST_HOUR):
        # A posting-time question is always performance-conditioned: "best time"
        # only means anything against how those posts actually did.
        dimensions.append(M_PERFORMANCE)

    return {
        "question_type": qtype,
        "metric": metric,
        "dimensions": list(dict.fromkeys(dimensions)),
        "filters": filters,
        "cohort": cohort,
        "aggregation": aggregation,
        "scope_source": scope_source,
        "requires_private_data": metric in _PRIVATE_METRICS,
        "top_n": _top_n(raw),
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# analytics-INFORMED recommendation (§22)
# ---------------------------------------------------------------------------
# "How long should the BodyShield concept we just discussed be?" is the mirror
# image of an analytics lookup: the decision frame resolves WHAT "this one" is,
# and duration analytics then INFORMS the recommendation. Answering it with a
# global median would ignore the concept; answering it with an idea list would
# ignore the question. It is neither an analytics question nor a bare idea ask.
_LENGTH_RECO_RE = re.compile(
    r"\bhow long should\b|\bhow many seconds should\b|\bwhat length should\b"
    r"|\bshould (?:it|this|that|the \w+|we make it)\s+be\b[^?]{0,30}"
    r"(?:long|length|seconds)\b"
    r"|\b(?:length|duration)\s+should\b|\bhow short should\b",
    re.IGNORECASE)


def parse_recommendation(text: str, frame: Optional[dict] = None,
                         context: Optional[list] = None) -> Optional[dict]:
    """Contract for a prescriptive ask that duration analytics should inform.

    Returns None for everything else, including a factual duration question
    (which `parse()` owns) — the two are mutually exclusive by construction.
    """
    raw = str(text or "").strip()
    if not raw or not _LENGTH_RECO_RE.search(_norm(raw)):
        return None
    t = _norm(raw)
    filters = _explicit_filters(raw)
    scope_source = "global"
    if filters.get("product") or filters.get("icp"):
        scope_source = "explicit"
    else:
        # No product named -> the frame is what "this one" refers to. Unlike a
        # factual question, a recommendation SHOULD inherit the live scope: that
        # is the whole point of asking "how long should THIS be".
        inherited = _frame_scope(frame)
        if inherited:
            filters.update(inherited)
            scope_source = "inherited"
    return {
        "question_type": "recommendation",
        "metric": M_DURATION,
        "dimensions": [M_PERFORMANCE],
        "filters": filters,
        "cohort": {"basis": "performance_label", "performance": "Great",
                   "label": "reels currently classified Great", "stated": True},
        "aggregation": [A_COUNT, A_MEDIAN, A_RANGE, A_DISTRIBUTION],
        "scope_source": scope_source,
        "requires_private_data": False,
        "top_n": 5,
        "raw": raw,
        "referent": (frame or {}).get("prior_recommendation") or "",
    }


def is_explicit_analytics_question(text: str, frame: Optional[dict] = None,
                                   context: Optional[list] = None) -> bool:
    """Precedence gate: does this turn name a metric and ask for a fact about it?

    True here means an active decision frame must NOT convert the turn into a
    creative recommendation (§2/§17). It does not mean the data exists — that is
    resolved downstream by the availability ladder.
    """
    return parse(text, frame=frame, context=context) is not None


def describe(aq: Optional[dict]) -> str:
    """One-line human summary, for route_debug only."""
    if not aq:
        return "no"
    return (f"{aq['question_type']}/{aq['metric']}"
            f" cohort={aq['cohort'].get('label', '')}"
            f" scope={aq['scope_source']}")
