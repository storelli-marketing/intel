"""Content-generation context (structure only — generation is NOT built yet).

Future content/email/ad generation will draw on four sources:
  1. data/storelli_context.md     (brand/strategy grounding — see below)
  2. data/latest_learnings.md     (synthesized signal/pattern intelligence)
  3. data/guidelines/*.md         (operator-uploaded brand/content guidelines)
  4. The Notion Brain databases   (queryable via notion_brain.NotionBrain)

gather_context() bundles the local file sources so a future generator (and
the Slack strategist, see social_strategist.py) can use them. Notion is left
as a live query for whoever builds generation.
"""
from __future__ import annotations

import os

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_BRAND_CONTEXT = os.path.join(_DATA, "storelli_context.md")
_LEARNINGS = os.path.join(_DATA, "latest_learnings.md")
_GUIDELINES_DIR = os.path.join(_DATA, "guidelines")


def gather_context() -> dict:
    """Return {brand_context: str, learnings: str, guidelines: {type: content}}.
    Empty/missing sources resolve to '' / {} rather than raising."""
    brand_context = ""
    if os.path.exists(_BRAND_CONTEXT):
        try:
            with open(_BRAND_CONTEXT, encoding="utf-8") as f:
                brand_context = f.read()
        except OSError:
            brand_context = ""

    learnings = ""
    if os.path.exists(_LEARNINGS):
        with open(_LEARNINGS, encoding="utf-8") as f:
            learnings = f.read()

    guidelines = {}
    if os.path.isdir(_GUIDELINES_DIR):
        for fn in sorted(os.listdir(_GUIDELINES_DIR)):
            if fn.endswith(".md"):
                with open(os.path.join(_GUIDELINES_DIR, fn), encoding="utf-8") as f:
                    guidelines[fn] = f.read()

    return {"brand_context": brand_context, "learnings": learnings, "guidelines": guidelines}
