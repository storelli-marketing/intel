"""Canonical metric registry (Phase 9) — one place that knows what a metric IS.

Every metric declares its source, whether it's public or private, whether it is
currently available, mutability, how it's derived, its denominator, and its
comparability limitations. The rest of the brain asks this registry instead of
assuming a column exists — which is what stops public mode from ever pretending
to have saves/reach/impressions/demographics.
"""
from __future__ import annotations

import re
from typing import Optional

import config

# source ids (kept aligned with evidence_contract.SourceClass values)
APIFY_PUBLIC = "apify_public"
IG_PRIVATE = "instagram_insights_private"
# Not obtainable from Apify OR private Insights — it would need a system the
# brain is not connected to (order data, a controlled test, a competitor's
# own dashboard). Connecting Meta does NOT unlock these.
UNOBTAINABLE = "unobtainable"
DERIVED = "derived"
HUMAN = "human"

_MUTABLE = "mutable"
_IMMUTABLE = "immutable"


class Metric:
    def __init__(self, name, source, public, mutability, denominator="",
                 derivation="", limitations="", column=None, path_to_answer="",
                 asked_as=()):
        self.name = name
        self.source = source
        self.public = public
        self.mutability = mutability
        self.denominator = denominator
        self.derivation = derivation
        self.limitations = limitations
        self.column = column or name
        # What it would actually take to answer a question about this metric —
        # so an honest "we can't" is never a dead end.
        self.path_to_answer = path_to_answer
        # How a human phrases a question about it, for question-time detection.
        self.asked_as = tuple(asked_as)

    def as_dict(self, available: bool, refreshed_at: str = "") -> dict:
        return {"canonical_name": self.name, "source": self.source,
                "visibility": "public" if self.public else "private",
                "available": available, "mutability": self.mutability,
                "derivation": self.derivation, "denominator": self.denominator,
                "last_refreshed": refreshed_at, "limitations": self.limitations,
                "path_to_answer": self.path_to_answer}


_CONNECT = ("connect private Instagram Insights (a Meta Business login for the "
            "Storelli account) — that's the only source for it")

# The canonical set. `public=False` metrics are ONLY obtainable from private
# Instagram Insights; in public (Apify) mode they are unavailable and must never
# be inferred or estimated.
REGISTRY: dict = {m.name: m for m in [
    Metric("VIEWS", APIFY_PUBLIC, True, _MUTABLE,
           limitations="plays/views definition varies by platform version"),
    Metric("LIKES", APIFY_PUBLIC, True, _MUTABLE),
    Metric("COMMENTS", APIFY_PUBLIC, True, _MUTABLE),
    Metric("SHARES", APIFY_PUBLIC, True, _MUTABLE,
           limitations="only present when the actor returns it"),
    Metric("FOLLOWERS_AT_MEASUREMENT", APIFY_PUBLIC, True, _MUTABLE,
           limitations="account-level snapshot at refresh time, not at post time"),
    Metric("POST_DATE", APIFY_PUBLIC, True, _IMMUTABLE,
           limitations="date only — carries no time of day, so it cannot answer "
                       "an hour-of-day question"),
    # Full-resolution publication timestamp. The Apify/Meta payload carries one;
    # POST_DATE deliberately keeps only the date, so hour-of-day analysis needs
    # this column to exist and be populated.
    Metric("POST_TIMESTAMP", APIFY_PUBLIC, True, _IMMUTABLE,
           limitations="UTC as delivered by the source; local posting time is "
                       "unknown unless STORELLI_POSTING_TIMEZONE is configured",
           path_to_answer=("keep the source timestamp at full resolution in a "
                           "POST_TIMESTAMP column instead of truncating it to a date"),
           asked_as=("posting timestamp", "exact time it was posted")),
    Metric("POST_DAY_OF_WEEK", DERIVED, True, _IMMUTABLE,
           derivation="weekday of POST_TIMESTAMP (or POST_DATE)",
           limitations="only as complete as the underlying date coverage",
           asked_as=("what day", "which day", "day of the week", "best day to post")),
    Metric("POST_HOUR", DERIVED, True, _IMMUTABLE,
           derivation="hour of POST_TIMESTAMP in the configured posting timezone",
           limitations="needs a timestamp WITH a time component; POST_DATE alone "
                       "cannot produce it, and hours are UTC until a posting "
                       "timezone is configured",
           path_to_answer=("populate POST_TIMESTAMP with the source's full "
                           "timestamp, then set STORELLI_POSTING_TIMEZONE"),
           asked_as=("what time", "best time to post", "which hour", "time of day")),
    Metric("POST_AGE_DAYS", DERIVED, True, _MUTABLE,
           derivation="now - POST_DATE",
           limitations="unknown for rows with no post date; a post younger than "
                       "the maturity window is deliberately unlabelled",
           asked_as=("how old is", "how recent", "latest reel")),
    Metric("DURATION_SECONDS", APIFY_PUBLIC, True, _IMMUTABLE,
           limitations="absent unless the actor returns a duration"),
    Metric("ENGAGEMENT_RATE", DERIVED, True, _MUTABLE,
           denominator="reach when private Insights available, else followers",
           derivation="interactions / denominator",
           limitations="denominator differs between public and private mode — "
                       "not comparable across modes"),
    Metric("PERFORMANCE", HUMAN, True, _MUTABLE,
           derivation="human label, or views/followers ratio via performance.py",
           denominator="followers at measurement (or configured fallback)",
           limitations="human labels are judgement, not a measured metric"),
    # private-only (unavailable in public mode) — connecting Instagram Insights
    # would unlock these, so that IS the path to an answer.
    Metric("SAVES", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("saves", "saved", "bookmark")),
    Metric("REACH", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("reach", "reached", "how many people saw")),
    Metric("IMPRESSIONS", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("impressions",)),
    Metric("PROFILE_VISITS", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("profile visit", "profile-visit", "profile view", "visited our profile")),
    Metric("WEBSITE_CLICKS", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("website click", "link click", "clicked through")),
    Metric("AGE_SPLIT", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("age split", "age and gender", "age breakdown", "how old",
                     "demographic", "age range")),
    Metric("GENDER_SPLIT", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("gender split", "gender breakdown", "male", "female",
                     "men or women")),
    Metric("LOCATION_SPLIT", IG_PRIVATE, False, _MUTABLE, path_to_answer=_CONNECT,
           asked_as=("location split", "what country", "which countries", "geograph")),
    Metric("FOLLOWER_NONFOLLOWER_SPLIT", IG_PRIVATE, False, _MUTABLE,
           path_to_answer=_CONNECT,
           asked_as=("non-follower", "nonfollower", "non follower",
                     "followers vs", "new audience or")),
    Metric("AUDIENCE_COMPOSITION", IG_PRIVATE, False, _MUTABLE,
           path_to_answer=_CONNECT,
           asked_as=("actually reaching", "who is watching", "who's watching",
                     "who watches", "audience made up", "just other keepers")),
    # NOT obtainable even with Meta connected.
    Metric("RETENTION_CURVE", UNOBTAINABLE, False, _MUTABLE,
           limitations="no per-second retention data exists in this brain",
           path_to_answer=("read the per-reel retention graph in the Instagram app "
                           "manually, or log it into the sheet — nothing here captures it"),
           asked_as=("drop off", "drop-off", "dropoff", "retention", "watch time",
                     "watch-time", "completion rate", "how far through",
                     "where do people stop", "feels slow in the middle", "pacing drag")),
    Metric("REVENUE_ATTRIBUTION", UNOBTAINABLE, False, _MUTABLE,
           limitations="nothing joins a reel to an order",
           path_to_answer=("join content to orders — a per-reel UTM or discount code, "
                           "plus Shopify order data"),
           asked_as=("revenue", "sales", "roas", "aov", "orders", "drive any sales",
                     "made us money", "conversion")),
    Metric("COMPETITOR_INTERNALS", UNOBTAINABLE, False, _MUTABLE,
           limitations="only a competitor's own dashboard has their real numbers",
           path_to_answer=("nothing legitimate gives us another account's internal "
                           "numbers — we only ever see their public counts"),
           asked_as=("their internal", "competitor's internal", "their real numbers",
                     "their actual numbers", "competitor's dashboard")),
    Metric("CAUSAL_ATTRIBUTION", UNOBTAINABLE, False, _MUTABLE,
           limitations="observational data cannot separate cause from confounder",
           path_to_answer=("run a controlled test — same product and ICP, one variable "
                           "changed, enough posts on each side to compare"),
           asked_as=("cause the lift", "caused the lift", "was it something else",
                      "did switching", "because of the change", "causal")),
]}

# Metrics a question may ask about, mapped to registry names.
_ALIASES = {"views": "VIEWS", "plays": "VIEWS", "likes": "LIKES", "comments": "COMMENTS",
            "comment": "COMMENTS", "shares": "SHARES", "saves": "SAVES", "save": "SAVES",
            "reach": "REACH", "impressions": "IMPRESSIONS", "followers": "FOLLOWERS_AT_MEASUREMENT",
            "engagement": "ENGAGEMENT_RATE", "engagement rate": "ENGAGEMENT_RATE",
            "duration": "DURATION_SECONDS", "length": "DURATION_SECONDS",
            "demographics": "AGE_SPLIT", "age": "AGE_SPLIT", "gender": "GENDER_SPLIT",
            "location": "LOCATION_SPLIT", "performance": "PERFORMANCE"}


def resolve(name: str) -> Optional[Metric]:
    key = str(name or "").strip()
    if key.upper() in REGISTRY:
        return REGISTRY[key.upper()]
    return REGISTRY.get(_ALIASES.get(key.lower(), ""), None)


def private_mode_available() -> bool:
    """True when private Instagram Insights are actually connected."""
    return config.private_insights_configured()


def is_available(name: str, columns: Optional[list] = None,
                 populated: Optional[dict] = None) -> bool:
    """Is this metric actually usable right now?

    A private metric is unavailable unless private Insights are connected. A
    public metric needs its column to exist AND (when coverage is supplied) to
    have at least one populated value — a present-but-empty column is not data.
    """
    m = resolve(name)
    if not m:
        return False
    if not m.public and not private_mode_available():
        return False
    if columns is not None:
        have = {str(c).strip().upper() for c in columns}
        if m.column.upper() not in have:
            return False
    if populated is not None:
        return bool(populated.get(m.column) or populated.get(m.name))
    return True


def unavailable_metrics(columns: Optional[list] = None,
                        populated: Optional[dict] = None) -> list:
    """Canonical names of metrics we genuinely do not have right now."""
    return [n for n in REGISTRY if not is_available(n, columns, populated)]


def describe(name: str, columns: Optional[list] = None, populated: Optional[dict] = None,
             refreshed_at: str = "") -> dict:
    m = resolve(name)
    if not m:
        return {"canonical_name": str(name), "available": False,
                "limitations": "unknown metric"}
    return m.as_dict(is_available(name, columns, populated), refreshed_at)


def metric_gap_note(name: str) -> str:
    """A short, honest sentence for a metric we can't report on."""
    m = resolve(name)
    if not m:
        return f"We don't track {name}."
    if not m.public and not private_mode_available():
        return (f"{m.name.replace('_', ' ').lower()} only comes from private Instagram "
                "Insights, which isn't connected — so we don't have it.")
    return f"{m.name.replace('_', ' ').lower()} isn't populated in the data yet."


# ---------------------------------------------------------------------------
# question-time detection: is the question ABOUT something we cannot measure?
# ---------------------------------------------------------------------------
def detect_asked_metrics(text: str) -> list:
    """Canonical names of metrics the question is asking about, by phrasing.

    Longest phrase first so "profile visit" doesn't lose to a shorter alias, and
    a metric is only claimed once.
    """
    low = " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "
    hits = []
    for name, m in REGISTRY.items():
        for phrase in m.asked_as:
            if phrase in low:
                hits.append((len(phrase), name))
                break
    return [n for _, n in sorted(hits, reverse=True)]


def unanswerable_in_question(text: str) -> list:
    """The metrics a question asks about that we genuinely cannot report."""
    return [n for n in detect_asked_metrics(text) if not is_available(n)]


# Questions ABOUT a metric's availability, or about the schema needed to capture
# it, are not requests for its value — and dedicated handlers answer them far
# better (the exact columns to add, why ICP/Product aren't demographics, the
# Meta-optional framing). The limit gate must stay out of their way.
_META_QUESTION = (
    r"\b(?:schemas?|columns?|fields?|headers?|tabs?|sheets?|plans?|insertion"
    r"|backfill|migrat"
    r"|what would (?:i|we) need to (?:add|track|capture|store)"
    r"|how (?:do|would) (?:i|we) (?:add|track|capture|start tracking)"
    r"|missing because|not connected|isn'?t connected|without meta"
    r"|trial vs|trial versus|standard|audit|readiness|are we able to"
    r"|can (?:you|we) (?:even )?(?:measure|track))\b")


def is_meta_question(text: str) -> bool:
    """True when the question is about the DATA MODEL, not about a metric value."""
    return bool(re.search(_META_QUESTION, str(text or ""), re.IGNORECASE))


def limit_answer(text: str) -> str:
    """An honest answer for a question that turns on a metric we don't have.

    Three obligations: say plainly that the DATA can't answer it, say what it
    would take, and offer what we can still legitimately give. A bare refusal is
    as unhelpful as a guess — the point is to keep the conversation moving
    without dressing judgement up as measurement.
    """
    if is_meta_question(text):
        return ""
    names = unanswerable_in_question(text)
    if not names:
        return ""
    metrics = [REGISTRY[n] for n in names[:2]]
    m = metrics[0]
    labels = [x.name.replace("_", " ").lower() for x in metrics]
    subject = labels[0] if len(labels) == 1 else f"{labels[0]} or {labels[1]}"

    if m.source == UNOBTAINABLE:
        lead = (f"I can't answer that from data — we don't have {subject}, and "
                f"connecting Meta wouldn't change that: {m.limitations}.")
    else:
        lead = (f"I can't answer that from data — we don't have {subject}. "
                f"It only comes from private Instagram Insights, which isn't connected.")
    parts = [lead, f"*To answer it properly:* {m.path_to_answer}."]

    have = [n for n, x in REGISTRY.items()
            if x.public and x.source != DERIVED and is_available(n)]
    if have:
        pretty = ", ".join(n.replace("_", " ").lower() for n in have[:5])
        parts.append(f"*What we do have:* {pretty}.")
    # Never a dead end: an honest craft read is still worth something, as long as
    # it is labelled as judgement and not passed off as a measurement.
    parts.append("*What I can still give you:* a craft judgement based on what we "
                 "do measure — clearly labelled as judgement, not a measured "
                 "result. Ask and I'll give you my read on that basis.")
    return "\n\n".join(parts)
