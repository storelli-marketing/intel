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
_EXTERNAL_SOURCE_VALUES = {"external", "inspiration", "reference",
                           "competitor", "creator"}
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


def buckets_for_rows(rows: list[dict]) -> dict[int, str]:
    """{row_index: bucket} for every row with a recognized PERFORMANCE value.

    Rows explicitly marked as external/inspiration via Source Type are excluded
    — they are inspiration, not evidence, and must not enter correlations.
    """
    out = {}
    for r in rows:
        if is_reference_row(r):
            continue
        b = bucket_from_performance(r.get("PERFORMANCE"))
        if b:
            out[r["_row"]] = b
    return out


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
