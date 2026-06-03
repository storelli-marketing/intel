"""Notion Brain sync.

Pushes the *synthesized* intelligence (never raw video rows) into five Notion
databases under NOTION_PARENT_PAGE_ID, upserting by a title key so re-syncs
update rows in place:

  Marketing Learnings · Signal Library · Next Creative Tests ·
  Product Learnings · ICP Learnings

build_entries() is a pure function (testable offline). NotionBrain performs the
live find-or-create + upsert. No local state/DB: databases are located by title
among the parent page's children on each run.
"""
from __future__ import annotations

import config
import correlations as corr
import taxonomy
from logger import get_logger
from synthesizer import MIN_GROUP

log = get_logger()

# Each database: ordered property schema (name -> Notion type) + title key.
SCHEMAS: dict[str, dict] = {
    "Marketing Learnings": {
        "key": "Title",
        "properties": {
            "Title": "title", "Channel": "rich_text", "ICP": "rich_text",
            "Product": "rich_text", "Funnel Stage": "rich_text", "Signal": "rich_text",
            "Layer": "rich_text", "Finding": "rich_text", "Confidence": "select",
            "Sample Size": "number", "Recommended Action": "rich_text",
            "Status": "select", "Date Generated": "date",
        },
    },
    "Signal Library": {
        "key": "Signal Name",
        "properties": {
            "Signal Name": "title", "Layer": "rich_text", "Works Best For": "rich_text",
            "Weak For": "rich_text", "Evidence Count": "number", "Confidence": "select",
            "Last Updated": "date",
        },
    },
    "Next Creative Tests": {
        "key": "Hypothesis",
        "properties": {
            "Hypothesis": "title", "Product": "rich_text", "ICP": "rich_text",
            "Channel": "rich_text", "Hook": "rich_text", "Format": "rich_text",
            "Problem Type": "rich_text", "Solution Type": "rich_text",
            "Priority": "select", "Status": "select", "Result": "rich_text",
        },
    },
    "Product Learnings": {
        "key": "Product",
        "properties": {
            "Product": "title", "Best Hooks": "rich_text", "Best Formats": "rich_text",
            "Weak Angles": "rich_text", "Next Direction": "rich_text",
            "Confidence": "select", "Last Updated": "date",
        },
    },
    "ICP Learnings": {
        "key": "ICP",
        "properties": {
            "ICP": "title", "Best Hooks": "rich_text", "Best Formats": "rich_text",
            "Core Motivation": "rich_text", "Recommended Messaging": "rich_text",
            "Confidence": "select", "Last Updated": "date",
        },
    },
}

# Static, brand-informed motivation hints per ICP (qualitative scaffolding).
_MOTIVATION = {
    "Parents": "Child safety and injury prevention.",
    "Aspiring Pro": "Performance edge and looking the part at the next level.",
    "Adult Amateur": "Confidence, comfort and avoiding nagging knocks.",
    "General": "Broad awareness of goalkeeper protection.",
}


# ---- pure helpers ----------------------------------------------------------
def _finding(r: dict, positive: bool) -> str:
    verb = "associated with a higher" if positive else "associated with a lower"
    return (f"'{r['label']}' ({r['layer']}) is {verb} Great rate "
            f"({corr.fmt_pct(r['high_rate_with'])} with vs "
            f"{corr.fmt_pct(r['high_rate_without'])} without).")


def _action(r: dict, positive: bool) -> str:
    if positive:
        return f"Test more {r['layer']} content using '{r['label']}'."
    return f"De-prioritize '{r['label']}' until more data confirms."


def _top_in_layer(rows: list[dict], layer: str, top: int = 2) -> list[str]:
    counts = {}
    for v in taxonomy.LAYERS[layer]:
        n = sum(1 for r in rows if str(r.get(taxonomy.column_for(layer, v), "")).strip() == "1")
        if n:
            counts[v] = n
    return [k for k, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top]]


def _group_conf(d: dict) -> str:
    n = len(d["rows"])
    return "High" if n >= 20 else "Medium" if n >= 8 else "Low"


def build_entries(s: dict, date_str: str) -> dict:
    """Pure: turn a synthesis dict into {db_title: [plain entry dicts]}."""
    win, weak = s["winning"], s["weak"]

    marketing, signals = [], []
    for r in win + weak:
        positive = r["lift"] > 0
        marketing.append({
            "Title": f"{r['label']} — {r['layer']}", "Channel": "Instagram",
            "ICP": "", "Product": "", "Funnel Stage": "",
            "Signal": r["label"], "Layer": r["layer"],
            "Finding": _finding(r, positive), "Confidence": r["confidence"],
            "Sample Size": r["videos_with_signal"],
            "Recommended Action": _action(r, positive),
            "Status": "New", "Date Generated": date_str,
        })
        signals.append({
            "Signal Name": r["label"], "Layer": r["layer"],
            "Works Best For": "Great performers" if positive else "",
            "Weak For": "Great performers" if not positive else "",
            "Evidence Count": r["videos_with_signal"], "Confidence": r["confidence"],
            "Last Updated": date_str,
        })

    tests = [{
        "Hypothesis": t["hypothesis"], "Product": t["product"], "ICP": t["icp"],
        "Channel": "Instagram", "Hook": t["hook"], "Format": t["format"],
        "Problem Type": t.get("problem_type", ""), "Solution Type": t.get("solution_type", ""),
        "Priority": t.get("priority", "Medium"), "Status": "Proposed", "Result": "",
    } for t in s["tests"]]

    products = []
    for name, d in s["products"].items():
        if name == "(unspecified)" or len(d["rows"]) < MIN_GROUP:
            continue
        rate = corr.fmt_pct(d["great"] / len(d["rows"]))
        products.append({
            "Product": name,
            "Best Hooks": ", ".join(_top_in_layer(d["rows"], "hook")) or "n/a",
            "Best Formats": ", ".join(_top_in_layer(d["rows"], "format")) or "n/a",
            "Weak Angles": "", "Next Direction": f"{rate} Great over {len(d['rows'])} videos.",
            "Confidence": _group_conf(d), "Last Updated": date_str,
        })

    icps = []
    for name, d in s["icps"].items():
        if name == "(unspecified)" or len(d["rows"]) < MIN_GROUP:
            continue
        icps.append({
            "ICP": name,
            "Best Hooks": ", ".join(_top_in_layer(d["rows"], "hook")) or "n/a",
            "Best Formats": ", ".join(_top_in_layer(d["rows"], "format")) or "n/a",
            "Core Motivation": _MOTIVATION.get(name, ""),
            "Recommended Messaging": f"Lead with {', '.join(_top_in_layer(d['rows'], 'hook')) or 'top hooks'}.",
            "Confidence": _group_conf(d), "Last Updated": date_str,
        })

    return {
        "Marketing Learnings": marketing, "Signal Library": signals,
        "Next Creative Tests": tests, "Product Learnings": products,
        "ICP Learnings": icps,
    }


# ---- live Notion client ----------------------------------------------------
def _schema_properties(title: str) -> dict:
    out = {}
    for name, typ in SCHEMAS[title]["properties"].items():
        out[name] = {typ: {}}
    return out


class NotionBrain:
    def __init__(self):
        config.require_notion()
        from notion_client import NotionSDK  # local notion_client.py loads the real SDK
        self.client = NotionSDK(auth=config.NOTION_API_KEY)
        self.parent = config.NOTION_PARENT_PAGE_ID

    # -- database discovery / creation --
    def _find_child_databases(self) -> dict:
        out, cursor = {}, None
        while True:
            kw = {"block_id": self.parent}
            if cursor:
                kw["start_cursor"] = cursor
            res = self.client.blocks.children.list(**kw)
            for b in res.get("results", []):
                if b.get("type") == "child_database":
                    title = b["child_database"].get("title", "")
                    out[title] = b["id"]
            if res.get("has_more"):
                cursor = res.get("next_cursor")
            else:
                break
        return out

    def _ensure_db(self, title: str, existing: dict) -> str:
        if title in existing:
            return existing[title]
        db = self.client.databases.create(
            parent={"type": "page_id", "page_id": self.parent},
            title=[{"type": "text", "text": {"content": title}}],
            properties=_schema_properties(title),
        )
        log.info("Created Notion database '%s'", title)
        return db["id"]

    # -- property payloads --
    def _to_props(self, title: str, entry: dict) -> dict:
        props = {}
        for name, typ in SCHEMAS[title]["properties"].items():
            val = entry.get(name, "")
            if typ == "title":
                props[name] = {"title": [{"text": {"content": str(val)[:2000]}}]}
            elif typ == "rich_text":
                props[name] = {"rich_text": [{"text": {"content": str(val)[:2000]}}]} if str(val) else {"rich_text": []}
            elif typ == "number":
                props[name] = {"number": (val if isinstance(val, (int, float)) else None)}
            elif typ == "select":
                props[name] = {"select": {"name": str(val)[:100]}} if str(val) else {"select": None}
            elif typ == "date":
                props[name] = {"date": {"start": str(val)}} if str(val) else {"date": None}
        return props

    def _find_page(self, db_id: str, key_prop: str, key_val: str):
        res = self.client.databases.query(
            database_id=db_id,
            filter={"property": key_prop, "title": {"equals": key_val}},
        )
        r = res.get("results", [])
        return r[0]["id"] if r else None

    def _upsert(self, title: str, db_id: str, entries: list[dict]) -> dict:
        key = SCHEMAS[title]["key"]
        created = updated = 0
        for e in entries:
            props = self._to_props(title, e)
            pid = self._find_page(db_id, key, str(e[key]))
            if pid:
                self.client.pages.update(page_id=pid, properties=props)
                updated += 1
            else:
                self.client.pages.create(parent={"database_id": db_id}, properties=props)
                created += 1
        return {"created": created, "updated": updated}

    def sync(self, synthesis: dict, date_str: str) -> dict:
        entries = build_entries(synthesis, date_str)
        existing = self._find_child_databases()
        summary = {}
        for title in SCHEMAS:
            db_id = self._ensure_db(title, existing)
            summary[title] = self._upsert(title, db_id, entries.get(title, []))
        log.info("Notion Brain sync summary: %s", summary)
        return summary
