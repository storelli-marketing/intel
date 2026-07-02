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

import json
import os
import time

import config
import correlations as corr
import taxonomy
from logger import get_logger
from synthesizer import MIN_GROUP

log = get_logger()

# Backoff for transient Notion 5xx / rate-limit errors during a multi-call sync.
NOTION_RETRY_DELAYS = (1, 3, 6)


def _is_transient(exc: Exception) -> bool:
    code = getattr(exc, "status", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if code in (429, 502, 503, 504):
        return True
    s = str(exc)
    return any(x in s for x in ("429", "502", "503", "504", "Bad Gateway",
                                "Service Unavailable", "Gateway Timeout", "rate_limited"))

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
    "Generated Social Ideas": {
        "key": "Title",
        "properties": {
            "Title": "title", "Channel": "rich_text",
            "Product": "rich_text", "ICP": "rich_text",
            "Hook": "rich_text", "Format": "rich_text",
            "Storytelling Structure": "rich_text",
            "Story Blocks": "rich_text", "Visual Beats": "rich_text",
            "Why This Should Work": "rich_text",
            "Confidence": "select", "Sources": "rich_text",
            "Status": "select", "Created At": "date",
            "Posted URL": "rich_text", "Result": "rich_text",
            "Feedback": "rich_text",
        },
        # Fields the operator edits by hand after an idea ships. On update we
        # do NOT overwrite these, so the human-added values survive resyncs.
        "preserve_on_update": ("Status", "Posted URL", "Result", "Feedback"),
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


GENERATED_IDEAS_JSONL = os.path.join(os.path.dirname(__file__), "..", "data",
                                     "generated_social_ideas.jsonl")


def _source_line(s: dict) -> str:
    """One-liner source rendering safe for Notion rich_text (2000-char cap
    applies at write-time in _to_props)."""
    parts = [f"[{s.get('id', '?')}]", s.get("type", ""), s.get("label", "")]
    if s.get("url"):
        parts.append(s["url"])
    return " ".join(p for p in parts if p).strip()


def generated_ideas_entries(ideas: list[dict], date_str: str) -> list[dict]:
    """Pure: map interpretation ideas -> Notion Generated Social Ideas rows.

    Status/Channel are defaulted (Proposed / Instagram). Posted URL, Result,
    and Feedback stay blank on first-create and are preserved on update
    (see SCHEMAS['Generated Social Ideas']['preserve_on_update']).
    """
    out = []
    for idea in ideas or []:
        srcs = idea.get("sources") or []
        out.append({
            "Title": str(idea.get("title", "")),
            "Channel": "Instagram",
            "Product": str(idea.get("product", "")),
            "ICP": str(idea.get("icp", "")),
            "Hook": str(idea.get("hook", "")),
            "Format": str(idea.get("format", "")),
            "Storytelling Structure": str(idea.get("storytelling_structure", "")),
            "Story Blocks": "\n".join(idea.get("story_blocks") or []),
            "Visual Beats": "\n".join(idea.get("visual_beats") or []),
            "Why This Should Work": str(idea.get("why_this_should_work", "")),
            "Confidence": str(idea.get("confidence", "Directional")),
            "Sources": "\n".join(_source_line(s) for s in srcs),
            "Status": "Proposed",
            "Created At": date_str,
            "Posted URL": "",
            "Result": "",
            "Feedback": "",
        })
    return out


def _persist_ideas_jsonl(ideas: list[dict], date_str: str) -> str:
    """Fallback when Notion is unavailable — one JSON line per idea.
    Idempotent by (date_str, title) — repeated calls append; the caller can
    dedupe downstream if needed. Returns the file path."""
    os.makedirs(os.path.dirname(GENERATED_IDEAS_JSONL), exist_ok=True)
    with open(GENERATED_IDEAS_JSONL, "a", encoding="utf-8") as f:
        for idea in ideas or []:
            record = dict(idea)
            record["created_at"] = date_str
            record.setdefault("status", "Proposed")
            record.setdefault("channel", "Instagram")
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return GENERATED_IDEAS_JSONL


def sync_or_persist_ideas(ideas: list[dict], date_str: str) -> dict:
    """Try Notion first; on missing config or any Notion failure, fall back
    to `data/generated_social_ideas.jsonl`. Never raises to the caller —
    always returns a summary dict describing what happened.
    """
    if not ideas:
        return {"ideas": 0, "target": "none", "note": "no ideas to persist"}
    if not (config.NOTION_API_KEY and config.NOTION_PARENT_PAGE_ID):
        path = _persist_ideas_jsonl(ideas, date_str)
        return {"ideas": len(ideas), "target": "jsonl", "path": path,
                "note": "Notion not configured — wrote jsonl fallback"}
    try:
        summary = NotionBrain().sync_generated_ideas(ideas, date_str)
        summary.update({"ideas": len(ideas), "target": "notion"})
        return summary
    except Exception as e:  # noqa: BLE001 - never crash on a Notion failure here
        log.warning("Notion generated-ideas sync failed (%s); writing jsonl fallback.", e)
        path = _persist_ideas_jsonl(ideas, date_str)
        return {"ideas": len(ideas), "target": "jsonl", "path": path,
                "note": f"Notion failed ({type(e).__name__}); wrote jsonl fallback"}


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

    def _call(self, fn, *args, **kwargs):
        """Invoke a Notion SDK call, retrying transient 5xx / rate-limit errors."""
        attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                if _is_transient(e) and attempt < len(NOTION_RETRY_DELAYS):
                    delay = NOTION_RETRY_DELAYS[attempt]
                    attempt += 1
                    log.warning("Notion transient error; retry %d/%d in %ds: %s",
                                attempt, len(NOTION_RETRY_DELAYS), delay, e)
                    time.sleep(delay)
                    continue
                raise

    # -- database discovery / creation --
    def _find_child_databases(self) -> dict:
        out, cursor = {}, None
        while True:
            kw = {"block_id": self.parent}
            if cursor:
                kw["start_cursor"] = cursor
            res = self._call(self.client.blocks.children.list, **kw)
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
        db = self._call(
            self.client.databases.create,
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
        res = self._call(
            self.client.databases.query,
            database_id=db_id,
            filter={"property": key_prop, "title": {"equals": key_val}},
        )
        r = res.get("results", [])
        return r[0]["id"] if r else None

    def _upsert(self, title: str, db_id: str, entries: list[dict]) -> dict:
        key = SCHEMAS[title]["key"]
        preserve = set(SCHEMAS[title].get("preserve_on_update", ()))
        created = updated = 0
        for e in entries:
            props = self._to_props(title, e)
            pid = self._find_page(db_id, key, str(e[key]))
            if pid:
                update_props = {k: v for k, v in props.items() if k not in preserve}
                self._call(self.client.pages.update, page_id=pid, properties=update_props)
                updated += 1
            else:
                self._call(self.client.pages.create,
                           parent={"database_id": db_id}, properties=props)
                created += 1
        return {"created": created, "updated": updated}

    # Existing five-database sync — kept intact. The 6th DB (Generated Social
    # Ideas) is synced separately via `sync_generated_ideas()` so existing
    # `notion-sync` runs don't touch it.
    _SYNTHESIS_DBS = ("Marketing Learnings", "Signal Library", "Next Creative Tests",
                      "Product Learnings", "ICP Learnings")

    def sync(self, synthesis: dict, date_str: str) -> dict:
        entries = build_entries(synthesis, date_str)
        existing = self._find_child_databases()
        summary = {}
        for title in self._SYNTHESIS_DBS:
            db_id = self._ensure_db(title, existing)
            summary[title] = self._upsert(title, db_id, entries.get(title, []))
        log.info("Notion Brain sync summary: %s", summary)
        return summary

    def sync_generated_ideas(self, ideas: list[dict], date_str: str) -> dict:
        """Upsert generated social ideas into the 'Generated Social Ideas' DB.

        Key = Title. Existing rows with the same title are updated in place,
        so re-runs are idempotent. Rows created by this function default to
        Status='Proposed' / Channel='Instagram' and never overwrite the
        Posted URL / Result / Feedback fields on update.
        """
        entries = generated_ideas_entries(ideas, date_str)
        existing = self._find_child_databases()
        db_id = self._ensure_db("Generated Social Ideas", existing)
        summary = self._upsert("Generated Social Ideas", db_id, entries)
        log.info("Notion Generated Social Ideas sync: %s", summary)
        return {"Generated Social Ideas": summary}
