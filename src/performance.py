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


def bucket_from_performance(value) -> str | None:
    return _MAP.get(str(value or "").strip().lower())


def buckets_for_rows(rows: list[dict]) -> dict[int, str]:
    """{row_index: bucket} for every row with a recognized PERFORMANCE value."""
    out = {}
    for r in rows:
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
