"""Per-video analysis orchestration.

Parses Gemini JSON, retries once on invalid JSON, and maps the structured
result into the flat set of 1/0 signal columns + AI meta columns.
"""
from __future__ import annotations

import json
import re

import taxonomy
from logger import get_logger

log = get_logger()


def _layers_text() -> dict:
    """Render the taxonomy as bullet lists for the prompt."""
    out = {}
    for layer, values in taxonomy.LAYERS.items():
        out[layer] = "\n".join(f"- {v}" for v in values)
    return out


def parse_model_json(text: str) -> dict:
    """Extract a JSON object from model text (tolerates ```json fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        # fall back to first {...} block
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


def _match_label(layer: str, value: str) -> str | None:
    """Map a model-returned string to the canonical taxonomy label."""
    if not value:
        return None
    target = taxonomy.slug(value)
    for canonical in taxonomy.LAYERS[layer]:
        if taxonomy.slug(canonical) == target:
            return canonical
    return None


def to_signal_columns(parsed: dict) -> dict:
    """Turn parsed JSON into {column: 0/1} + meta fields."""
    cols = {c: 0 for c in taxonomy.all_signal_columns()}

    # multi-label layers
    for layer in taxonomy.MULTI_LABEL_LAYERS:
        for raw in parsed.get(layer, []) or []:
            canonical = _match_label(layer, raw)
            if canonical:
                cols[taxonomy.column_for(layer, canonical)] = 1
            else:
                log.warning("Unknown %s label from model: %r", layer, raw)

    # single-label primitive
    prim_raw = parsed.get("primitive")
    prim = _match_label("primitive", prim_raw) if prim_raw else None
    if prim:
        cols[taxonomy.column_for("primitive", prim)] = 1
    elif prim_raw:
        log.warning("Unknown primitive from model: %r", prim_raw)

    meta = {
        "ai_summary": (parsed.get("summary") or "").strip(),
        "primary_delivery": _match_label("delivery", parsed.get("primary_delivery", "")) or "",
        "primary_hook": _match_label("hook", parsed.get("primary_hook", "")) or "",
        "primary_primitive": prim or "",
    }
    return {**cols, **meta}


def analyze_video(gemini, ig_link: str, product: str, icp: str, notes: str) -> dict:
    """Run Gemini analysis with one retry on invalid JSON.

    Returns the signal/meta column dict. Raises on download or persistent
    failure so the caller can mark the row failed.
    """
    layers = _layers_text()
    last_err = None
    for attempt in (1, 2):
        text = gemini.analyze(ig_link, layers, product, icp, notes)
        try:
            parsed = parse_model_json(text)
            return to_signal_columns(parsed)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            log.warning("Invalid JSON from Gemini (attempt %d): %s", attempt, e)
    raise RuntimeError(f"Gemini returned invalid JSON twice: {last_err}")
