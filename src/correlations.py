"""Signal <-> performance association engine.

For each signal column we compute the high-performer (Great) rate when the
signal is present vs absent, and the lift between them. This is correlation /
association, NOT causation.

Confidence by sample size (videos WITH the signal):
  High   >= 20
  Medium 8-19
  Low    < 8
"""
import taxonomy
from performance import is_positive


def _confidence(sample_size: int) -> str:
    if sample_size >= 20:
        return "High"
    if sample_size >= 8:
        return "Medium"
    return "Low"


def compute(rows: list[dict], buckets: dict[int, str]) -> list[dict]:
    """rows must each have signal columns (0/1) and a _row index.

    Returns a list of per-signal association dicts sorted by lift desc.
    """
    idx = taxonomy.signal_index()
    total = len(rows)
    results = []

    for col, meta in idx.items():
        with_rows = [r for r in rows if str(r.get(col, 0)) == "1"]
        without_rows = [r for r in rows if str(r.get(col, 0)) != "1"]
        n_with = len(with_rows)
        n_without = len(without_rows)

        hi_with = sum(1 for r in with_rows if is_positive(buckets.get(r["_row"], "")))
        hi_without = sum(1 for r in without_rows if is_positive(buckets.get(r["_row"], "")))

        rate_with = (hi_with / n_with) if n_with else 0.0
        rate_without = (hi_without / n_without) if n_without else 0.0
        lift = rate_with - rate_without

        results.append({
            "signal": col,
            "layer": meta["layer"],
            "label": meta["label"],
            "total_videos": total,
            "videos_with_signal": n_with,
            "high_rate_with": rate_with,
            "high_rate_without": rate_without,
            "lift": lift,
            "confidence": _confidence(n_with),
        })

    results.sort(key=lambda r: r["lift"], reverse=True)
    return results


def winning(results: list[dict], min_present: int = 1) -> list[dict]:
    return [r for r in results if r["lift"] > 0 and r["videos_with_signal"] >= min_present]


def weak(results: list[dict], min_present: int = 1) -> list[dict]:
    out = [r for r in results if r["lift"] < 0 and r["videos_with_signal"] >= min_present]
    out.sort(key=lambda r: r["lift"])  # most negative first
    return out


def fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def fmt_lift(x: float) -> str:
    return f"{'+' if x >= 0 else ''}{x * 100:.0f}%"
