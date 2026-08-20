"""Lightweight change history for derived intelligence (Phase 13).

Snapshots the strength of each derived pattern (a correlation signal) after a
refresh so the brain can actually answer "did that pattern get stronger?" with a
comparison instead of restating the current state.

Deliberately small: one row per pattern per snapshot, only the fields needed for
a delta. Internal evidence only — external inspiration is never a pattern here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from logger import get_logger

log = get_logger()

HISTORY_TAB = "PATTERN_HISTORY"
HISTORY_COLUMNS = ("SNAPSHOT_AT", "PATTERN_ID", "LAYER", "LABEL", "STRENGTH",
                   "CONFIDENCE", "SAMPLE_SIZE", "PREV_STRENGTH", "PREV_CONFIDENCE",
                   "PREV_SAMPLE_SIZE", "DELTA", "DIRECTION", "REASON")

STRONGER, WEAKER, STABLE, NEW = "stronger", "weaker", "stable", "new"
_EPS = 0.02          # ignore trivial wobble


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def pattern_id(layer: str, label: str) -> str:
    return f"{str(layer or '').strip().lower()}::{str(label or '').strip().lower()}"


def _num(v, default=0.0) -> float:
    try:
        return float(str(v).strip() or default)
    except (TypeError, ValueError):
        return default


def snapshot_from_correlations(results: list) -> list:
    """correlations.compute() output -> snapshot rows (strength = lift)."""
    out = []
    for r in results or []:
        out.append({
            "pattern_id": pattern_id(r.get("layer", ""), r.get("label", "")),
            "layer": r.get("layer", ""), "label": r.get("label", ""),
            "strength": round(_num(r.get("lift")), 4),
            "confidence": str(r.get("confidence", "")),
            "sample_size": int(_num(r.get("videos_with_signal"))),
        })
    return out


# ---------------------------------------------------------------------------
# read / write
# ---------------------------------------------------------------------------
def _ws(sheets):
    import gspread
    # InspirationSheets exposes the spreadsheet as `_sh`; the POC client exposes a
    # worksheet as `ws`. Assuming only the latter meant every caller passing an
    # InspirationSheets hit an AttributeError and the history was never recorded.
    sh = (getattr(sheets, "_sh", None)
          or getattr(getattr(sheets, "ws", None), "spreadsheet", None))
    if sh is None:
        raise AttributeError("cannot reach the spreadsheet handle for the history tab")
    try:
        return sh.worksheet(HISTORY_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=HISTORY_TAB, rows=2000, cols=len(HISTORY_COLUMNS))
        ws.update(range_name="A1", values=[list(HISTORY_COLUMNS)], value_input_option="RAW")
        return ws


def read_latest(sheets) -> dict:
    """{pattern_id: most recent snapshot row}. Fail-soft -> {}."""
    try:
        vals = _ws(sheets).get_all_values()
    except Exception as e:  # noqa: BLE001
        log.info("pattern history unavailable: %s", e)
        return {}
    if not vals or len(vals) < 2:
        return {}
    hdr = [h.strip() for h in vals[0]]
    latest = {}
    for row in vals[1:]:
        if not any(str(c).strip() for c in row):
            continue
        rec = dict(zip(hdr, row))
        pid = rec.get("PATTERN_ID", "").strip()
        if pid:
            latest[pid] = rec          # later rows win (append-only, chronological)
    return latest


def diff(current: list, previous: dict) -> list:
    """Compare current snapshot rows against the previous ones -> change rows."""
    out = []
    for c in current:
        pid = c["pattern_id"]
        prev = previous.get(pid)
        prev_strength = _num((prev or {}).get("STRENGTH"), None) if prev else None
        prev_n = int(_num((prev or {}).get("SAMPLE_SIZE"))) if prev else 0
        if prev is None:
            direction, delta, reason = NEW, None, "first time this pattern qualified"
        else:
            delta = round(c["strength"] - prev_strength, 4)
            if abs(delta) < _EPS and c["sample_size"] == prev_n:
                direction, reason = STABLE, "no material change"
            elif delta > _EPS or c["sample_size"] > prev_n:
                direction = STRONGER
                gained = c["sample_size"] - prev_n
                reason = (f"{gained} more supporting video(s)" if gained > 0
                          else "performance gap widened")
            else:
                direction = WEAKER
                reason = ("newer videos underperformed" if delta < -_EPS
                          else "supporting sample shrank")
        out.append({**c, "prev_strength": prev_strength, "prev_sample_size": prev_n,
                    "prev_confidence": (prev or {}).get("CONFIDENCE", ""),
                    "delta": delta, "direction": direction, "reason": reason})
    return out


def record(sheets, results: list) -> dict:
    """Snapshot the current pattern strengths and append the computed deltas.
    Returns a summary the refresh report/Slack can use. Never raises."""
    try:
        current = snapshot_from_correlations(results)
        if not current:
            return {"recorded": 0, "changes": []}
        previous = read_latest(sheets)
        changes = diff(current, previous)
        now = _now()
        rows = [[now, c["pattern_id"], c["layer"], c["label"], c["strength"], c["confidence"],
                 c["sample_size"], "" if c["prev_strength"] is None else c["prev_strength"],
                 c["prev_confidence"], c["prev_sample_size"],
                 "" if c["delta"] is None else c["delta"], c["direction"], c["reason"]]
                for c in changes]
        _ws(sheets).append_rows(rows, value_input_option="RAW")
        strengthened = [c for c in changes if c["direction"] == STRONGER]
        return {"recorded": len(rows), "changes": changes,
                "strengthened": strengthened,
                "weakened": [c for c in changes if c["direction"] == WEAKER],
                "new": [c for c in changes if c["direction"] == NEW]}
    except Exception as e:  # noqa: BLE001
        log.warning("pattern history record failed: %s", e)
        return {"recorded": 0, "changes": [], "error": str(e)}


def strongest_change(sheets, layer: str = "", label: str = "") -> Optional[dict]:
    """The most notable recent change (optionally for one pattern) — powers
    'did that pattern get stronger?' with real before/after numbers."""
    latest = read_latest(sheets)
    if not latest:
        return None
    rows = list(latest.values())
    if label:
        pid = pattern_id(layer, label)
        rec = latest.get(pid)
        if not rec:
            return None
        rows = [rec]
    def _score(r):
        try:
            return abs(float(r.get("DELTA") or 0))
        except (TypeError, ValueError):
            return 0.0
    rows.sort(key=_score, reverse=True)
    top = rows[0]
    return {"label": top.get("LABEL", ""), "layer": top.get("LAYER", ""),
            "direction": top.get("DIRECTION", ""), "delta": top.get("DELTA", ""),
            "sample_size": top.get("SAMPLE_SIZE", ""),
            "prev_sample_size": top.get("PREV_SAMPLE_SIZE", ""),
            "reason": top.get("REASON", ""), "snapshot_at": top.get("SNAPSHOT_AT", "")}
