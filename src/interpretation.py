"""Idea interpretation layer — turns evidence into grounded Storelli reels.

Reads (never writes):
  - Analyzed sheet rows (internal = evidence; external = inspiration only)
  - Correlation findings (winning / weak signals with lift + confidence)
  - Synthesized learnings (data/latest_learnings.md, via content_context)
  - Guideline files (data/guidelines/*.md, via content_context)
  - Storelli product context (taxonomy.PRODUCT / PRODUCT_CONTEXT)

Core rule: internal Storelli content = evidence; external/reference content
= inspiration only. Reference rows never enter the learning layer (guarded by
`performance.is_reference_row`) and never justify a lift here either.

Every idea returned cites only sources it actually consulted. No invented
links, no invented metrics, always associational language.
"""
from __future__ import annotations

import re

import correlations as corr
import performance
import taxonomy
from logger import get_logger

log = get_logger()

# Confidence label mapping (corr uses High/Medium/Low; spec asks for
# Low/Directional/Medium/Strong for the returned idea).
_CONF_MAP = {"High": "Strong", "Medium": "Medium", "Low": "Low"}


def _idea_confidence(*signals: dict) -> str:
    """Blend the correlation confidence of the signals backing an idea into
    the four-level label: Strong / Medium / Low / Directional."""
    if not signals:
        return "Directional"
    labels = [_CONF_MAP.get(s.get("confidence", ""), "Directional") for s in signals]
    if "Strong" in labels and labels.count("Strong") >= 1:
        return "Strong"
    if "Medium" in labels:
        return "Medium"
    if "Low" in labels:
        return "Low"
    return "Directional"


# --- bias detection from the user's question -------------------------------
_PRODUCT_ALIASES = {
    "GK Gloves": ("glove", "gloves", "gk glove", "gk gloves", "silencer",
                  "gladiator pro"),
    "BodyShield Leggings": ("bodyshield", "leggings", "pants"),
    "CoolCore Leggings": ("coolcore",),
    "ExoShield Head Guard": ("head guard", "headguard", "exoshield", "concussion"),
    "Sliders": ("slider", "sliders", "shorts"),
}
_ICP_ALIASES = {
    "Parents": ("parent", "parents", "mom", "dad", "child"),
    "Aspiring Pro": ("aspiring pro", "aspiring", "pro", "college", "elite"),
    "Adult Amateur": ("amateur", "adult amateur", "hobby", "weekend"),
    "General": ("general",),
}


def _detect_bias(question: str, mapping: dict[str, tuple]) -> str:
    q = (question or "").lower()
    for canonical, aliases in mapping.items():
        for a in aliases:
            if re.search(rf"\b{re.escape(a)}\b", q):
                return canonical
    return ""


def detect_product(question: str) -> str:
    return _detect_bias(question, _PRODUCT_ALIASES)


def detect_icp(question: str) -> str:
    return _detect_bias(question, _ICP_ALIASES)


# --- storytelling helpers --------------------------------------------------
def storytelling_structure(row: dict | None, hook: str, fmt: str,
                           problem: str = "", solution: str = "",
                           funnel: str = "") -> str:
    """Use the row's Storytelling structure if present, else derive one from
    the taxonomy fields available for the idea. No Gemini call."""
    if row:
        existing = str(row.get("Storytelling structure", "") or "").strip()
        if existing:
            return existing
    parts = []
    if hook:
        parts.append(f"{hook} hook")
    if fmt:
        parts.append(f"{fmt} format")
    if problem:
        parts.append(f"{problem} problem")
    if solution:
        parts.append(f"{solution} solution")
    if funnel:
        parts.append(f"{funnel} angle")
    return " → ".join(parts) if parts else "Curiosity Gap hook → Demo format → Prevention solution → Awareness angle"


_HOOK_OPENER = {
    "Curiosity Gap": "a keeper-facing question the viewer wants answered",
    "Fear / Risk": "the moment right before an injury or a bad hop",
    "Aspiration": "the finished, protected, confident version of the keeper",
    "Education": "a specific teaching promise ('the one thing 90% miss…')",
    "Humor": "a relatable goalkeeper fail or reaction",
    "Social Proof": "a coach or higher-level keeper using the gear",
    "Authority": "a credibility cue — pro, coach, or physio speaking",
}

_FMT_BODY = {
    "POV": "first-person, close to the action, natural sound",
    "Tutorial": "step-by-step demonstration, on-screen labels",
    "Do / Don't": "side-by-side of the wrong vs right approach",
    "Story": "problem → attempt → resolution beat structure",
    "Demo": "product-in-context, one clean protective moment",
    "Comparison": "before/after or with/without the product",
    "Reaction": "keeper's real face reacting to a save or a moment",
}


def _story_blocks(hook: str, fmt: str, product: str, solution: str) -> list[str]:
    """3 short beats describing the structure. No metric/link invention."""
    opener = _HOOK_OPENER.get(hook, f"a {hook.lower()} moment")
    body = _FMT_BODY.get(fmt, f"a {fmt.lower()} sequence")
    return [
        f"Open on {opener} — set the stakes in the first 3 seconds.",
        f"Middle: {body}; show {product} solving the {solution.lower()} need in-frame.",
        "Close: soft-follow CTA — no aggressive discount push.",
    ]


def _visual_beats(hook: str, fmt: str, product: str) -> list[str]:
    return [
        f"0–3s: tight shot cued by the {hook} hook; product visible.",
        f"3–12s: {fmt} sequence — {product} in the protective moment.",
        "12–20s: keeper-facing outro, on-screen product name, follow prompt.",
    ]


# --- support: pick the right internal row for evidence citation ------------
def _top_group(rows: list[dict], key: str) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        g = str(r.get(key, "") or "").strip()
        if g and g.lower() != "(unspecified)":
            counts[g] = counts.get(g, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _first_by_layer(win: list[dict], layer: str) -> dict | None:
    for r in win:
        if r["layer"] == layer:
            return r
    return None


def _example_row(rows: list[dict], hook_col: str, fmt_col: str) -> dict | None:
    """Prefer a Great performer carrying both hook+fmt; else any row with them;
    else a row with the hook alone. Rows must be internal (never inspiration)."""
    tier1 = tier2 = tier3 = None
    for r in rows:
        if performance.is_reference_row(r):
            continue
        has_hook = str(r.get(hook_col, "") or "").strip() == "1"
        has_fmt = str(r.get(fmt_col, "") or "").strip() == "1"
        if has_hook and has_fmt:
            if str(r.get("PERFORMANCE", "")).strip().lower() == "great":
                tier1 = tier1 or r
            else:
                tier2 = tier2 or r
        elif has_hook and not tier3:
            tier3 = r
    return tier1 or tier2 or tier3


def _inspiration_row(rows: list[dict], hook_col: str) -> dict | None:
    """One external/inspiration row carrying the hook. Inspiration only —
    never used as evidence."""
    for r in rows:
        if not performance.is_reference_row(r):
            continue
        if str(r.get(hook_col, "") or "").strip() == "1":
            return r
    return None


# --- source registry -------------------------------------------------------
class _SourceRegistry:
    """Global (per-call) source registry. Dedupes by (type,label,url) and
    assigns stable S1/S2/... ids across all ideas in a single call."""

    def __init__(self):
        self._by_key: dict[tuple, dict] = {}
        self._order: list[dict] = []

    def add(self, type_: str, label: str, url: str = "") -> dict:
        key = (type_, label, url)
        if key in self._by_key:
            return self._by_key[key]
        entry = {"id": f"S{len(self._order) + 1}",
                 "type": type_, "label": label, "url": url}
        self._by_key[key] = entry
        self._order.append(entry)
        return entry

    def all(self) -> list[dict]:
        return list(self._order)


# --- public API ------------------------------------------------------------
def build_idea_candidates(question: str, rows: list[dict], findings: list[dict],
                          context: dict, limit: int = 5) -> list[dict]:
    """Generate 3–5 grounded Storelli reel ideas.

    - Uses winning signals from `findings` (already computed over evidence rows).
    - Weak signals surface as an "avoid" line in the why.
    - Product/ICP bias comes from the question if present, else top groups.
    - Only internal rows (`is_reference_row=False`) can be cited as evidence;
      external rows may be cited as inspiration.
    - `context` is `{"learnings": <str>, "guidelines": {name: content}}` from
      content_context.gather_context().
    """
    question = question or ""
    rows = rows or []
    findings = findings or []
    context = context or {}
    limit = max(3, min(int(limit or 5), 5))

    winning = corr.winning(findings)
    weak = corr.weak(findings)
    if not winning and not rows:
        return []

    internal = [r for r in rows if not performance.is_reference_row(r)]

    product_bias = detect_product(question)
    icp_bias = detect_icp(question)
    top_product = product_bias or _top_group(internal, "Product") or "GK Gloves"
    top_icp = icp_bias or _top_group(internal, "ICP") or "General"

    top_hooks = [r for r in winning if r["layer"] == "hook"][:3]
    top_formats = [r for r in winning if r["layer"] == "format"][:3]
    if not top_hooks:
        # No winning hooks yet — fall back to canonical directional pair.
        top_hooks = [{"label": "Curiosity Gap", "layer": "hook", "lift": 0.0,
                      "videos_with_signal": 0, "confidence": "Low"}]
    if not top_formats:
        top_formats = [{"label": "Demo", "layer": "format", "lift": 0.0,
                        "videos_with_signal": 0, "confidence": "Low"}]

    solution = (_first_by_layer(winning, "solution_type") or {}).get("label", "Prevention")
    problem = (_first_by_layer(winning, "problem_type") or {}).get("label", "Chronic Pain")
    funnel = (_first_by_layer(winning, "funnel_stage") or {}).get("label", "Awareness")

    # Build the combo list — favor top hook × top format, then diversify.
    combos: list[tuple[dict, dict]] = []
    seen = set()
    for h in top_hooks:
        for f in top_formats:
            key = (h["label"], f["label"])
            if key in seen:
                continue
            seen.add(key)
            combos.append((h, f))
            if len(combos) >= limit:
                break
        if len(combos) >= limit:
            break

    # Guideline names to reference (bounded so we don't spam sources).
    guideline_names = list((context.get("guidelines") or {}).keys())
    learnings_available = bool(context.get("learnings"))

    reg = _SourceRegistry()
    ideas: list[dict] = []

    weak_avoid = ""
    if weak:
        w = weak[0]
        weak_avoid = (f" Avoid '{w['label']}' ({w['layer']}) — associated with a "
                      f"{corr.fmt_lift(w['lift'])} lift.")

    for h, f in combos:
        hook_col = taxonomy.column_for("hook", h["label"])
        fmt_col = taxonomy.column_for("format", f["label"])
        example = _example_row(internal, hook_col, fmt_col)
        inspo = _inspiration_row(rows, hook_col)

        ss = storytelling_structure(example, h["label"], f["label"],
                                    problem=problem, solution=solution, funnel=funnel)
        story_blocks = _story_blocks(h["label"], f["label"], top_product, solution)
        visual_beats = _visual_beats(h["label"], f["label"], top_product)

        # Sources — only cite what was retrieved.
        idea_sources: list[dict] = []
        if example:
            perf = str(example.get("PERFORMANCE", "") or "?").strip() or "?"
            link = str(example.get("LINK", "") or "").strip()
            label = f"row {example['_row']} — {perf}"
            idea_sources.append(reg.add("sheet_row", label, link))
        if inspo and inspo is not example:
            link = str(inspo.get("LINK", "") or "").strip()
            src_kind = performance.source_type(inspo) or "reference"
            label = f"row {inspo['_row']} — inspiration ({src_kind})"
            idea_sources.append(reg.add("sheet_row", label, link))
        if learnings_available and (h["videos_with_signal"] or f["videos_with_signal"]):
            idea_sources.append(reg.add("learnings",
                                        "data/latest_learnings.md — Winning Patterns"))
        for gname in guideline_names[:2]:
            idea_sources.append(reg.add("guideline", gname))

        # Why — associational only, quotes real lifts if they exist.
        why_parts = []
        if h["videos_with_signal"]:
            why_parts.append(
                f"Based on current analyzed data, '{h['label']}' is associated with a "
                f"{corr.fmt_lift(h['lift'])} lift on the Great rate "
                f"(n={h['videos_with_signal']}, {h['confidence']} confidence).")
        else:
            why_parts.append(
                f"'{h['label']}' is directional here — no reliable lift yet, so treat "
                f"as an inspiration-grade hook.")
        if f["videos_with_signal"]:
            why_parts.append(
                f"'{f['label']}' format is associated with a "
                f"{corr.fmt_lift(f['lift'])} lift (n={f['videos_with_signal']}).")
        if weak_avoid:
            why_parts.append(weak_avoid.strip())
        why = " ".join(why_parts)

        ideas.append({
            "title": f"{f['label']}: {h['label']} on {top_product}",
            "hook": f"Open with {_HOOK_OPENER.get(h['label'], h['label'].lower())} for {top_icp}.",
            "format": f["label"],
            "storytelling_structure": ss,
            "product": top_product,
            "icp": top_icp,
            "story_blocks": story_blocks,
            "visual_beats": visual_beats,
            "why_this_should_work": why,
            "confidence": _idea_confidence(h, f),
            "sources": idea_sources,
        })

    return ideas


def collect_sources(ideas: list[dict]) -> list[dict]:
    """Flatten & dedupe sources across ideas, preserving Sx ordering."""
    seen: dict[str, dict] = {}
    order: list[dict] = []
    for idea in ideas or []:
        for s in idea.get("sources") or []:
            sid = s.get("id")
            if sid and sid not in seen:
                seen[sid] = s
                order.append(s)
    order.sort(key=lambda s: int(re.sub(r"\D", "", s["id"]) or 0))
    return order
