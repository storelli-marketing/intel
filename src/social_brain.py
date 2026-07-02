"""Marketing Brain answering engine for Slack chat.

`answer_question(user_text)` routes a mention to one of four deterministic
modes — ideas / feedback / learnings / next tests — and returns a Slack-safe
markdown string. Read-only: never writes to the Sheet and never triggers video
analysis.

Every substantive answer cites its sources:
  [S1] Sheet row/link       (retrieved from the analyzed Sheet)
  [S2] latest_learnings.md  (synthesized learnings file)
  [S3] <guideline>.md       (operator-uploaded brand/content guidelines)

Sources are only cited when they were actually retrieved this call. Metrics and
links are never invented. Language is always associational ("associated with" /
"correlated with"), never causal.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import config
import correlations as corr
import interpretation
import taxonomy
from content_context import gather_context
from logger import get_logger

log = get_logger()

_HELP = (
    "Hi — I'm the Storelli Marketing Brain. Ask me:\n"
    "• *ideas* — 3–5 practical Storelli social video ideas grounded in current signals\n"
    "• *feedback <link>* — how a specific reel performed and what to do next\n"
    "• *learnings* — current winning + weak patterns and what to scale/avoid\n"
    "• *tests* — next creative tests to run\n"
    "\n"
    "_I can only read. To analyze new videos, use the dashboard button "
    "*Analyze All Untagged Videos* to tag the full Sheet._\n"
)

# --- routing ---------------------------------------------------------------
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_IDEAS_KW = ("idea", "ideas", "give me ideas", "what should we post",
             "next video", "next videos", "content idea", "post idea")
_FEEDBACK_KW = ("feedback", "analyze this", "why did this perform",
                "how did this do", "review this")
_LEARNINGS_KW = ("learning", "learnings", "what is working", "what's working",
                 "winning pattern", "winning patterns", "what works")
_TESTS_KW = ("test", "tests", "what should we test", "next test", "creative test")


def _has_any(text: str, keywords) -> bool:
    return any(k in text for k in keywords)


def _route(text: str) -> str:
    t = text.lower()
    if _URL_RE.search(text) or _has_any(t, _FEEDBACK_KW):
        return "feedback"
    if _has_any(t, _IDEAS_KW):
        return "ideas"
    if _has_any(t, _LEARNINGS_KW):
        return "learnings"
    if _has_any(t, _TESTS_KW):
        return "tests"
    return "help"


# --- shared retrieval ------------------------------------------------------
def _load_sheet_state():
    """Return (analyzed, buckets, results) or None if Sheets isn't configured
    / reachable. Never raises; the caller shows a clean fallback message.
    """
    try:
        from main import compute_findings
        from sheets_client import SheetsClient
        sheets = SheetsClient()
        sheets.validate_columns()
        return compute_findings(sheets)
    except Exception as e:  # noqa: BLE001
        log.warning("social_brain: sheet unavailable: %s", e)
        return None


def _load_all_rows() -> Optional[list[dict]]:
    """Return every row (including inspiration/external) or None if the sheet
    isn't reachable. Used by the idea interpreter so it can surface external
    rows as *inspiration* without letting them contaminate the learning layer.
    """
    try:
        from sheets_client import SheetsClient
        sheets = SheetsClient()
        sheets.validate_columns()
        return sheets.read_rows()
    except Exception as e:  # noqa: BLE001
        log.warning("social_brain: full-sheet read unavailable: %s", e)
        return None


def _sources_line(used: dict) -> str:
    """Render only the sources that were actually consulted this call."""
    parts = []
    if used.get("sheet_rows"):
        rows = ", ".join(str(r) for r in used["sheet_rows"][:5])
        parts.append(f"[S1] Sheet rows: {rows}")
    if used.get("learnings"):
        parts.append("[S2] data/latest_learnings.md")
    if used.get("guidelines"):
        gs = ", ".join(used["guidelines"][:3])
        parts.append(f"[S3] guidelines: {gs}")
    return ("\n\n_Sources:_ " + " · ".join(parts)) if parts else ""


def _guideline_names(ctx: dict) -> list[str]:
    return list(ctx.get("guidelines", {}).keys())


def _thin_data_note(analyzed: list[dict], buckets: dict) -> str:
    from performance import is_positive
    great = sum(1 for r in analyzed if is_positive(buckets.get(r["_row"], "")))
    if great < 5:
        return (f"\n> ⚠️ Thin data: only {great} 'Great' video(s) across "
                f"{len(analyzed)} tagged. Treat everything below as "
                f"*directional only*.")
    return ""


# --- modes -----------------------------------------------------------------
_NO_DATA_MSG = ("I don't have enough analyzed source data yet. Run "
                "*Run Social Media Learning* / *Generate Learnings* first.")


def _render_ideas(ideas: list[dict], analyzed: list[dict], buckets: dict) -> str:
    """Slack-friendly render of the interpretation output."""
    lines = ["*Ideas grounded in current Storelli signals.*"]
    note = _thin_data_note(analyzed, buckets)
    if note:
        lines.append(note)

    for i, idea in enumerate(ideas, 1):
        src_ids = ", ".join(s["id"] for s in idea.get("sources") or []) or "(none)"
        blocks = "\n".join(f"  - {b}" for b in idea.get("story_blocks") or [])
        lines.append(
            f"\n*{i}. {idea['title']}*\n"
            f"Hook: {idea['hook']}\n"
            f"Structure: {idea['storytelling_structure']}\n"
            f"Product / ICP: {idea['product']} / {idea['icp']}\n"
            f"Story blocks:\n{blocks}\n"
            f"Why: {idea['why_this_should_work']}\n"
            f"Confidence: {idea['confidence']}\n"
            f"Sources: {src_ids}"
        )

    all_sources = interpretation.collect_sources(ideas)
    if all_sources:
        lines.append("\n*Sources:*")
        for s in all_sources:
            suffix = f" — {s['url']}" if s.get("url") else ""
            lines.append(f"  {s['id']} [{s['type']}] {s['label']}{suffix}")
    return "\n".join(lines)


def _mode_ideas(user_text: str) -> str:
    state = _load_sheet_state()
    ctx = gather_context()
    if not state:
        return _NO_DATA_MSG
    analyzed, buckets, results = state
    if not analyzed and not ctx.get("learnings"):
        return _NO_DATA_MSG

    # Pull ALL rows so interpretation can also surface external/inspiration
    # rows (never as evidence). Fall back to analyzed-only if unreachable.
    rows = _load_all_rows() or analyzed

    ideas = interpretation.build_idea_candidates(
        question=user_text, rows=rows, findings=results, context=ctx, limit=5)
    if not ideas:
        return _NO_DATA_MSG
    return _render_ideas(ideas, analyzed, buckets)


def _find_row_by_link(analyzed: list[dict], link: str) -> Optional[dict]:
    def _norm(u: str) -> str:
        u = u.strip().split("?")[0].rstrip("/")
        return u.lower()
    target = _norm(link)
    for r in analyzed:
        if _norm(str(r.get("LINK", ""))) == target:
            return r
    # substring fallback: sometimes the pasted URL differs slightly.
    for r in analyzed:
        raw = _norm(str(r.get("LINK", "")))
        if raw and (raw in target or target in raw):
            return r
    return None


def _row_signals(row: dict) -> list[str]:
    """Human labels of the taxonomy signals present on this row."""
    idx = taxonomy.signal_index()
    labels = []
    for col, meta in idx.items():
        if str(row.get(col, "")).strip() == "1":
            labels.append(f"{meta['label']} ({meta['layer']})")
    return labels


def _mode_feedback(user_text: str) -> str:
    m = _URL_RE.search(user_text)
    ctx = gather_context()
    used = {"guidelines": _guideline_names(ctx)}

    if not m:
        return ("Paste an Instagram link with the word *feedback* and I'll look "
                "it up in the analyzed Sheet.\n\n" + _HELP)

    link = m.group(0).rstrip(">.,);]")
    state = _load_sheet_state()
    if not state:
        return ("I can't reach the analyzed Sheet right now, so I can't look "
                "up that link. Try again once Sheets is configured."
                + _sources_line(used))

    analyzed, buckets, results = state
    row = _find_row_by_link(analyzed, link)
    if not row:
        return (f"That link isn't in the analyzed Sheet yet: {link}\n"
                "Add it to the sheet with a PERFORMANCE value and run "
                "*Run Social Media Learning* to analyze it — then ask me again.")

    used["sheet_rows"] = [row["_row"]]
    perf = str(row.get("PERFORMANCE", "")).strip() or "(none)"
    product = str(row.get("Product", "")).strip() or "(unspecified)"
    icp = str(row.get("ICP", "")).strip() or "(unspecified)"
    signals = _row_signals(row)
    signals_str = ", ".join(signals) if signals else "(none tagged)"

    # Cross-reference this row's signals against the correlations to diagnose.
    win_labels = {(r["label"], r["layer"]) for r in corr.winning(results)}
    weak_labels = {(r["label"], r["layer"]) for r in corr.weak(results)}
    row_signal_pairs = set()
    idx = taxonomy.signal_index()
    for col, meta in idx.items():
        if str(row.get(col, "")).strip() == "1":
            row_signal_pairs.add((meta["label"], meta["layer"]))
    aligned_win = sorted(row_signal_pairs & win_labels)
    aligned_weak = sorted(row_signal_pairs & weak_labels)

    if perf.lower() == "great":
        diagnosis = ("This is a *Great* performer. "
                     + (f"It carries winning-associated signals ({', '.join(f'{l} ({ly})' for l,ly in aligned_win)}). "
                        if aligned_win else "")
                     + "Consider producing more variants in the same hook × format space.")
        next_rec = ("Scale it: replicate the same hook + format on a different "
                    f"product ({product}) angle or ICP variation.")
    elif perf.lower() == "underdog":
        diagnosis = ("This underperformed. "
                     + (f"It carries weak-associated signals ({', '.join(f'{l} ({ly})' for l,ly in aligned_weak)}). "
                        if aligned_weak else "")
                     + "Signals here are correlated, not causal.")
        next_rec = "De-prioritize this pattern until more data confirms; try a different hook × format next."
    else:
        diagnosis = f"Performance bucket: *{perf}*. Signals present: {signals_str}."
        next_rec = "Not enough on this row alone — see *learnings* for what's working overall."

    lines = [
        f"*Feedback on:* {link}",
        f"  • Performance: *{perf}*",
        f"  • Product / ICP: {product} / {icp}",
        f"  • Signals: {signals_str}",
        f"  • Diagnosis: {diagnosis}",
        f"  • Next: {next_rec}",
    ]
    return "\n".join(lines) + _sources_line(used)


def _mode_learnings() -> str:
    ctx = gather_context()
    used = {"guidelines": _guideline_names(ctx),
            "learnings": bool(ctx["learnings"])}

    if not ctx["learnings"]:
        return ("No learnings synthesized yet — run *Generate Learnings* "
                "on the dashboard, then ask me again.")

    state = _load_sheet_state()
    if not state:
        # Have learnings.md but no live sheet — return the file's summary anyway.
        return ctx["learnings"][:3500] + _sources_line(used)

    analyzed, buckets, results = state
    win = corr.winning(results)[:5]
    weak = corr.weak(results)[:5]

    def _fmt(r):
        return (f"  • *{r['label']}* ({r['layer']}) — lift {corr.fmt_lift(r['lift'])} · "
                f"n={r['videos_with_signal']} · {r['confidence']} confidence")

    lines = ["*Current learnings* — correlation, not causation."]
    note = _thin_data_note(analyzed, buckets)
    if note:
        lines.append(note)

    lines.append("\n*Top winning signals — scale these:*")
    lines += [_fmt(r) for r in win] or ["  • (none yet)"]

    lines.append("\n*Top weak signals — avoid or de-prioritize:*")
    lines += [_fmt(r) for r in weak] or ["  • (none yet)"]

    if win:
        first = win[0]
        lines.append(f"\n*Scale:* {first['label']} ({first['layer']}) — "
                     f"associated with a +{corr.fmt_lift(first['lift'])[1:]} lift on Great rate.")
    if weak:
        w = weak[0]
        lines.append(f"*Avoid:* {w['label']} ({w['layer']}) — associated with "
                     f"a {corr.fmt_lift(w['lift'])} lift on Great rate.")

    used["sheet_rows"] = [r["_row"] for r in analyzed[:5]]
    return "\n".join(lines) + _sources_line(used)


def _mode_tests() -> str:
    ctx = gather_context()
    used = {"guidelines": _guideline_names(ctx),
            "learnings": bool(ctx["learnings"])}
    state = _load_sheet_state()
    if not state:
        return ("I can't reach the analyzed Sheet right now, so I can't "
                "propose grounded tests." + _sources_line(used))

    analyzed, buckets, results = state
    if not analyzed:
        return ("No tagged rows yet — run *Generate Learnings* first, then "
                "ask me for tests.")

    import synthesizer
    s = synthesizer.synthesize(analyzed, buckets, results)
    used["sheet_rows"] = [r["_row"] for r in analyzed[:5]]

    tests = s["tests"][:3]
    if not tests:
        return ("Not enough signal to propose tests yet — synthesize on more "
                "tagged rows and try again.")

    lines = ["*Next creative tests* — grounded in current signals."]
    note = _thin_data_note(analyzed, buckets)
    if note:
        lines.append(note)
    for i, t in enumerate(tests, 1):
        lines.append(
            f"\n*{i}. {t['hook']} × {t['format']}* — {t['icp']} / {t['product']}\n"
            f"  • Hypothesis: {t['hypothesis']}\n"
            f"  • Execution: {t['execution']}\n"
            f"  • Confidence: {t.get('confidence', 'Directional')}"
        )
    return "\n".join(lines) + _sources_line(used)


# --- public ---------------------------------------------------------------
def answer_question(user_text: str) -> str:
    text = (user_text or "").strip()
    if not text:
        return _HELP
    mode = _route(text)
    try:
        if mode == "ideas":
            return _mode_ideas(text)
        if mode == "feedback":
            return _mode_feedback(text)
        if mode == "learnings":
            return _mode_learnings()
        if mode == "tests":
            return _mode_tests()
        return _HELP
    except Exception as e:  # noqa: BLE001 - Slack should never see a stack trace
        log.exception("social_brain: mode %s failed", mode)
        return f"Something went wrong answering that. ({type(e).__name__}: {e})"
