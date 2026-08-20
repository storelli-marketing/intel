"""Performance buckets — from the manual `PERFORMANCE` column or auto-computed
from the views/followers ratio.

Read-side mapping (legacy + new vocabularies coexist):
  Great                -> "Great" (high performer / positive class for lift)
  Good                 -> "OK"    (average, new auto-compute label)
  Ok                   -> "OK"    (average, legacy manual label)
  Underdog             -> "Bad"   (low)
  Non classified / blank / anything else -> None (skip)

Auto-compute (views/followers ratio):
  r >  1.0      -> "Great"
  0.5 <= r <= 1.0  -> "Good"
  r <  0.5      -> "Underdog"
Boundaries widen slightly vs the spec's `< 0.499` / `<= 0.990` so the ratio
domain is fully covered; an exact 1.0 maps to Good (Great is strictly >).
"""
from __future__ import annotations

import re

import config

from logger import get_logger

log = get_logger()

_MAP = {
    "great": "Great",
    "good": "OK",
    "ok": "OK",
    "underdog": "Bad",
}

POSITIVE_BUCKET = "Great"

# Canonical display values written back to the sheet's PERFORMANCE column.
PERF_LABELS = ("Great", "Good", "Underdog")

# Optional Source Type column — when present, rows explicitly marked as
# external/inspiration are excluded from the *learning layer* (correlations,
# lifts). They can still be shown as inspiration by the idea interpreter.
# Sheets without this column behave exactly as before.
_SOURCE_TYPE_ALIASES = ("source type", "source_type", "source")
# EXTERNAL_INSPIRATION is the SOURCE_TYPE stamped on every row the Inspiration
# Layer writes to the INSPIRATION_CONTENT tab. It is listed here so that even if
# such a row were ever fed into the internal learning functions, it is treated
# as reference-only and can never become Storelli performance evidence.
_EXTERNAL_SOURCE_VALUES = {"external", "inspiration", "reference",
                           "competitor", "creator", "external_inspiration"}
_INTERNAL_SOURCE_VALUES = {"internal", "storelli", "owned"}


def source_type(row: dict) -> str:
    """Return the row's Source Type value (lowercased, stripped) or ''.
    Case-insensitive across a few common column-name spellings."""
    for k, v in row.items():
        if k and k.lower() in _SOURCE_TYPE_ALIASES:
            return str(v or "").strip().lower()
    return ""


def is_reference_row(row: dict) -> bool:
    """True if this row is explicitly external/inspiration and must NOT
    contaminate the Storelli learning layer."""
    return source_type(row) in _EXTERNAL_SOURCE_VALUES


def is_internal_row(row: dict) -> bool:
    """True if this row is explicitly internal/Storelli-owned. When no Source
    Type column exists, the answer is 'unknown' (returns False)."""
    return source_type(row) in _INTERNAL_SOURCE_VALUES


def bucket_from_performance(value) -> str | None:
    return _MAP.get(str(value or "").strip().lower())


# --- maturity + provenance -------------------------------------------------
_DATE_ALIASES = ("post_date", "posted", "date", "published", "publish date", "posted at")
_SOURCE_ALIASES = ("performance_source", "performance source")
_MEASURED_ALIASES = ("performance_measured_at", "performance measured at")


def _norm_key(k) -> str:
    """Fold case and separator style so POST_DATE / 'Post Date' / 'post-date'
    all resolve to the same alias."""
    return re.sub(r"[^a-z0-9]+", "_", str(k).strip().lower()).strip("_")


def _ci(row: dict, aliases) -> str:
    lower = {_norm_key(k): k for k in row}
    for a in aliases:
        actual = lower.get(_norm_key(a))
        if actual is not None and str(row[actual]).strip():
            return str(row[actual]).strip()
    return ""


def post_age_days(row: dict, now=None) -> float | None:
    """Age of the post in days from its POST_DATE, or None when unknown."""
    raw = _ci(row, _DATE_ALIASES)
    if not raw:
        return None
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            ts = datetime.strptime(raw[:len(fmt) + 2].strip()[:19], fmt)
            return (now - ts.replace(tzinfo=timezone.utc)).total_seconds() / 86400.0
        except ValueError:
            continue
    return None


def performance_source(row: dict) -> str:
    return _ci(row, _SOURCE_ALIASES).upper()


def is_auto_labelled(row: dict) -> bool:
    return performance_source(row) == config.PERF_SOURCE_AUTO


def is_mature(row: dict, now=None) -> bool:
    """True when the post is old enough to classify. Unknown age counts as
    mature: we never withhold a label from pre-existing, human-curated data
    just because it has no POST_DATE."""
    age = post_age_days(row, now)
    return True if age is None else age >= config.PERFORMANCE_MATURITY_DAYS


def is_immature_auto(row: dict, now=None) -> bool:
    """An AUTO-classified post that is still too young to trust. These are held
    out of correlations. A human label is never withheld."""
    return is_auto_labelled(row) and not is_mature(row, now)


def buckets_for_rows(rows: list[dict], now=None) -> dict[int, str]:
    """{row_index: bucket} for every row with a recognized PERFORMANCE value.

    Excluded: rows explicitly marked external/inspiration via Source Type (they
    are inspiration, not evidence), and AUTO-classified posts younger than the
    maturity threshold (still accumulating distribution — their label would
    distort the correlations). Human labels always count.
    """
    out = {}
    for r in rows:
        if is_reference_row(r):
            continue
        if is_immature_auto(r, now):
            continue
        b = bucket_from_performance(r.get("PERFORMANCE"))
        if b:
            out[r["_row"]] = b
    return out


def pending_maturity_rows(rows: list[dict], now=None) -> list[int]:
    """Rows held back from classification/correlations pending maturity."""
    return [r["_row"] for r in rows
            if not is_reference_row(r)
            and str(r.get("LINK", "")).strip()
            and not is_mature(r, now)]


def is_positive(bucket: str) -> bool:
    """High performer = the positive class used for lift."""
    return bucket == POSITIVE_BUCKET


def ratio_to_performance(ratio: float | None) -> str | None:
    """Map a views/followers ratio to one of Great / Good / Underdog.

    Returns None when the ratio is missing or non-numeric.
    """
    if ratio is None:
        return None
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return None
    if r < 0.5:
        return "Underdog"
    if r <= 1.0:
        return "Good"
    return "Great"
