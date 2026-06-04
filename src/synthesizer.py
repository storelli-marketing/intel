"""Learning Synthesizer.

Turns the correlation results + tagged rows + performance buckets into a
structured markdown brief at data/latest_learnings.md.

Pure computation — no Gemini calls — so it's free, repeatable, and honest about
thin data. Everything here is correlation / association, never causation.
"""
from __future__ import annotations

import os

import correlations as corr
import taxonomy
from logger import get_logger
from performance import is_positive

log = get_logger()

_OUT = os.path.join(os.path.dirname(__file__), "..", "data", "latest_learnings.md")
LEARNINGS_PATH = _OUT  # public alias for other modules

MIN_GROUP = 2  # min videos before a product/ICP group is worth a finding


# ---- helpers ---------------------------------------------------------------
def _great_count(rows: list[dict], buckets: dict) -> int:
    return sum(1 for r in rows if is_positive(buckets.get(r["_row"], "")))


def _group(rows: list[dict], buckets: dict, key: str) -> dict:
    groups: dict[str, dict] = {}
    for r in rows:
        g = str(r.get(key, "")).strip() or "(unspecified)"
        d = groups.setdefault(g, {"rows": [], "great": 0})
        d["rows"].append(r)
        if is_positive(buckets.get(r["_row"], "")):
            d["great"] += 1
    return groups


def _top_signals_in(rows: list[dict], top: int = 3) -> list[tuple[str, int]]:
    """Most frequent taxonomy labels among the given rows."""
    idx = taxonomy.signal_index()
    counts: dict[str, int] = {}
    for col, meta in idx.items():
        n = sum(1 for r in rows if str(r.get(col, "")).strip() == "1")
        if n:
            counts[f"{meta['label']} ({meta['layer']})"] = n
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]


def _first_label(results: list[dict], layer: str, default: str) -> str:
    for r in results:
        if r["layer"] == layer:
            return r["label"]
    return default


def _best_group(groups: dict) -> str | None:
    """Best group by Great rate, requiring a minimum sample so a lone 1-video
    group can't win on a 100% rate. Falls back to the largest group."""
    eligible = [(g, d) for g, d in groups.items()
                if g != "(unspecified)" and len(d["rows"]) >= MIN_GROUP]
    if not eligible:
        return None
    # rank by Great rate, tie-break by sample size
    ranked = sorted(eligible,
                    key=lambda kv: (kv[1]["great"] / len(kv[1]["rows"]), len(kv[1]["rows"])),
                    reverse=True)
    return ranked[0][0]


_HOOK_OPENER = {
    "Curiosity Gap": "open with a keeper-facing question",
    "Fear / Risk": "open on the injury/risk moment",
    "Aspiration": "open on the aspirational end-state",
    "Education": "open with a quick teaching promise",
    "Humor": "open with a relatable goalkeeper moment",
    "Social Proof": "open with a pro/coach using the gear",
    "Authority": "open with an expert/credibility cue",
}


def _test_confidence(great_count: int, win: list[dict]) -> str:
    """Reliability label for creative-test recommendations, driven by how many
    Great videos support the pattern (the positive class) and signal strength."""
    has_high = any(r["confidence"] == "High" for r in win if r["layer"] in ("hook", "format"))
    if great_count >= 10 and has_high:
        return "Strong confidence"
    if great_count >= 5:
        return "Medium confidence"
    return "Directional"


def _execution(hook: str, fmt: str) -> str:
    opener = _HOOK_OPENER.get(hook, f"open with a {hook.lower()} hook")
    return (f"{opener}, show the protection moment in the first 3 seconds, "
            f"end with a soft-follow CTA.")


def _next_tests(win: list[dict], products: dict, icps: dict, great_count: int = 0) -> list[dict]:
    hook = _first_label(win, "hook", "Curiosity Gap")
    fmt = _first_label(win, "format", "Demo")
    funnel = _first_label(win, "funnel_stage", "Awareness")
    sol = _first_label(win, "solution_type", "Prevention")
    prod = _best_group(products) or "GK Gloves"
    icp = _best_group(icps) or "General"
    confidence = _test_confidence(great_count, win)

    combos = [
        (hook, fmt),
        (hook, "POV"),
        ("Fear / Risk", fmt),
        ("Authority", "Tutorial"),
        (hook, "Do / Don't"),
    ]
    problem = _first_label(win, "problem_type", "Chronic Pain")
    seen, tests = set(), []
    for h, f in combos:
        key = (h, f)
        if key in seen:
            continue
        seen.add(key)
        tests.append({
            "hypothesis": f"A '{f}' format led by a '{h}' hook lifts the Great rate for {icp}.",
            "icp": icp, "product": prod, "hook": h, "format": f, "funnel": funnel,
            "problem_type": problem, "solution_type": sol,
            "priority": "High" if len(tests) < 2 else "Medium",
            "confidence": confidence,
            "execution": _execution(h, f),
            "idea": (f"{f} reel opening on a '{h}' hook, showing {prod} solving a "
                     f"{sol.lower()} need; target {icp} at the {funnel.lower()} stage."),
        })
        if len(tests) >= 5:
            break
    return tests


# ---- synthesis -------------------------------------------------------------
def synthesize(rows: list[dict], buckets: dict, results: list[dict]) -> dict:
    win = corr.winning(results)
    weak = corr.weak(results)
    return {
        "n": len(rows),
        "great": _great_count(rows, buckets),
        "winning": win[:8],
        "weak": weak[:8],
        "products": _group(rows, buckets, "Product"),
        "icps": _group(rows, buckets, "ICP"),
        "fmt_scale": [r for r in results if r["layer"] == "format"
                      and r["lift"] > 0 and r["videos_with_signal"] >= 1],
        "fmt_kill": [r for r in results if r["layer"] == "format"
                     and r["lift"] < 0 and r["videos_with_signal"] >= 1],
        "tests": _next_tests(win, _group(rows, buckets, "Product"),
                             _group(rows, buckets, "ICP"), _great_count(rows, buckets)),
    }


def _pattern_lines(items: list[dict]) -> str:
    if not items:
        return "_No signals in this category yet._\n"
    out = []
    for r in items:
        out.append(
            f"- **{r['label']}** ({r['layer']}) — lift **{corr.fmt_lift(r['lift'])}** "
            f"({corr.fmt_pct(r['high_rate_with'])} Great with vs "
            f"{corr.fmt_pct(r['high_rate_without'])} without · n={r['videos_with_signal']} · "
            f"{r['confidence']} confidence)"
        )
    return "\n".join(out) + "\n"


def _group_lines(groups: dict, buckets: dict, direction_verb: str) -> str:
    rows_out = []
    ranked = sorted(groups.items(), key=lambda kv: len(kv[1]["rows"]), reverse=True)
    for name, d in ranked:
        n = len(d["rows"])
        if n < MIN_GROUP:
            continue
        rate = corr.fmt_pct(d["great"] / n if n else 0)
        sup = ", ".join(f"{lbl}×{cnt}" for lbl, cnt in _top_signals_in(d["rows"]))
        rows_out.append(
            f"- **{name}** — {n} videos, {d['great']} Great ({rate}). "
            f"Supporting signals: {sup or 'n/a'}. "
            f"_{direction_verb} {name}'s most-present signals above._"
        )
    return ("\n".join(rows_out) + "\n") if rows_out else \
        f"_No group with ≥{MIN_GROUP} tagged videos yet._\n"


def render_markdown(s: dict, timestamp: str = "") -> str:
    great_pct = corr.fmt_pct(s["great"] / s["n"]) if s["n"] else "0%"
    caveat = ""
    if s["great"] < 5:
        caveat = (f"\n> ⚠️ **Thin data:** only {s['great']} 'Great' video(s) among "
                  f"{s['n']} tagged. Treat every pattern below as *directional only* — "
                  f"lifts are not yet statistically reliable. Re-synthesize as more "
                  f"rows are tagged.\n")

    md = [f"# Storelli — Latest Learnings\n"]
    if timestamp:
        md.append(f"_Generated {timestamp}. Associations are correlation, not causation._\n")
    md.append(f"_Based on **{s['n']} tagged videos**, **{s['great']} Great** ({great_pct}). "
              f"Positive class = Great._\n")
    if caveat:
        md.append(caveat)

    md.append("\n# Winning Patterns\n" + _pattern_lines(s["winning"]))
    md.append("\n# Weak Patterns\n" + _pattern_lines(s["weak"]))
    md.append("\n# Product Learnings\n" + _group_lines(s["products"], None, "Lean into"))
    md.append("\n# ICP Learnings\n" + _group_lines(s["icps"], None, "Lean into"))

    md.append("\n# Next Tests\n")
    if s["tests"]:
        for i, t in enumerate(s["tests"], 1):
            md.append(
                f"{i}. **{t['hypothesis']}**\n"
                f"   - ICP: {t['icp']} · Product: {t['product']} · Hook: {t['hook']} · "
                f"Format: {t['format']} · Funnel: {t['funnel']}\n"
                f"   - Idea: {t['idea']}"
            )
        md.append("")
    else:
        md.append("_Not enough signal to propose tests yet._\n")

    md.append("\n# Formats To Scale\n" + _pattern_lines(s["fmt_scale"]))
    md.append("\n# Formats To Kill\n" + _pattern_lines(s["fmt_kill"]))
    return "\n".join(md) + "\n"


def write_learnings(rows: list[dict], buckets: dict, results: list[dict],
                    timestamp: str = "") -> str:
    s = synthesize(rows, buckets, results)
    md = render_markdown(s, timestamp)
    os.makedirs(os.path.dirname(_OUT), exist_ok=True)
    with open(_OUT, "w", encoding="utf-8") as f:
        f.write(md)
    log.info("Wrote learnings to %s (%d tagged, %d Great)", _OUT, s["n"], s["great"])
    return _OUT
