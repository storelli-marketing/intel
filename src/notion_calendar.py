"""Read-only reader for the Storelli Notion Content Production Calendar.

Fetches calendar pages, normalizes them to a flat dict, and selects the
"proposed / not yet shoot-ready" items that are worth rating. NEVER writes to
Notion — only queries the database and reads page properties.
"""
from __future__ import annotations

import re
from typing import Optional

import config
from logger import get_logger

log = get_logger()

# Camera / production emoji that mark an item as already shoot-ready / in
# production (excluded by default via config.CALENDAR_EXCLUDE_CAMERA_EMOJI).
CAMERA_EMOJI = ("🎥", "📸", "🎬", "🎞", "📹", "🎦", "📷")

# Status values (lowercased) that mean the item is proposed / early enough to
# rate. Anything in _EXCLUDE_STATUS is past the ideation stage.
_RATE_STATUS = {"idea", "draft", "proposed", "backlog", "to review",
                "needs review", "needs revision", "ready for review", "to do",
                "on hold", "copy", "design", "brainstorm", "concept"}
_EXCLUDE_STATUS = {"published", "scheduled", "approved", "ready to ship",
                   "shot", "filmed", "done", "complete", "completed", "archived",
                   "cancelled", "canceled", "live", "posted"}


# ---------------------------------------------------------------------------
# Notion API (read-only)
# ---------------------------------------------------------------------------
def _headers() -> dict:
    return {"Authorization": f"Bearer {config.NOTION_API_KEY}",
            "Notion-Version": "2022-06-28", "Content-Type": "application/json"}


def fetch_calendar_pages(db_id: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Return raw Notion page objects from the content calendar (read-only)."""
    import httpx
    db_id = db_id or config.NOTION_CONTENT_CALENDAR_DB_ID
    if not (config.NOTION_API_KEY and db_id):
        raise RuntimeError("Notion content calendar not configured "
                           "(NOTION_API_KEY / NOTION_CONTENT_CALENDAR_DB_ID).")
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    out, cursor = [], None
    while len(out) < limit:
        body = {"page_size": min(100, limit - len(out))}
        if cursor:
            body["start_cursor"] = cursor
        r = httpx.post(url, headers=_headers(), json=body, timeout=45)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out[:limit]


# ---------------------------------------------------------------------------
# normalization (pure — accepts a raw Notion page dict)
# ---------------------------------------------------------------------------
def _prop_value(prop: dict) -> str:
    t = prop.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
    if t == "select":
        return (prop.get("select") or {}).get("name", "")
    if t == "status":
        return (prop.get("status") or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select", []))
    if t == "date":
        return (prop.get("date") or {}).get("start", "")
    if t == "people":
        return ", ".join(p.get("name", "") for p in prop.get("people", []))
    return ""


def _find(props: dict, *names, ptype=None) -> str:
    for name, prop in props.items():
        if ptype and prop.get("type") != ptype:
            continue
        if any(n.lower() in name.lower() for n in names):
            return _prop_value(prop)
    return ""


def has_camera_emoji(text: str) -> bool:
    return any(e in str(text or "") for e in CAMERA_EMOJI)


# Product / ICP are not calendar properties — derive from the text.
_PRODUCT_KW = {"bodyshield": "BodyShield", "leggings": "Leggings", "pants": "Pants",
               "glove": "Gloves", "exoshield": "ExoShield", "head guard": "Head Guard",
               "slider": "Sliders", "jersey": "Jersey"}
_ICP_KW = {"parent": "Parents", "youth": "Parents", "aspiring pro": "Aspiring Pro",
           "amateur": "Adult Amateur", "adult": "Adult Amateur"}


def _derive(text: str, kw: dict) -> str:
    t = str(text or "").lower()
    for k, v in kw.items():
        if k in t:
            return v
    return ""


def normalize_page(page: dict) -> dict:
    props = page.get("properties", {})
    title = next((_prop_value(p) for p in props.values() if p.get("type") == "title"), "")
    notes = _find(props, "notes", "concept", "caption", "script", "description", ptype="rich_text")
    status = next((_prop_value(p) for p in props.values() if p.get("type") == "status"), "") \
        or _find(props, "status")
    platform = _find(props, "channel", "platform", ptype="multi_select") or _find(props, "channel", "platform")
    asset = _find(props, "asset format", "format", ptype="multi_select")
    brand = _find(props, "brand", ptype="multi_select")
    publish = _find(props, "publish", "date", ptype="date")
    entry_kind = _find(props, "entry kind", "kind", "type", ptype="select")
    owner = _find(props, "assigned", "owner", "author", ptype="people")
    tags = ", ".join(x for x in (asset, entry_kind) if x)
    blob = f"{title} {notes}"
    return {
        "page_id": page.get("id", ""),
        "url": page.get("url", ""),
        "title": title.strip(),
        "status": status.strip(),
        "publish_date": publish,
        "platform": platform,
        "asset_format": asset,
        "brand": brand,
        "entry_kind": entry_kind,
        "product": _derive(blob, _PRODUCT_KW),
        "icp": _derive(blob, _ICP_KW),
        "notes": notes.strip(),
        "owner": owner,
        "tags": tags,
        "has_camera_emoji": has_camera_emoji(title) or has_camera_emoji(notes),
    }


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------
def should_rate(item: dict, exclude_camera: Optional[bool] = None) -> tuple[bool, str]:
    """Return (should_rate, exclusion_reason). Rates proposed/early items; skips
    published/shot/done, camera-emoji (by default), non-deliverables, and empty
    items."""
    if exclude_camera is None:
        exclude_camera = config.CALENDAR_EXCLUDE_CAMERA_EMOJI
    status = str(item.get("status", "")).strip().lower()
    title = str(item.get("title", "")).strip()
    notes = str(item.get("notes", "")).strip()

    if not (title or notes):
        return False, "no idea content (empty title/notes)"
    if status in _EXCLUDE_STATUS:
        return False, f"status '{item.get('status')}' is past ideation"
    kind = str(item.get("entry_kind", "")).strip().lower()
    if kind in ("key date", "campaign marker"):
        return False, f"entry kind '{item.get('entry_kind')}' is not a content idea"
    if exclude_camera and item.get("has_camera_emoji"):
        return False, "camera/production emoji marks it as shoot-ready/in production"
    if status and status not in _RATE_STATUS:
        # Unknown status that isn't an explicit exclusion — rate but flag.
        return True, ""
    return True, ""


def read_ratable_calendar_items(db_id: Optional[str] = None, limit: int = 100,
                                exclude_camera: Optional[bool] = None) -> tuple[list, list]:
    """Fetch + normalize + partition. Returns (ratable, excluded) lists where
    each entry is (item, reason)."""
    pages = fetch_calendar_pages(db_id, limit=limit)
    ratable, excluded = [], []
    for pg in pages:
        item = normalize_page(pg)
        ok, reason = should_rate(item, exclude_camera)
        (ratable if ok else excluded).append((item, reason))
    return ratable, excluded
