"""Canonical signal taxonomy + column-name helpers.

Four layers. Delivery / Hook / Context are multi-label (0+ tags each).
Primitive is single-label (exactly one primary value).

Signal column convention:  signal_<layer>_<slug>
e.g. "UGC" in delivery -> signal_delivery_ugc
"""
import re

DELIVERY = [
    "UGC",
    "Product Demo",
    "Tutorial",
    "Talking Head",
    "POV",
    "Storytelling",
    "Before/After",
    "Slow Motion",
    "Fast Paced",
    "Close-Up",
    "Voiceover",
    "Captions",
    "Jump Cuts",
]

HOOK = [
    "Curiosity Gap",
    "Fear Framing",
    "Negative Framework",
    "Positive Promise",
    "Social Proof",
    "Surprise",
    "Pattern Interrupt",
    "Problem/Solution",
    "Performance Claim",
    "Injury Risk",
    "Relatable Pain",
]

PRIMITIVE = [
    "Protection / Safety",
    "Confidence",
    "Performance Improvement",
    "Fear Avoidance",
    "Achievement / Mastery",
    "Competitive Edge",
    "Identity / Belonging",
    "Status",
    "Trust / Authority",
    "Aspiration",
    "Relief / Security",
    "Transformation",
    "Discipline / Hard Work",
    "Curiosity / Discovery",
    "Entertainment / Dopamine",
    "Validation / Recognition",
    "Self-Expression",
    "Excitement / Adventure",
    "Parental Anxiety",
]

CONTEXT = [
    "Diving Save",
    "1v1",
    "Shot Stopping",
    "Turf Impact",
    "Collision",
    "Landing Impact",
    "Training Session",
    "Match Situation",
    "GK Leggings",
    "Gloves",
    "Head Protection",
    "Elbow Protection",
    "Knee Protection",
    "Hip Protection",
    "Parent Buyer",
    "Teen Goalkeeper",
    "Competitive Goalkeeper",
    "Coach Buyer",
]

LAYERS = {
    "delivery": DELIVERY,
    "hook": HOOK,
    "primitive": PRIMITIVE,
    "context": CONTEXT,
}

MULTI_LABEL_LAYERS = ("delivery", "hook", "context")
SINGLE_LABEL_LAYERS = ("primitive",)


def slug(value: str) -> str:
    """'Before/After' -> 'before_after', '1v1' -> '1v1'."""
    s = value.lower()
    s = re.sub(r"[/\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def column_for(layer: str, value: str) -> str:
    return f"signal_{layer}_{slug(value)}"


def all_signal_columns() -> list[str]:
    cols = []
    for layer, values in LAYERS.items():
        for v in values:
            cols.append(column_for(layer, v))
    return cols


# Map signal column -> (layer, human label) for reporting.
def signal_index() -> dict[str, dict[str, str]]:
    idx = {}
    for layer, values in LAYERS.items():
        for v in values:
            idx[column_for(layer, v)] = {"layer": layer, "label": v}
    return idx


# Output (AI-written, non-signal) columns. Used as a guard so we never
# clobber raw user metrics — see sheets_client.RAW_COLUMNS.
OUTPUT_META_COLUMNS = [
    "ai_summary",
    "primary_delivery",
    "primary_hook",
    "primary_primitive",
    "performance_bucket",
    "processed_status",
    "processed_at",
]


def all_output_columns() -> list[str]:
    return all_signal_columns() + OUTPUT_META_COLUMNS
