"""Content-generation context (structure only — generation is NOT built yet).

Future content/email/ad generation will draw on three sources:
  1. data/latest_learnings.md     (synthesized signal/pattern intelligence)
  2. data/guidelines/*.md         (operator-uploaded brand/content guidelines)
  3. The Notion Brain databases   (queryable via notion_brain.NotionBrain)

gather_context() bundles the local file sources so a future generator can use
them. Notion is left as a live query for whoever builds generation.
"""
from __future__ import annotations

import os

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_LEARNINGS = os.path.join(_DATA, "latest_learnings.md")
_GUIDELINES_DIR = os.path.join(_DATA, "guidelines")


def gather_context() -> dict:
    """Return {learnings: str, guidelines: {type: content}}. Empty/missing
    sources resolve to '' / {} rather than raising."""
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

    return {"learnings": learnings, "guidelines": guidelines}
