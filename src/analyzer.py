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


def render_taxonomy() -> str:
    """Render the taxonomy as an instruction block for the prompt."""
    blocks = []
    for layer, values in taxonomy.LAYERS.items():
        title = layer.replace("_", " ").upper()
        if layer in taxonomy.MULTI_LABEL_LAYERS:
            rule = "tag ALL that apply, list the most dominant first"
        else:
            rule = "choose EXACTLY ONE"
        opts = "\n".join(f"- {v}" for v in values)
        blocks.append(f"### {title} ({rule})\n{opts}")
    return "\n\n".join(blocks)


def parse_model_json(text: str) -> dict:
    """Extract a JSON object from model text (tolerates ```json fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
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


def _as_list(value) -> list:
    """Normalize a layer value (string or list) into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def to_signal_columns(parsed: dict) -> dict:
    """Turn parsed JSON into {column: 0/1} + primary_<layer> + suggestions.

    primary_*, ai_summary, icp_suggested and product_suggested are internal
    (not written to the POC sheet's taxonomy columns) but are used for the
    QA pass and the fill-if-blank ICP/Product logic.
    """
    cols = {c: 0 for c in taxonomy.all_signal_columns()}
    meta = {
        "ai_summary": (parsed.get("summary") or "").strip(),
        "icp_suggested": (parsed.get("icp_suggested") or "").strip(),
        "product_suggested": (parsed.get("product_suggested") or "").strip(),
    }

    for layer in taxonomy.LAYERS:
        raw_values = _as_list(parsed.get(layer))
        single = layer in taxonomy.SINGLE_LABEL_LAYERS
        primary = ""
        for raw in raw_values:
            canonical = _match_label(layer, raw)
            if not canonical:
                log.warning("Unknown %s label from model: %r", layer, raw)
                continue
            cols[taxonomy.column_for(layer, canonical)] = 1
            if not primary:
                primary = canonical
            if single:
                break  # single-label layers keep only the first valid value
        meta[f"primary_{layer}"] = primary

    return {**cols, **meta}


def _parse_with_retry(produce_text, label: str) -> dict:
    """Call produce_text() (a 0-arg fn returning model text), parse JSON,
    retry once on failure."""
    last_err = None
    for attempt in (1, 2):
        text = produce_text()
        try:
            return parse_model_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            log.warning("Invalid JSON from %s (attempt %d): %s", label, attempt, e)
    raise RuntimeError(f"{label} returned invalid JSON twice: {last_err}")


def analyze_and_compile(gemini, ig_link: str, product: str, icp: str,
                        notes: str) -> dict:
    """Pass 1 (video analysis) -> QA compiler pass -> final signal columns.

    Each Gemini call retries once on invalid JSON. Raises on download or
    persistent failure so the caller can mark the row failed.
    """
    taxonomy_block = render_taxonomy()

    parsed1 = _parse_with_retry(
        lambda: gemini.analyze(ig_link, taxonomy_block, product, icp, notes),
        "Gemini analysis",
    )

    initial_json = json.dumps(parsed1, ensure_ascii=False)
    parsed2 = _parse_with_retry(
        lambda: gemini.qa_review(initial_json, taxonomy_block, product, icp, notes),
        "QA compiler",
    )

    return to_signal_columns(parsed2)
