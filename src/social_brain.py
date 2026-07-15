"""Marketing Brain answering engine for Slack chat.

`answer_question(user_text)` routes a mention to a deterministic mode — ideas /
feedback / learnings / signals / examples / tests / summary — and returns a
Slack-safe markdown string. Read-only throughout: never writes to the Sheet,
never writes to Notion, never triggers video analysis, never posts anywhere on
its own.

**Notion Brain is the primary memory layer.** Learnings/signal/test questions
try Notion first (via `notion_retrieval.py`); when a database doesn't exist
yet, has no matching entry, or Notion isn't configured, they fall back to
`data/latest_learnings.md` and a live Sheet + correlation computation — the
same data, just recomputed locally instead of read from the synced snapshot.

Every substantive answer cites only the sources it actually used this call,
numbered in retrieval-priority order:
  [S1] Notion: <database> — <title>  (Notion Brain, when used)
  [S2] Notion: <database> — <title>  (a second Notion source, when used)
  ...   latest_learnings.md
  ...   Sheet rows / link
  ...   guideline file(s)

Metrics and links are never invented. Language is always associational
("associated with" / "correlated with"), never causal. Thin-data segments say
so instead of guessing.
"""
from __future__ import annotations

import os
import re
from typing import Optional

import config
import correlations as corr
import interpretation
import notion_retrieval
import social_strategist
import social_retrieval
import taxonomy
from content_context import gather_context
from logger import get_logger

log = get_logger()

_HELP = (
    "Hi — I'm the Storelli Marketing Brain. Ask me:\n"
    "• *ideas* — top rated creative ideas (from INSPIRATION_IDEAS) with proof + critique\n"
    "  _e.g. \"give me 5 BodyShield ideas\", \"which ideas are worth shooting?\", "
    "\"critique the top ideas\", \"which ideas are too generic?\"_\n"
    "• *feedback <link>* — how a specific reel performed and what to do next\n"
    "• *what is working / what should we avoid* — current winning + weak patterns\n"
    "• *what hooks/formats work for <product/ICP>?* / *what did we learn about <product>?*\n"
    "• *examples* — show me example videos (optionally by performance/product/ICP)\n"
    "• *tests* — next creative tests to run\n"
    "• *summarize the brain* — a broad overview across everything synced\n"
    "\n"
    "_I can only read (Notion Brain first, then the Sheet/learnings file as "
    "fallback). To analyze new videos, use the dashboard button "
    "*Analyze All Untagged Videos*._\n"
)

# --- routing ---------------------------------------------------------------
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_IDEAS_KW = ("idea", "ideas", "give me ideas", "what should we post",
             "next video", "next videos", "content idea", "post idea")
_FEEDBACK_KW = ("feedback", "analyze this", "why did this perform",
                "how did this do", "review this")
_LEARNINGS_KW = ("learning", "learnings", "what is working", "what's working",
                 "winning pattern", "winning patterns", "what works",
                 "have we learned", "what have we learned",
                 "show sources", "show me sources", "sources for", "what sources")
_AVOID_KW = ("avoid", "should we avoid", "what to avoid", "de-prioritize",
             "stop doing")
# Deliberately does NOT include a bare "show me" — a combined message like
# "...biggest learnings... show me the sources u used" would otherwise match
# here before the learnings check ever runs, mis-routing a Notion-first
# learnings question to the Sheet-only examples mode (which has no Notion/
# latest_learnings.md fallback at all). Requires the word "example(s)".
_EXAMPLES_KW = ("example", "examples", "show examples", "show me example")
_TESTS_KW = ("test", "tests", "what should we test", "next test", "creative test")
_SIGNAL_HINT_KW = ("work", "works", "avoid", "should")
_ABOUT_KW = ("learn about", "learnings about", "learned about", "tell me about",
             "what did we learn")
_SUMMARY_KW = ("summarize the brain", "summarize", "summary", "brain dump",
               "give me an overview", "overview")


def _has_any(text: str, keywords) -> bool:
    return any(k in text for k in keywords)


def _route(text: str) -> str:
    t = text.lower()
    # A pasted link is unambiguous — always a specific-row lookup.
    if _URL_RE.search(text):
        return "feedback"
    if _has_any(t, _SUMMARY_KW):
        return "summary"
    if _has_any(t, _EXAMPLES_KW):
        return "examples"
    layer = social_retrieval.detect_layer(t)
    filters = social_retrieval.extract_filters(t)
    segment = filters.get("icp") or filters.get("product")
    # "what hooks work for parents?" / "what formats should we avoid?" — a
    # taxonomy-layer word plus a filtering verb means a filtered signal
    # breakdown. "what did we learn about ExoShield?" — a recognized product/
    # ICP plus "learn about" phrasing, with no layer, means the same mode but
    # unfiltered by layer (general product/ICP learnings).
    if layer and _has_any(t, _SIGNAL_HINT_KW):
        return "signals"
    if segment and _has_any(t, _ABOUT_KW):
        return "signals"
    if _has_any(t, _IDEAS_KW):
        return "ideas"
    if _has_any(t, _LEARNINGS_KW) or _has_any(t, _AVOID_KW) or _has_any(t, _FEEDBACK_KW):
        # Bare "why did this perform well?" / "review this" with no link can't
        # look up a specific row, so it falls back to the general patterns.
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


def _cite_notion(chunk: dict) -> str:
    label = chunk.get("title") or chunk.get("database")
    db = chunk.get("database", "Notion")
    if chunk.get("url"):
        return f"Notion: {db} — {label} ({chunk['url']})"
    return f"Notion: {db} — {label}"


def _render_sources(notion_chunks=(), learnings_used: bool = False,
                    sheet_rows=(), guideline_names=()) -> str:
    """Priority-ordered, dynamically numbered Sources line — Notion first,
    then latest_learnings.md, then Sheet rows, then guidelines. Only ever
    cites what was actually retrieved this call; never invents a source."""
    parts = [_cite_notion(c) for c in notion_chunks]
    if learnings_used:
        parts.append("latest_learnings.md")
    if sheet_rows:
        parts.append("Sheet rows: " + ", ".join(str(r) for r in list(sheet_rows)[:5]))
    if guideline_names:
        parts.append("guidelines: " + ", ".join(list(guideline_names)[:3]))
    if not parts:
        return ""
    return "\n\n_Sources:_ " + " · ".join(f"[S{i}] {p}" for i, p in enumerate(parts, 1))


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
    body = _render_ideas(ideas, analyzed, buckets)

    # Best-effort Notion supplement (informational, not a numbered citation —
    # this mode already has its own tested S1/S2 source registry above).
    product = interpretation.detect_product(user_text)
    icp = interpretation.detect_icp(user_text)
    if product or icp:
        notion_matches = (notion_retrieval.query("Product Learnings", product=product, limit=5)
                          + notion_retrieval.query("Generated Social Ideas", product=product, icp=icp, limit=5))
        if notion_matches:
            c = notion_matches[0]
            body += f"\n\n_Also in Notion Brain:_ {c.get('database')} — {c.get('title')}"
    return body


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
    guideline_names = _guideline_names(ctx)

    if not m:
        return ("Paste an Instagram link with the word *feedback* and I'll look "
                "it up in the analyzed Sheet.\n\n" + _HELP)

    link = m.group(0).rstrip(">.,);]")
    state = _load_sheet_state()
    if not state:
        return ("I can't reach the analyzed Sheet right now, so I can't look "
                "up that link. Try again once Sheets is configured."
                + _render_sources(guideline_names=guideline_names))

    analyzed, buckets, results = state
    row = _find_row_by_link(analyzed, link)
    if not row:
        return (f"That link isn't in the analyzed Sheet yet: {link}\n"
                "Add it to the sheet with a PERFORMANCE value and run "
                "*Run Social Media Learning* to analyze it — then ask me again.")

    sheet_rows = [row["_row"]]
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
    return "\n".join(lines) + _render_sources(sheet_rows=sheet_rows, guideline_names=guideline_names)


def _signal_library_split(chunks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split Notion 'Signal Library' chunks into (winning, weak) by which
    field ('Works Best For' vs 'Weak For') notion_brain.py populated at sync
    time — that already encodes the win/weak classification, no recomputation."""
    win, weak = [], []
    for c in chunks:
        extra = c.get("extra", {})
        if str(extra.get("Works Best For", "")).strip():
            win.append(c)
        elif str(extra.get("Weak For", "")).strip():
            weak.append(c)
    return win, weak


def _fmt_notion_signal(c: dict) -> str:
    extra = c.get("extra", {})
    bits = []
    if extra.get("Evidence Count"):
        bits.append(f"n={extra['Evidence Count']}")
    if c.get("confidence"):
        bits.append(f"{c['confidence']} confidence")
    tail = " · ".join(bits)
    layer = extra.get("Layer", "")
    return f"  • *{c.get('title', '')}*{f' ({layer})' if layer else ''}{' — ' + tail if tail else ''}"


def _mode_learnings() -> str:
    ctx = gather_context()
    guideline_names = _guideline_names(ctx)
    learnings_used = bool(ctx.get("learnings"))

    # Notion-first: Signal Library already encodes the win/weak split from
    # the last notion-sync, so no recomputation is needed if it's populated.
    notion_win, notion_weak = _signal_library_split(notion_retrieval.query("Signal Library", limit=40))
    if notion_win or notion_weak:
        top_win, top_weak = notion_win[:3], notion_weak[:2]
        lines = ["*Current learnings* (Notion Brain) — correlation, not causation."]
        lines.append("\n*Winning — scale these:*")
        lines += [_fmt_notion_signal(c) for c in top_win] or ["  • (none yet)"]
        lines.append("\n*Weak — avoid/de-prioritize:*")
        lines += [_fmt_notion_signal(c) for c in top_weak] or ["  • (none yet)"]
        cited = top_win + top_weak
        return "\n".join(lines) + _render_sources(notion_chunks=cited, learnings_used=learnings_used)

    # Fallback: Notion not configured / not synced yet / empty -> live Sheet +
    # correlation computation (same underlying data, computed on the spot).
    if not ctx["learnings"]:
        state = _load_sheet_state()
        if not state or not state[0]:
            return _NO_DATA_MSG

    state = _load_sheet_state()
    if not state:
        if not ctx["learnings"]:
            return _NO_DATA_MSG
        # Have learnings.md but no live sheet — return the file's summary anyway.
        return ("_Notion Brain has no synced signals yet — showing "
                "latest_learnings.md._\n\n" + ctx["learnings"][:3500]
                + _render_sources(learnings_used=learnings_used))

    analyzed, buckets, results = state
    if not analyzed:
        return _NO_DATA_MSG
    win = corr.winning(results)[:3]
    weak = corr.weak(results)[:2]

    def _fmt(r):
        return (f"  • *{r['label']}* ({r['layer']}) — lift {corr.fmt_lift(r['lift'])} · "
                f"n={r['videos_with_signal']} · {r['confidence']} confidence")

    lines = ["*Current learnings* — correlation, not causation."]
    note = _thin_data_note(analyzed, buckets)
    if note:
        lines.append(note)

    lines.append("\n*Winning — scale these:*")
    lines += [_fmt(r) for r in win] or ["  • (none yet)"]

    lines.append("\n*Weak — avoid/de-prioritize:*")
    lines += [_fmt(r) for r in weak] or ["  • (none yet)"]

    sheet_rows = [r["_row"] for r in analyzed[:5]]
    return "\n".join(lines) + _render_sources(learnings_used=learnings_used,
                                              sheet_rows=sheet_rows,
                                              guideline_names=guideline_names)


def _mode_signals(user_text: str) -> str:
    """Filtered signal breakdown for questions like 'what hooks work for
    parents?', 'what formats should we avoid?', or 'what did we learn about
    ExoShield?'. Notion-first: a detected Product/ICP checks the matching
    'Product Learnings' / 'ICP Learnings' Notion entry (which already stores
    per-segment best hooks/formats from the last sync); a layer with no
    segment checks Notion 'Signal Library'. Falls back to a live Sheet +
    correlation computation — recomputed within the Product/ICP subgroup when
    Notion has nothing for that specific segment."""
    layer = social_retrieval.detect_layer(user_text)
    filters = social_retrieval.extract_filters(user_text)
    icp, product = filters.get("icp"), filters.get("product")

    if icp or product:
        db = "ICP Learnings" if icp else "Product Learnings"
        seg = icp or product
        matches = notion_retrieval.query(db, icp=icp, product=product, limit=25)
        if matches:
            c = matches[0]
            extra = c.get("extra", {})
            layer_label = layer.replace("_", " ").title() if layer else "What we know"
            lines = [f"*{layer_label} for {seg}* (Notion Brain) — associated with "
                     "performance, not causal."]
            if not layer or layer == "hook":
                lines.append(f"  • Best hooks: {c.get('hook') or 'n/a'}")
            if not layer or layer == "format":
                lines.append(f"  • Best formats: {c.get('format') or 'n/a'}")
            direction = extra.get("Next Direction") or extra.get("Recommended Messaging")
            if direction:
                lines.append(f"  • {direction}")
            if extra.get("Weak Angles"):
                lines.append(f"  • Weak angles: {extra['Weak Angles']}")
            if extra.get("Core Motivation"):
                lines.append(f"  • Core motivation: {extra['Core Motivation']}")
            if c.get("confidence"):
                lines.append(f"  • Confidence: {c['confidence']}")
            return "\n".join(lines) + _render_sources(notion_chunks=[c])

    if layer and not (icp or product):
        layer_chunks = [c for c in notion_retrieval.query("Signal Library", limit=40)
                        if c.get("extra", {}).get("Layer", "").lower() == layer]
        if layer_chunks:
            win, weak = _signal_library_split(layer_chunks)
            top_win, top_weak = win[:3], weak[:2]
            layer_label = layer.replace("_", " ").title()
            lines = [f"*{layer_label}* (Notion Brain) — associated with performance, not causal."]
            lines.append("\n*Work well:*")
            lines += [_fmt_notion_signal(c) for c in top_win] or ["  • (none yet)"]
            lines.append("\n*Avoid / weak:*")
            lines += [_fmt_notion_signal(c) for c in top_weak] or ["  • (none yet)"]
            return "\n".join(lines) + _render_sources(notion_chunks=(top_win + top_weak))

    # Fallback: Notion has nothing for this specific slice (not configured,
    # not synced, or this segment/layer combo wasn't in the last sync) -> a
    # live Sheet computation, segmented the same way, is more accurate anyway.
    ctx = gather_context()
    guideline_names = _guideline_names(ctx)
    state = _load_sheet_state()
    if not state:
        return ("I can't reach the analyzed Sheet right now, so I can't "
                "answer that." + _render_sources(guideline_names=guideline_names))
    analyzed, buckets, results = state
    if not analyzed:
        return _NO_DATA_MSG

    seg_results, seg_note = social_retrieval.segment_results(analyzed, buckets, results, filters)
    win = social_retrieval.signals_for_layer(seg_results, layer, winning=True) if layer \
        else corr.winning(seg_results)
    weak = social_retrieval.signals_for_layer(seg_results, layer, winning=False) if layer \
        else corr.weak(seg_results)

    def _fmt(r):
        return (f"  • *{r['label']}* — lift {corr.fmt_lift(r['lift'])} · "
                f"n={r['videos_with_signal']} · {r['confidence']} confidence")

    layer_label = layer.replace("_", " ").title() if layer else "Signals"
    lines = [f"*{layer_label}{seg_note}* — associated with performance, not causal."]
    note = _thin_data_note(analyzed, buckets)
    if note:
        lines.append(note)
    lines.append("\n*Work well:*")
    lines += [_fmt(r) for r in win[:3]] or ["  • (none yet)"]
    lines.append("\n*Avoid / weak:*")
    lines += [_fmt(r) for r in weak[:2]] or ["  • (none yet)"]

    sheet_rows = [r["_row"] for r in analyzed[:5]]
    return "\n".join(lines) + _render_sources(learnings_used=bool(ctx.get("learnings")),
                                              sheet_rows=sheet_rows,
                                              guideline_names=guideline_names)


def _mode_examples(user_text: str) -> str:
    """Show concrete example rows, optionally filtered by performance bucket /
    product / ICP (e.g. 'show me examples of Great videos'). Notion Brain
    doesn't store per-video rows, so this mode is Sheet-only by nature."""
    ctx = gather_context()
    guideline_names = _guideline_names(ctx)
    state = _load_sheet_state()
    if not state:
        return ("I can't reach the analyzed Sheet right now, so I can't pull "
                "examples." + _render_sources(guideline_names=guideline_names))
    analyzed, buckets, _results = state
    if not analyzed:
        return _NO_DATA_MSG

    filters = social_retrieval.extract_filters(user_text)
    pool = social_retrieval.filter_rows(analyzed, filters)
    if not pool:
        seg = filters.get("performance") or filters.get("product") or filters.get("icp") or "that"
        return f"No tagged rows match {seg} yet." + _render_sources(guideline_names=guideline_names)

    examples = social_retrieval.example_rows(pool, limit=3)
    heading = "*Examples"
    if filters.get("performance"):
        heading += f" — {filters['performance']}"
    heading += ":*"
    lines = [heading]
    note = _thin_data_note(analyzed, buckets)
    if note:
        lines.append(note)
    for r in examples:
        link = str(r.get("LINK", "")).strip() or "(no link)"
        perf = str(r.get("PERFORMANCE", "")).strip() or "?"
        signals = _row_signals(r)
        sig_str = ", ".join(signals[:4]) if signals else "(none tagged)"
        lines.append(f"  • row {r['_row']} — *{perf}* — {link}\n    Signals: {sig_str}")

    sheet_rows = [r["_row"] for r in examples]
    return "\n".join(lines) + _render_sources(sheet_rows=sheet_rows, guideline_names=guideline_names)


def _mode_tests() -> str:
    # Notion-first: the 'Next Creative Tests' DB already stores the last
    # synthesizer output, plus any operator edits to Status/Result.
    notion_tests = notion_retrieval.query("Next Creative Tests", limit=10)
    if notion_tests:
        tests = notion_tests[:3]
        lines = ["*Next creative tests* (Notion Brain) — grounded in current signals."]
        for i, t in enumerate(tests, 1):
            extra = t.get("extra", {})
            lines.append(
                f"\n*{i}. {t.get('hook', 'n/a')} × {t.get('format', 'n/a')}* — "
                f"{t.get('icp', 'n/a')} / {t.get('product', 'n/a')}\n"
                f"  • Hypothesis: {t.get('title', '')}\n"
                f"  • Priority: {extra.get('Priority', 'n/a')}"
                + (f" · Status: {extra.get('Status')}" if extra.get("Status") else "")
            )
        return "\n".join(lines) + _render_sources(notion_chunks=tests)

    # Fallback: Notion not configured / no tests synced yet -> compute live.
    ctx = gather_context()
    guideline_names = _guideline_names(ctx)
    state = _load_sheet_state()
    if not state:
        return ("I can't reach the analyzed Sheet right now, so I can't "
                "propose grounded tests." + _render_sources(guideline_names=guideline_names))

    analyzed, buckets, results = state
    if not analyzed:
        return ("No tagged rows yet — run *Generate Learnings* first, then "
                "ask me for tests.")

    import synthesizer
    s = synthesizer.synthesize(analyzed, buckets, results)
    sheet_rows = [r["_row"] for r in analyzed[:5]]

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
    return "\n".join(lines) + _render_sources(learnings_used=bool(ctx.get("learnings")),
                                              sheet_rows=sheet_rows,
                                              guideline_names=guideline_names)


def _mode_summary() -> str:
    """Broad, compact overview across everything synced ('summarize the
    brain'). Notion-first, falls back to a live Sheet + learnings summary."""
    all_dbs = notion_retrieval.fetch_all(limit_per_db=25)
    if all_dbs:
        lines = ["*Storelli Marketing Brain — summary* (Notion Brain) — "
                 "correlation, not causation."]
        win, weak = _signal_library_split(all_dbs.get("Signal Library", []))
        cited = []
        if win:
            lines.append(f"  • Top winning signal: *{win[0]['title']}*")
            cited.append(win[0])
        if weak:
            lines.append(f"  • Top weak signal: *{weak[0]['title']}*")
            cited.append(weak[0])
        ml = all_dbs.get("Marketing Learnings", [])
        if ml:
            lines.append(f"  • {len(ml)} marketing learning(s) synced")
        tests = all_dbs.get("Next Creative Tests", [])
        if tests:
            lines.append(f"  • Next test queued: {tests[0].get('title', '')}")
            cited.append(tests[0])
        covered = []
        if all_dbs.get("Product Learnings"):
            covered.append(f"{len(all_dbs['Product Learnings'])} product(s)")
        if all_dbs.get("ICP Learnings"):
            covered.append(f"{len(all_dbs['ICP Learnings'])} ICP(s)")
        if all_dbs.get("Generated Social Ideas"):
            covered.append(f"{len(all_dbs['Generated Social Ideas'])} generated idea(s)")
        if covered:
            lines.append("  • Also covering: " + ", ".join(covered))
        if len(lines) == 1:
            lines.append("  • Notion Brain databases exist but are empty — "
                         "run *notion-sync* after tagging more videos.")
        return "\n".join(lines) + _render_sources(notion_chunks=cited)

    # Fallback: Notion not configured / not synced yet -> summarize live.
    ctx = gather_context()
    guideline_names = _guideline_names(ctx)
    state = _load_sheet_state()
    if not state and not ctx.get("learnings"):
        return _NO_DATA_MSG

    lines = ["*Storelli Marketing Brain — summary* — correlation, not causation."]
    sheet_rows = []
    if state:
        analyzed, buckets, results = state
        if not analyzed:
            return _NO_DATA_MSG
        win = corr.winning(results)
        weak = corr.weak(results)
        lines.append(f"  • {len(analyzed)} tagged video(s) analyzed")
        if win:
            lines.append(f"  • Top winning signal: *{win[0]['label']}* ({win[0]['layer']})")
        if weak:
            lines.append(f"  • Top weak signal: *{weak[0]['label']}* ({weak[0]['layer']})")
        note = _thin_data_note(analyzed, buckets)
        if note:
            lines.append(note)
        sheet_rows = [r["_row"] for r in analyzed[:5]]
    else:
        lines.append("  • Live Sheet unreachable — summarizing from latest_learnings.md.")

    return "\n".join(lines) + _render_sources(learnings_used=bool(ctx.get("learnings")),
                                              sheet_rows=sheet_rows,
                                              guideline_names=guideline_names)


# --- public ---------------------------------------------------------------
def answer_question(user_text: str) -> str:
    text = (user_text or "").strip()
    if not text:
        return _HELP
    mode = _route(text)
    try:
        if mode == "ideas":
            # Prefer the curated RATED ideas (INSPIRATION_IDEAS, Milestone 4A/4B);
            # fall back to the live signal-grounded generator when none exist.
            import idea_retrieval
            return idea_retrieval.answer_ideas(text, fallback=lambda: _mode_ideas(text))
        if mode == "feedback":
            return _mode_feedback(text)
        if mode == "learnings":
            return _mode_learnings()
        if mode == "tests":
            return _mode_tests()
        if mode == "signals":
            return _mode_signals(text)
        if mode == "examples":
            return _mode_examples(text)
        if mode == "summary":
            return _mode_summary()
        return _HELP
    except Exception as e:  # noqa: BLE001 - Slack should never see a stack trace
        log.exception("social_brain: mode %s failed", mode)
        return f"Something went wrong answering that. ({type(e).__name__}: {e})"


# ---------------------------------------------------------------------------
# Conversational mode — thread-aware follow-ups on top of the modes above.
#
# answer_conversation() never re-invents retrieval: every follow-up either (a)
# transforms the PREVIOUS assistant message deterministically (expand a numbered
# item, re-show its sources, compress it, reformat it as a brief) so nothing new
# is invented, or (b) re-runs one of the existing grounded modes above with a
# segment/topic pulled from the conversation. LLM polish (opt-in, see
# config.SLACK_LLM_POLISH_ENABLED) may only reword the result — it is validated
# afterward and discarded if it drops/adds a citation, adds a number that wasn't
# already present, or uses causal language.
# ---------------------------------------------------------------------------
_EXPAND_RE = re.compile(r"\bexpand\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"#\s*(\d+)|\b(\d+)\b")
_SHOW_SOURCES_KW = ("show me sources", "show sources", "what source", "what sources",
                    "cite your sources", "sources?")
_SOURCE_DEBUG_KW = ("source debug", "sources you used", "debug sources")
_BRIEF_KW = ("content brief", "into a brief", "make it a brief")
_SHORTER_KW = ("shorter", "make it shorter", "tighten", "condense", "trim it")
_MORE_KW = ("more", "5 more", "few more", "another one", "give me more")
_WHY_KW = ("why?", "why do you recommend", "why is that", "why though", "why not")
_MAKE_FOR_RE = re.compile(r"\bmake (?:it|that|those|these)\s+for\s+(.+)", re.IGNORECASE)
_NEXT_KW = ("what should we do next", "what's next", "what next", "next steps")
_RISKY_KW = ("risky version", "risky one", "bolder version", "bolder one",
            "more aggressive", "spicier", "edgier")

_FOLLOWUP_NUDGE = "\n\n_Want me to turn this into content briefs, or go deeper on any part?_"


def _classify_followup(text: str) -> str:
    t = text.lower().strip()
    if _has_any(t, _SOURCE_DEBUG_KW):
        return "source_debug"
    if _has_any(t, _SHOW_SOURCES_KW):
        return "sources"
    if _EXPAND_RE.search(t):
        return "expand"
    if _MAKE_FOR_RE.search(t):
        return "make_for"
    if _has_any(t, _BRIEF_KW):
        return "brief"
    if _has_any(t, _RISKY_KW):
        return "risky"
    if _has_any(t, _SHORTER_KW):
        return "shorter"
    if t in ("why?", "why", "why though") or _has_any(t, _WHY_KW):
        return "why"
    if _has_any(t, _MORE_KW):
        return "more"
    if _has_any(t, _NEXT_KW):
        return "next"
    return "none"


def _extract_sources_block(text: str) -> str:
    """Grab the trailing sources block, whichever style rendered it — the
    single-line '_Sources:_ [S1]...' (most modes), the multi-line
    '*Sources:*\\n  S1 ...' block (ideas mode), or the plain 'Sources: [S1],
    [S2]' line a strategist-composed answer renders per its prompt template."""
    m = re.search(r"_Sources:_.*", text)
    if m:
        return m.group(0)
    idx = text.find("*Sources:*")
    if idx != -1:
        return text[idx:].strip()
    m2 = re.search(r"^Sources:\s*\[S\d+\].*$", text, re.MULTILINE | re.IGNORECASE)
    return m2.group(0).strip() if m2 else ""


def _split_numbered_items(text: str) -> list[str]:
    """Split a rendered list-style reply into its numbered items — works for
    both '*1. Title*' (ideas) and '*1. Hook × Format*' (tests) renderings."""
    parts = re.split(r"\n(?=\*\d+\.\s)", text)
    return [p.strip() for p in parts if re.match(r"^\*\d+\.\s", p.strip())]


def _detect_last_mode(last_assistant: str) -> str:
    """Best-effort classification of which mode produced the previous answer,
    from its rendering — used to decide how to re-run it for a follow-up.
    Checks both the deterministic-mode renderings AND the strategist's own
    contract shapes (a strategist-composed message won't contain the
    deterministic modes' exact markup, e.g. "*Ideas grounded")."""
    if "*Ideas grounded" in last_assistant:
        return "ideas"
    if "Next creative tests" in last_assistant:
        return "tests"
    if "*Storelli Marketing Brain — summary*" in last_assistant:
        return "summary"
    if last_assistant.startswith("*Examples"):
        return "examples"
    if last_assistant.startswith("*Feedback on:") or last_assistant.startswith("Diagnosis:"):
        return "feedback"
    if "*Current learnings*" in last_assistant or "Winning —" in last_assistant:
        return "learnings"
    if "Work well:" in last_assistant or "associated with performance" in last_assistant:
        return "signals"
    # Strategist contract shapes (see social_strategist.py's prompt template).
    if re.search(r"^Ideas:\s*$", last_assistant, re.MULTILINE):
        return "ideas"
    if "Biggest learnings:" in last_assistant:
        return "learnings"
    return "ideas"


def _followup_sources(last_assistant: str) -> str:
    block = _extract_sources_block(last_assistant)
    if not block:
        return ("I didn't cite any sources in my last message — ask me a grounded "
                "question (like *what is working?*) and I'll show sources with the answer.")
    return "Here are the sources behind that:\n\n" + block


def _followup_expand(text: str, last_assistant: str) -> str:
    items = _split_numbered_items(last_assistant)
    if not items:
        return ("I don't have a numbered list from my last message to expand on — "
                "ask a specific question and I'll go deeper.")
    m = _NUMBER_RE.search(text)
    n = int(next(g for g in m.groups() if g)) if m else 1
    if n < 1 or n > len(items):
        return f"I only had {len(items)} item(s) last time — try a number between 1 and {len(items)}."
    return f"*Expanding on #{n}:*\n\n{items[n - 1]}"


def _followup_make_for(text: str, context: list[dict]) -> str:
    m = _MAKE_FOR_RE.search(text)
    segment = (m.group(1).strip() if m else text).rstrip("?.!")
    last_assistant = next((m2["text"] for m2 in reversed(context) if m2.get("role") == "assistant"), "")
    last_user = next((m2["text"] for m2 in reversed(context) if m2.get("role") == "user"), "")
    mode = _detect_last_mode(last_assistant)
    if mode in ("learnings", "signals") and (social_retrieval.detect_layer(last_user)
                                             or social_retrieval.detect_layer(text)):
        return _mode_signals(f"{last_user} for {segment}".strip())
    # Ideas (and any other prior mode) — re-run ideas biased to the new segment;
    # this covers the common case ("make it for parents" after an ideas answer)
    # and degrades sensibly otherwise.
    return _mode_ideas(f"{last_user or 'ideas'} for {segment}".strip())


def _followup_brief(last_assistant: str) -> str:
    items = _split_numbered_items(last_assistant)
    item = items[0] if items else last_assistant

    def _field(label: str) -> str:
        fm = re.search(rf"{label}:\s*(.+)", item)
        return fm.group(1).strip() if fm else ""

    title_m = re.search(r"\*\d+\.\s*(.+?)\*", item)
    title = title_m.group(1) if title_m else "Untitled"
    blocks_m = re.search(r"Story blocks:\n((?:\s*-.+\n?)+)", item)
    blocks = blocks_m.group(1).strip() if blocks_m else ""
    hook, structure = _field("Hook"), _field("Structure")
    prod_icp, why = _field("Product / ICP"), _field("Why")
    conf, sources = _field("Confidence"), _field("Sources")
    if not (hook or structure or blocks):
        return ("I don't have a prior idea to turn into a brief — ask for "
                "*ideas* first, then \"turn this into a content brief\".")
    return (
        f"*Content Brief — {title}*\n"
        f"  • Hook: {hook or 'n/a'}\n"
        f"  • Structure: {structure or 'n/a'}\n"
        f"  • Product / ICP: {prod_icp or 'n/a'}\n"
        f"  • Beats:\n{blocks or '    (none)'}\n"
        f"  • Why: {why or 'n/a'}\n"
        f"  • Confidence: {conf or 'n/a'}\n"
        f"  • Sources: {sources or 'n/a'}"
    )


def _followup_risky(last_assistant: str) -> str:
    items = _split_numbered_items(last_assistant)
    if not items:
        return "I don't have prior ideas to pick a bolder angle from — ask for *ideas* first."
    risky = next((it for it in items if re.search(r"fear|risk", it, re.IGNORECASE)), items[0])
    return "*Here's the bolder angle:*\n\n" + risky


def _followup_shorter(last_assistant: str) -> str:
    src = _extract_sources_block(last_assistant)
    body = last_assistant
    if src:
        body = body[:body.find(src)]
    lines = [l for l in body.splitlines() if l.strip()]
    out = "\n".join(lines[:4]) or "(nothing to shorten)"
    return out + (f"\n\n{src}" if src else "")


def _followup_why(last_assistant: str) -> str:
    lines = [l for l in last_assistant.splitlines()
            if re.search(r"\b(why|diagnosis|associated with|correlated with|lift)\b", l, re.IGNORECASE)]
    src = _extract_sources_block(last_assistant)
    if not lines and not src:
        return ("I don't have a specific reason recorded from my last message — "
                "ask a grounded question like *what is working?* and I'll explain with evidence.")
    body = "\n".join(lines[:5]) if lines else "(see sources below)"
    return "Here's the evidence behind that:\n\n" + body + (f"\n\n{src}" if src else "")


def _followup_more(context: list[dict]) -> str:
    last_user = next((m["text"] for m in reversed(context) if m.get("role") == "user"), "")
    last_assistant = next((m["text"] for m in reversed(context) if m.get("role") == "assistant"), "")
    if _detect_last_mode(last_assistant) == "ideas":
        return ("_Current signals support at most 5 grounded ideas at once — here they are:_\n\n"
                + _mode_ideas(last_user or "ideas"))
    return answer_question(last_user or "learnings")


# --- optional LLM polish (opt-in, validated, never the source of truth) -----
_CAUSAL_WORDS = ("causes", "caused by", "causing", "leads to", "results in", "because of the")


def _has_causal_language(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in _CAUSAL_WORDS)


def _citations_preserved(original: str, polished: str) -> bool:
    orig = set(re.findall(r"\[S\d+\]", original))
    new = set(re.findall(r"\[S\d+\]", polished))
    if not orig:
        return not new  # nothing to cite -> polished must not invent a citation
    return bool(new) and new.issubset(orig)


def _introduced_new_numbers(original: str, polished: str) -> bool:
    orig_nums = set(re.findall(r"\d+%|\bn=\d+\b", original))
    new_nums = set(re.findall(r"\d+%|\bn=\d+\b", polished))
    return not new_nums.issubset(orig_nums)


def _maybe_llm_polish(deterministic_text: str, user_text: str) -> Optional[str]:
    """Best-effort, opt-in (config.SLACK_LLM_POLISH_ENABLED): ask Gemini to
    reword the deterministic answer more conversationally. Never re-retrieves
    facts itself. Returns None (caller uses the deterministic text as-is) when
    disabled, on any failure, or when the output fails validation — dropped or
    invented [S#] citations, invented numbers, or causal language."""
    if not (config.SLACK_LLM_POLISH_ENABLED and config.GEMINI_API_KEY):
        return None
    try:
        from gemini_client import GeminiClient
        prompt = (
            "Rewrite the following Slack bot reply to sound more natural and "
            "conversational, as a direct reply to the user's message below. "
            "Keep every fact, number, and [S#] citation EXACTLY as given — do "
            "not add, remove, or change any of them, and do not invent new "
            "sources, links, or metrics. Never say something 'causes' or "
            "'leads to' performance — only 'associated with' / 'correlated "
            "with'. Keep it roughly the same length. Use Slack's mrkdwn "
            "formatting, NOT standard Markdown: *single asterisks* for bold "
            "(never **double asterisks**), _underscores_ for italics, and "
            "plain '•' or '-' for bullets. Return ONLY the rewritten text, "
            "no commentary.\n\n"
            f"User asked: {user_text}\n\nOriginal reply:\n{deterministic_text}"
        )
        polished = GeminiClient().summarize_findings(prompt).strip()
    except Exception as e:  # noqa: BLE001 - LLM polish is optional, never fatal
        log.warning("LLM polish failed (%s); using deterministic answer.", e)
        return None

    if not polished:
        return None
    # Defensive: normalize standard-Markdown bold to Slack's mrkdwn syntax
    # regardless of whether the model actually followed the prompt's instruction.
    polished = re.sub(r"\*\*(.+?)\*\*", r"*\1*", polished)
    if not _citations_preserved(deterministic_text, polished):
        log.warning("LLM polish dropped/altered citations; using deterministic answer.")
        return None
    if _introduced_new_numbers(deterministic_text, polished):
        log.warning("LLM polish introduced numbers not in the original; using deterministic answer.")
        return None
    if _has_causal_language(polished):
        log.warning("LLM polish introduced causal language; using deterministic answer.")
        return None
    return polished


def _finish_conversational(base: str, user_text: str, skip_polish: bool = False) -> str:
    """Optional validated LLM polish (skipped when strategist mode already ran),
    then the CEO-conversation style pass: strip canned endings (no auto
    "want me to…" nudge) and enforce the length for the detected mode
    (concise / default / deep), always preserving the Sources block."""
    text = base
    if not skip_polish and not config.SLACK_STRATEGIST_MODE_ENABLED:
        text = _maybe_llm_polish(base, user_text) or base
    import slack_response_style as style
    return style.compact_slack_response(text, style.detect_response_mode(user_text))


def _deterministic_conversation_answer(text: str, context: list, last_assistant: str) -> str:
    """The proven, fully-deterministic engine from the conversational-mode
    milestone — used as-is whenever strategist mode is off, unavailable, or
    its output fails validation."""
    if not last_assistant:
        return answer_question(text)
    follow_up = _classify_followup(text)
    if follow_up == "source_debug":
        return social_strategist.render_source_debug(text, context)
    if follow_up == "sources":
        return _followup_sources(last_assistant)
    if follow_up == "expand":
        return _followup_expand(text, last_assistant)
    if follow_up == "make_for":
        return _followup_make_for(text, context)
    if follow_up == "brief":
        return _followup_brief(last_assistant)
    if follow_up == "risky":
        return _followup_risky(last_assistant)
    if follow_up == "shorter":
        return _followup_shorter(last_assistant)
    if follow_up == "why":
        return _followup_why(last_assistant)
    if follow_up == "more":
        return _followup_more(context)
    if follow_up == "next":
        return _mode_tests()
    return answer_question(text)


# --- public: conversational entrypoint --------------------------------------
def answer_conversation(user_text: str, conversation_context: Optional[list[dict]] = None,
                        channel_context: Optional[dict] = None, progress_cb=None) -> str:
    """Thread-aware, multi-turn variant of answer_question().

    conversation_context: chronological (oldest-first) list of
    {"role": "user"|"assistant", "text": str} — typically live Slack thread
    history or the lightweight in-memory cache (see slack_bot.py).

    When strategist mode is enabled (config.SLACK_STRATEGIST_MODE_ENABLED,
    default on when GEMINI_API_KEY is set), retrieval happens first via the
    same deterministic modes as always, then `social_strategist.py` asks
    Gemini to compose real judgment — a recommendation, tradeoffs, a "why" —
    from that already-cited evidence pack, never inventing a source or fact.
    Any failure, invalid citation, invented number, or causal claim discards
    the LLM output and falls through to the proven deterministic engine
    (expand/sources/shorter/brief/risky — no new retrieval, so nothing is
    invented — or a fresh re-run of the matching grounded mode).

    progress_cb(str), if given, is called with short PUBLIC stage names
    ("notion", "evidence", "writing") right before each real phase of work —
    never private chain-of-thought, just what a Slack progress indicator
    shows the user is happening. channel_context is reserved for future
    per-channel metadata and is currently unused.
    """
    text = (user_text or "").strip()
    if not text:
        return _HELP

    context = conversation_context or []
    last_assistant = next((m["text"] for m in reversed(context) if m.get("role") == "assistant"), "")

    # "source debug" / "show me the sources you used" is a deterministic,
    # mechanical reflection of what was actually cited — never routed through
    # Gemini (nothing to compose; it's for operators, not the strategist voice).
    if last_assistant and _classify_followup(text) == "source_debug":
        return social_strategist.render_source_debug(text, context)

    # Rated-idea retrieval (Milestone 4B) is deterministic, cited, and read-only —
    # answer it directly instead of routing idea asks through the LLM strategist.
    # Follow-up transforms (e.g. "turn this into a brief") are NOT idea queries
    # and fall through to the existing conversational engine below.
    try:
        # Semantic inspiration layer first: "what videos should we take
        # inspiration from" must return external reference videos, not the idea
        # list. Read-only.
        import semantic_connections as sc
        if sc.is_inspiration_query(text):
            insp = sc.answer_inspiration(text, context)
            if insp:
                return _finish_conversational(insp, text, skip_polish=True)

        # Conversational RAG orchestrator: reasoning-heavy intents (urgent
        # tests, deep-dive on a prior/named idea, compare) with thread memory.
        # Returns None for everything else -> the concise retrieval paths below.
        import slack_conversation_orchestrator as orch
        reasoned = orch.answer(text, context)
        if reasoned:
            return _finish_conversational(reasoned, text, skip_polish=True)

        import calendar_retrieval
        if calendar_retrieval.is_calendar_query(text):
            return _finish_conversational(calendar_retrieval.answer_calendar(text), text,
                                          skip_polish=True)
        import idea_retrieval
        if idea_retrieval.is_idea_query(text):
            answer = idea_retrieval.answer_ideas(text, fallback=lambda: _mode_ideas(text))
            return _finish_conversational(answer, text, skip_polish=True)
    except Exception as e:  # noqa: BLE001 - never let idea/calendar retrieval break the bot
        log.warning("idea/calendar retrieval intercept failed, falling through: %s", e)

    try:
        if config.SLACK_STRATEGIST_MODE_ENABLED and config.GEMINI_API_KEY:
            pack = social_strategist.build_context_pack(text, context, progress_cb=progress_cb)
            strategic = social_strategist.compose_strategic_answer(text, context, pack,
                                                                    progress_cb=progress_cb)
            if strategic:
                return _finish_conversational(strategic, text, skip_polish=True)

        base = _deterministic_conversation_answer(text, context, last_assistant)
        return _finish_conversational(base, text)
    except Exception as e:  # noqa: BLE001 - Slack should never see a stack trace
        log.exception("answer_conversation failed")
        return f"Something went wrong answering that. ({type(e).__name__}: {e})"
