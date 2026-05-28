"""Performance buckets from the sheet's manual PERFORMANCE column.

The POC sheet carries human-judged performance, not metrics. We map it
directly and use it as the source of truth for correlations.

  Great     -> "Great"  (high performer / positive class for lift)
  Ok        -> "OK"      (average)
  Underdog  -> "Bad"     (low)
  Non classified / blank / anything else -> None (skip)
"""
from __future__ import annotations

from logger import get_logger

log = get_logger()

_MAP = {
    "great": "Great",
    "ok": "OK",
    "underdog": "Bad",
}

POSITIVE_BUCKET = "Great"


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
