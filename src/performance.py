"""Performance scoring + relative bucketing across the current dataset.

Score = weighted sum of min-max normalized metrics:
  views .30, reach .20, shares .20, saves .20, comments .10
Missing metrics are treated as 0 contribution but their weight is dropped
from the denominator so a row isn't unfairly penalized.

Buckets (relative to this dataset, by percentile rank of score):
  top 20%      -> Great
  50-80%       -> Good
  20-50%       -> OK
  bottom 20%   -> Bad
"""
from __future__ import annotations

from logger import get_logger

log = get_logger()

WEIGHTS = {
    "views": 0.30,
    "reach": 0.20,
    "shares": 0.20,
    "saves": 0.20,
    "comments": 0.10,
}


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _minmax(values: list[float]) -> dict:
    present = [v for v in values if v is not None]
    if not present:
        return {"min": 0.0, "max": 0.0}
    return {"min": min(present), "max": max(present)}


def compute_scores(rows: list[dict]) -> dict[int, float]:
    """Return {row_index: performance_score in 0..1}."""
    # build per-metric ranges
    ranges = {}
    for metric in WEIGHTS:
        ranges[metric] = _minmax([_to_float(r.get(metric)) for r in rows])

    scores = {}
    for r in rows:
        num = 0.0
        denom = 0.0
        for metric, weight in WEIGHTS.items():
            raw = _to_float(r.get(metric))
            if raw is None:
                continue
            lo, hi = ranges[metric]["min"], ranges[metric]["max"]
            norm = 0.0 if hi == lo else (raw - lo) / (hi - lo)
            num += norm * weight
            denom += weight
        scores[r["_row"]] = (num / denom) if denom else 0.0
    return scores


def assign_buckets(scores: dict[int, float]) -> dict[int, str]:
    """Percentile-rank scores into Bad/OK/Good/Great."""
    if not scores:
        return {}
    items = sorted(scores.items(), key=lambda kv: kv[1])
    n = len(items)
    buckets = {}
    for rank, (row_idx, _score) in enumerate(items):
        # percentile in [0,1): fraction of rows strictly below this one
        pct = rank / n
        if pct >= 0.80:
            buckets[row_idx] = "Great"
        elif pct >= 0.50:
            buckets[row_idx] = "Good"
        elif pct >= 0.20:
            buckets[row_idx] = "OK"
        else:
            buckets[row_idx] = "Bad"
    return buckets


def is_good_or_great(bucket: str) -> bool:
    return bucket in ("Good", "Great")
