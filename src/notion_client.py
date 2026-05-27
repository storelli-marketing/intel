"""Notion findings dashboard.

Each run creates a dated child page under NOTION_PARENT_PAGE_ID with five
sections rendered as Notion tables. Snapshot-per-run keeps it simple and
avoids fragile database-row reconciliation (MVP scope).
"""
import os
import sys
from datetime import datetime, timezone

import config
from logger import get_logger

log = get_logger()


def _load_notion_sdk():
    """Import the installed `notion-client` SDK.

    This file is itself named notion_client.py and sits on sys.path[0] when
    running `python src/main.py`, so a plain `import notion_client` would
    resolve to THIS module. We temporarily drop our own directory from
    sys.path (and the cached self-module) so the real PyPI package wins.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    saved_path = sys.path[:]
    saved_self = sys.modules.pop("notion_client", None)
    try:
        sys.path = [p for p in sys.path if os.path.abspath(p or ".") != here]
        import notion_client as sdk  # resolves to site-packages now
        return sdk.Client
    finally:
        sys.path = saved_path
        if saved_self is not None:
            sys.modules["notion_client"] = saved_self


NotionSDK = _load_notion_sdk()

MAX_CELL = 2000


def _text(s) -> list:
    s = "" if s is None else str(s)
    return [{"type": "text", "text": {"content": s[:MAX_CELL]}}]


def _heading(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _text(text)},
    }


def _table(headers: list[str], rows: list[list[str]]) -> dict:
    width = len(headers)
    children = [{
        "type": "table_row",
        "table_row": {"cells": [_text(h) for h in headers]},
    }]
    for r in rows:
        cells = list(r) + [""] * (width - len(r))
        children.append({
            "type": "table_row",
            "table_row": {"cells": [_text(c) for c in cells[:width]]},
        })
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        },
    }


def _para(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _text(text)},
    }


class NotionDashboard:
    def __init__(self):
        config.require_notion()
        self.client = NotionSDK(auth=config.NOTION_API_KEY)
        self.parent = config.NOTION_PARENT_PAGE_ID

    def publish(self, findings: dict) -> str:
        """findings keys: winning_signals, weak_signals, icp_learnings,
        product_learnings, next_creative_tests. Returns the new page URL."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        blocks = []

        blocks.append(_para(
            "Associations below are correlations, not causation. "
            f"Generated {ts}."
        ))

        blocks.append(_heading("Winning Signals"))
        blocks.append(_table(
            ["Signal", "Layer", "Finding", "Lift", "Sample Size", "Confidence", "Recommended Action"],
            [[s.get("signal", ""), s.get("layer", ""), s.get("finding", ""),
              s.get("lift", ""), str(s.get("sample_size", "")), s.get("confidence", ""),
              s.get("recommended_action", "")] for s in findings.get("winning_signals", [])],
        ))

        blocks.append(_heading("Weak Signals"))
        blocks.append(_table(
            ["Signal", "Layer", "Finding", "Lift", "Sample Size", "Confidence", "Recommended Action"],
            [[s.get("signal", ""), s.get("layer", ""), s.get("finding", ""),
              s.get("lift", ""), str(s.get("sample_size", "")), s.get("confidence", ""),
              s.get("recommended_action", "")] for s in findings.get("weak_signals", [])],
        ))

        blocks.append(_heading("ICP Learnings"))
        blocks.append(_table(
            ["ICP", "Finding", "Supporting Signals", "Recommended Content Direction"],
            [[s.get("icp", ""), s.get("finding", ""), s.get("supporting_signals", ""),
              s.get("recommended_content_direction", "")] for s in findings.get("icp_learnings", [])],
        ))

        blocks.append(_heading("Product Learnings"))
        blocks.append(_table(
            ["Product", "Finding", "Supporting Signals", "Recommended Content Direction"],
            [[s.get("product", ""), s.get("finding", ""), s.get("supporting_signals", ""),
              s.get("recommended_content_direction", "")] for s in findings.get("product_learnings", [])],
        ))

        blocks.append(_heading("Next Creative Tests"))
        blocks.append(_table(
            ["Hypothesis", "ICP", "Product", "Delivery", "Hook", "Primitive", "Suggested Video Idea"],
            [[s.get("hypothesis", ""), s.get("icp", ""), s.get("product", ""),
              s.get("delivery", ""), s.get("hook", ""), s.get("primitive", ""),
              s.get("suggested_video_idea", "")] for s in findings.get("next_creative_tests", [])],
        ))

        page = self.client.pages.create(
            parent={"type": "page_id", "page_id": self.parent},
            properties={"title": [{"type": "text", "text": {
                "content": f"Storelli Intelligence — Findings {ts}"}}]},
            children=blocks,
        )
        return page.get("url", "")
