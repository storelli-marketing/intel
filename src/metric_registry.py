"""Canonical metric registry (Phase 9) — one place that knows what a metric IS.

Every metric declares its source, whether it's public or private, whether it is
currently available, mutability, how it's derived, its denominator, and its
comparability limitations. The rest of the brain asks this registry instead of
assuming a column exists — which is what stops public mode from ever pretending
to have saves/reach/impressions/demographics.
"""
from __future__ import annotations

from typing import Optional

import config

# source ids (kept aligned with evidence_contract.SourceClass values)
APIFY_PUBLIC = "apify_public"
IG_PRIVATE = "instagram_insights_private"
DERIVED = "derived"
HUMAN = "human"

_MUTABLE = "mutable"
_IMMUTABLE = "immutable"


class Metric:
    def __init__(self, name, source, public, mutability, denominator="",
                 derivation="", limitations="", column=None):
        self.name = name
        self.source = source
        self.public = public
        self.mutability = mutability
        self.denominator = denominator
        self.derivation = derivation
        self.limitations = limitations
        self.column = column or name

    def as_dict(self, available: bool, refreshed_at: str = "") -> dict:
        return {"canonical_name": self.name, "source": self.source,
                "visibility": "public" if self.public else "private",
                "available": available, "mutability": self.mutability,
                "derivation": self.derivation, "denominator": self.denominator,
                "last_refreshed": refreshed_at, "limitations": self.limitations}


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
    Metric("POST_DATE", APIFY_PUBLIC, True, _IMMUTABLE),
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
    # private-only (unavailable in public mode)
    Metric("SAVES", IG_PRIVATE, False, _MUTABLE),
    Metric("REACH", IG_PRIVATE, False, _MUTABLE),
    Metric("IMPRESSIONS", IG_PRIVATE, False, _MUTABLE),
    Metric("PROFILE_VISITS", IG_PRIVATE, False, _MUTABLE),
    Metric("WEBSITE_CLICKS", IG_PRIVATE, False, _MUTABLE),
    Metric("AGE_SPLIT", IG_PRIVATE, False, _MUTABLE),
    Metric("GENDER_SPLIT", IG_PRIVATE, False, _MUTABLE),
    Metric("LOCATION_SPLIT", IG_PRIVATE, False, _MUTABLE),
    Metric("FOLLOWER_NONFOLLOWER_SPLIT", IG_PRIVATE, False, _MUTABLE),
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
