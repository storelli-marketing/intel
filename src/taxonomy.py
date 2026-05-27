"""Canonical Storelli signal taxonomy + column-name helpers.

Nine AI-tagged layers. Hook / Format / Visual Style are multi-label (0+ tags).
The remaining six are single-label (exactly one value each).

ICP and Product are NOT AI-tagged layers — they are human-provided raw columns
used as grouping dimensions for ICP/Product learnings. Their canonical
vocabularies live here (ICP / PRODUCT) for validation and reporting.

Signal column convention:  signal_<layer>_<slug>
e.g. "Curiosity Gap" in hook -> signal_hook_curiosity_gap
"""
import re

HOOK = [
    "Curiosity Gap",
    "Fear / Risk",
    "Aspiration",
    "Education",
    "Humor",
    "Social Proof",
    "Authority",
]

FORMAT = [
    "POV",
    "Tutorial",
    "Do / Don't",
    "Story",
    "Demo",
    "Comparison",
    "Reaction",
]

VISUAL_STYLE = [
    "Raw / UGC",
    "Polished",
    "Action",
    "Talking Head",
]

PROBLEM_TYPE = [
    "Acute Pain",
    "Chronic Pain",
    "Latent",
]

SOLUTION_TYPE = [
    "Fix",
    "Prevention",
    "Enhancement",
]

CONVERSION = [
    "Direct Purchase",
    "Learn More",
    "Soft / Follow",
    "None",
]

OFFER = [
    "Discount",
    "Bundle",
    "Free Shipping",
    "No Offer",
]

PRODUCT_PRESENCE = [
    "None",
    "Soft",
    "Hard Focus",
]

FUNNEL_STAGE = [
    "Awareness",
    "Consideration",
    "Conversion",
    "Retention",
]

# Grouping dimensions (human-provided raw columns, not AI signal layers).
ICP = [
    "Parents",
    "Aspiring Pro",
    "Adult Amateur",
    "General",
]

PRODUCT = [
    "CoolCore Leggings",
    "BodyShield Leggings",
    "ExoShield Head Guard",
    "GK Gloves",
    "Sliders",
]

LAYERS = {
    "hook": HOOK,
    "format": FORMAT,
    "visual_style": VISUAL_STYLE,
    "problem_type": PROBLEM_TYPE,
    "solution_type": SOLUTION_TYPE,
    "conversion": CONVERSION,
    "offer": OFFER,
    "product_presence": PRODUCT_PRESENCE,
    "funnel_stage": FUNNEL_STAGE,
}

MULTI_LABEL_LAYERS = ("hook", "format", "visual_style")
SINGLE_LABEL_LAYERS = (
    "problem_type",
    "solution_type",
    "conversion",
    "offer",
    "product_presence",
    "funnel_stage",
)


def slug(value: str) -> str:
    """'Fear / Risk' -> 'fear_risk', 'Do / Don't' -> 'do_dont'."""
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
OUTPUT_META_COLUMNS = (
    ["ai_summary"]
    + [f"primary_{layer}" for layer in LAYERS]
    + ["performance_bucket", "processed_status", "processed_at"]
)


def all_output_columns() -> list[str]:
    return all_signal_columns() + OUTPUT_META_COLUMNS
