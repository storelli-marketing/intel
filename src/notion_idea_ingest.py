"""Ad-hoc Notion idea ingestion (READ-ONLY).

Detects a Notion page URL in Slack text, reads the page's properties and block
content via the existing Notion auth, and normalizes it into a structured idea
object the evaluator can score. NEVER writes to Notion, never changes a page
status — it only issues GET requests.

Returns (idea, error) where exactly one is set:
  - error == ACCESS_ERROR       -> workspace/page not shared with the integration
  - error == INSUFFICIENT_ERROR -> page opens but has too little idea detail
"""
from __future__ import annotations

import re
from typing import Callable, Optional

import config
from logger import get_logger

log = get_logger()

# User-facing errors (verbatim wording required by the spec).
ACCESS_ERROR = ("I can’t access that Notion page yet — make sure the integration has "
                "permission or share the page with the Storelli Notion integration.")
INSUFFICIENT_ERROR = ("I can open the link, but there isn’t enough idea detail to "
                      "evaluate yet.")

# A Notion page URL (notion.so / notion.site). The 32-hex (optionally dashed)
# page id is the trailing token of the path.
NOTION_URL_RE = re.compile(r"https?://(?:www\.)?notion\.(?:so|site)/[^\s<>|]+", re.I)
_ID_RE = re.compile(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")

# Blocks whose rich_text we harvest into raw_text.
_TEXT_BLOCKS = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
                "numbered_list_item", "to_do", "toggle", "quote", "callout", "code")

# Product / ICP derived from free text (calendar uses the same idea).
_PRODUCT_KW = {"bodyshield": "BodyShield GK Leggings", "gk leggings": "BodyShield GK Leggings",
               "leggings": "Pants & Leggings", "pants": "Pants & Leggings",
               "slider": "Sliders", "glove": "Gloves", "exoshield": "ExoShield",
               "head guard": "Head Guard", "jersey": "Jersey"}
_ICP_KW = {"parent": "Parents", "youth": "Parents", "aspiring pro": "Aspiring Pro",
           "amateur": "Adult Amateur", "adult": "Adult Amateur", "pro": "Aspiring Pro"}
_HOOK_KW = {"curiosity": "Curiosity Gap", "fear": "Fear / Risk", "risk": "Fear / Risk",
            "myth": "Do / Don't", "mistake": "Education", "how to": "Education",
            "vs": "Do / Don't", "don't": "Do / Don't"}
_FORMAT_KW = {"demo": "Demo", "tutorial": "Tutorial", "reel": "Reel", "short": "Short",
              "story": "Story", "skit": "Skit", "voiceover": "Voiceover", "pov": "POV"}


# ---------------------------------------------------------------------------
# URL / id detection (pure)
# ---------------------------------------------------------------------------
def find_notion_url(text: str) -> str:
    m = NOTION_URL_RE.search(str(text or ""))
    return m.group(0).rstrip(").,") if m else ""


def extract_page_id(url: str) -> str:
    """Trailing 32-hex page id (dashes stripped), or "" if none."""
    ids = _ID_RE.findall(str(url or ""))
    if not ids:
        return ""
    return ids[-1].replace("-", "").lower()


def _dash(page_id: str) -> str:
    p = str(page_id or "").replace("-", "")
    if len(p) != 32:
        return page_id
    return f"{p[0:8]}-{p[8:12]}-{p[12:16]}-{p[16:20]}-{p[20:32]}"


# ---------------------------------------------------------------------------
# Notion read (httpx GET only — never writes)
# ---------------------------------------------------------------------------
def _headers() -> dict:
    return {"Authorization": f"Bearer {config.NOTION_API_KEY}",
            "Notion-Version": "2022-06-28", "Content-Type": "application/json"}


class NotionAccessError(Exception):
    pass


def fetch_page(page_id: str) -> tuple[dict, list]:
    """Return (page_object, blocks) for a page — READ-ONLY. Raises
    NotionAccessError when the page is not shared with the integration."""
    import httpx
    if not config.NOTION_API_KEY:
        raise NotionAccessError("Notion not configured (NOTION_API_KEY).")
    pid = _dash(page_id)
    try:
        pr = httpx.get(f"https://api.notion.com/v1/pages/{pid}", headers=_headers(), timeout=45)
        if pr.status_code in (401, 403, 404):
            raise NotionAccessError(f"page not accessible ({pr.status_code})")
        pr.raise_for_status()
        page = pr.json()
        blocks, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            br = httpx.get(f"https://api.notion.com/v1/blocks/{pid}/children",
                           headers=_headers(), params=params, timeout=45)
            if br.status_code in (401, 403, 404):
                break   # properties still usable even if children are restricted
            br.raise_for_status()
            data = br.json()
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return page, blocks
    except NotionAccessError:
        raise
    except Exception as e:  # noqa: BLE001 - network / API -> treated as access error
        raise NotionAccessError(str(e))


# ---------------------------------------------------------------------------
# property + block normalization (pure)
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
    if t == "url":
        return prop.get("url", "") or ""
    return ""


def _find_prop(props: dict, *names, ptype=None) -> str:
    for name, prop in props.items():
        if ptype and prop.get("type") != ptype:
            continue
        if any(n.lower() in name.lower() for n in names):
            v = _prop_value(prop)
            if v:
                return v
    return ""


def _block_text(block: dict) -> str:
    t = block.get("type")
    if t not in _TEXT_BLOCKS:
        return ""
    rich = (block.get(t, {}) or {}).get("rich_text", [])
    txt = "".join(x.get("plain_text", "") for x in rich)
    if t == "to_do":
        checked = (block.get(t, {}) or {}).get("checked")
        txt = f"[{'x' if checked else ' '}] {txt}"
    return txt.strip()


def _derive(text: str, kw: dict) -> str:
    t = str(text or "").lower()
    for k, v in kw.items():
        if k in t:
            return v
    return ""


def _multiselect_tags(props: dict) -> list:
    tags = []
    for prop in props.values():
        if prop.get("type") == "multi_select":
            tags.extend(o.get("name", "") for o in prop.get("multi_select", []))
    return [t for t in tags if t]


def normalize(page: dict, blocks: list) -> dict:
    props = page.get("properties", {})
    title = next((_prop_value(p) for p in props.values() if p.get("type") == "title"), "").strip()
    status = (next((_prop_value(p) for p in props.values() if p.get("type") == "status"), "")
              or _find_prop(props, "status")).strip()
    platform = _find_prop(props, "channel", "platform", ptype="multi_select") or \
        _find_prop(props, "channel", "platform")
    fmt = _find_prop(props, "asset format", "format", ptype="multi_select") or \
        _find_prop(props, "format")
    product = _find_prop(props, "product", "gear")
    icp = _find_prop(props, "icp", "audience", "persona")
    hook = _find_prop(props, "hook", "angle")
    concept = _find_prop(props, "concept", "idea", "description", ptype="rich_text")
    caption = _find_prop(props, "caption", "copy", ptype="rich_text")
    script = _find_prop(props, "script", "vo", "voiceover", ptype="rich_text")
    notes = _find_prop(props, "notes", "brief", ptype="rich_text")

    body = "\n".join(t for t in (_block_text(b) for b in blocks) if t)
    blob = " ".join([title, concept, caption, script, notes, body, product, icp])
    raw_text = "\n".join(x for x in [title, concept or notes, caption, script, body] if x).strip()

    return {
        "source_type": "notion_page",
        "page_id": str(page.get("id", "")).replace("-", ""),
        "page_url": page.get("url", ""),
        "title": title,
        "status": status,
        "platform": platform,
        "product": product or _derive(blob, _PRODUCT_KW),
        "icp": icp or _derive(blob, _ICP_KW),
        "format": fmt or _derive(blob, _FORMAT_KW),
        "hook": hook or _derive(blob, _HOOK_KW),
        "concept": (concept or notes or body)[:2000],
        "caption": caption[:1500],
        "script": script[:2000],
        "notes": notes[:1500],
        "tags": _multiselect_tags(props),
        "raw_text": raw_text[:4000],
    }


def _has_enough_detail(idea: dict) -> bool:
    """Meaningful idea content beyond a bare title — enough to actually judge."""
    title = str(idea.get("title", "")).strip()
    body = " ".join(str(idea.get(k, "")) for k in
                    ("concept", "caption", "script", "notes")).strip()
    body_chars = len(re.sub(r"[^a-z0-9]", "", body.lower()))
    title_chars = len(re.sub(r"[^a-z0-9]", "", title.lower()))
    # Need a real body, or at least a descriptive multi-word title.
    return body_chars >= 25 or (title_chars >= 12 and len(title.split()) >= 3 and body_chars >= 8)


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------
def ingest(text_or_url: str,
           fetcher: Optional[Callable[[str], tuple]] = None) -> tuple[Optional[dict], Optional[str]]:
    """Detect + read + normalize a Notion idea page. `fetcher(page_id)` is
    injectable for tests; defaults to the read-only Notion API. Returns
    (idea, None) on success or (None, error_message)."""
    url = find_notion_url(text_or_url) or str(text_or_url or "").strip()
    page_id = extract_page_id(url)
    if not page_id:
        return None, ACCESS_ERROR
    try:
        page, blocks = (fetcher or fetch_page)(page_id)
    except NotionAccessError as e:
        log.info("notion idea ingest access error: %s", e)
        return None, ACCESS_ERROR
    except Exception as e:  # noqa: BLE001
        log.warning("notion idea ingest failed: %s", e)
        return None, ACCESS_ERROR
    idea = normalize(page or {}, blocks or [])
    if not idea.get("page_url"):
        idea["page_url"] = url
    if not _has_enough_detail(idea):
        return None, INSUFFICIENT_ERROR
    return idea, None
