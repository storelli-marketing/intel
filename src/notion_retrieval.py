"""Read-only Notion Brain retrieval for the Slack conversational bot.

Connects with the existing NOTION_API_KEY / NOTION_PARENT_PAGE_ID, finds the
Marketing Brain's child databases by title (same pattern as notion_brain.py),
queries rows, and normalizes them into simple chunks the Slack answer engine
can filter and cite. Read-only: only ever calls `databases.query` /
`blocks.children.list` — never creates or updates a page. Fails clean (returns
[]) whenever Notion isn't configured or a call errors, so the Slack bot can
always fall back to latest_learnings.md / the Sheet without crashing.
"""
from __future__ import annotations

from logger import get_logger

log = get_logger()

# The six Marketing Brain databases this bot may read from.
DATABASES = (
    "Marketing Learnings",
    "Signal Library",
    "Next Creative Tests",
    "Product Learnings",
    "ICP Learnings",
    "Generated Social Ideas",
)

# Per-database property-name mapping into the normalized chunk shape.
# "extra" lists additional properties worth surfacing verbatim (by name).
_DB_FIELD_MAP = {
    "Marketing Learnings": {
        "title": "Title", "product": "Product", "icp": "ICP",
        "hook": None, "format": None, "confidence": "Confidence",
        "extra": ["Signal", "Layer", "Finding", "Recommended Action"],
    },
    "Signal Library": {
        "title": "Signal Name", "product": None, "icp": None,
        "hook": None, "format": None, "confidence": "Confidence",
        "extra": ["Layer", "Works Best For", "Weak For", "Evidence Count"],
    },
    "Next Creative Tests": {
        "title": "Hypothesis", "product": "Product", "icp": "ICP",
        "hook": "Hook", "format": "Format", "confidence": None,
        "extra": ["Problem Type", "Solution Type", "Priority", "Status", "Result"],
    },
    "Product Learnings": {
        "title": "Product", "product": "Product", "icp": None,
        "hook": "Best Hooks", "format": "Best Formats", "confidence": "Confidence",
        "extra": ["Weak Angles", "Next Direction"],
    },
    "ICP Learnings": {
        "title": "ICP", "product": None, "icp": "ICP",
        "hook": "Best Hooks", "format": "Best Formats", "confidence": "Confidence",
        "extra": ["Core Motivation", "Recommended Messaging"],
    },
    "Generated Social Ideas": {
        "title": "Title", "product": "Product", "icp": "ICP",
        "hook": "Hook", "format": "Format", "confidence": "Confidence",
        "extra": ["Storytelling Structure", "Why This Should Work", "Status"],
    },
}


def available() -> bool:
    import config
    return bool(config.NOTION_API_KEY and config.NOTION_PARENT_PAGE_ID)


def _prop_text(prop: dict | None) -> str:
    """Best-effort plain-text rendering of a Notion property value."""
    if not prop:
        return ""
    t = prop.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title") or [])
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text") or [])
    if t == "select":
        return (prop.get("select") or {}).get("name", "") or ""
    if t == "multi_select":
        return ", ".join(x.get("name", "") for x in prop.get("multi_select") or [])
    if t == "number":
        n = prop.get("number")
        return "" if n is None else str(n)
    if t == "date":
        return (prop.get("date") or {}).get("start", "") or ""
    return ""


def _normalize(db_title: str, page: dict, field_map: dict) -> dict:
    props = page.get("properties", {})

    def get(name):
        return _prop_text(props.get(name)) if name else ""

    return {
        "database": db_title,
        "title": get(field_map.get("title")),
        "product": get(field_map.get("product")),
        "icp": get(field_map.get("icp")),
        "hook": get(field_map.get("hook")),
        "format": get(field_map.get("format")),
        "confidence": get(field_map.get("confidence")),
        "url": page.get("url", ""),
        "extra": {name: get(name) for name in field_map.get("extra", [])},
    }


class NotionRetrieval:
    """Thin, read-only client. Raises on connect (caller wraps in try/except
    via the module-level helpers below); every method here is a plain read."""

    def __init__(self):
        import config
        config.require_notion()
        from notion_client import NotionSDK  # real PyPI SDK, see notion_client.py
        self.client = NotionSDK(auth=config.NOTION_API_KEY)
        self.parent = config.NOTION_PARENT_PAGE_ID

    def _find_databases(self) -> dict:
        out, cursor = {}, None
        while True:
            kw = {"block_id": self.parent}
            if cursor:
                kw["start_cursor"] = cursor
            res = self.client.blocks.children.list(**kw)
            for b in res.get("results", []):
                if b.get("type") == "child_database":
                    out[b["child_database"].get("title", "")] = b["id"]
            if res.get("has_more"):
                cursor = res.get("next_cursor")
            else:
                break
        return out

    def query_database(self, title: str, page_size: int = 20) -> list[dict]:
        dbs = self._find_databases()
        db_id = dbs.get(title)
        if not db_id:
            return []
        field_map = _DB_FIELD_MAP.get(title, {})
        res = self.client.databases.query(database_id=db_id, page_size=page_size)
        return [_normalize(title, page, field_map) for page in res.get("results", [])]


def _word_overlap_match(query: str, value: str) -> bool:
    """True if query and value substring-match either way, or share a
    significant word. Handles the taxonomy's canonical product name (e.g.
    'BodyShield Leggings') not being an exact substring of the Sheet's actual
    free-text value (e.g. 'BodyShield NoBurn GK Leggings')."""
    q, v = query.lower(), value.lower()
    if not q or not v:
        return False
    if q in v or v in q:
        return True
    stopwords = {"gk", "the", "a", "of"}
    q_words = set(q.split()) - stopwords
    v_words = set(v.split()) - stopwords
    return bool(q_words & v_words)


def filter_chunks(chunks: list[dict], *, product: str = "", icp: str = "",
                  keyword: str = "") -> list[dict]:
    """Filter normalized chunks by product / ICP (substring or shared-word
    match), or a free keyword matched against title/product/icp/hook/format/
    extra values."""
    out = list(chunks)
    if product:
        out = [c for c in out if _word_overlap_match(product, c.get("product") or "")]
    if icp:
        out = [c for c in out if _word_overlap_match(icp, c.get("icp") or "")]
    if keyword:
        k = keyword.lower()

        def _hay(c: dict) -> str:
            vals = [c.get("title", ""), c.get("product", ""), c.get("icp", ""),
                    c.get("hook", ""), c.get("format", "")]
            vals += list((c.get("extra") or {}).values())
            return " ".join(str(v) for v in vals).lower()

        out = [c for c in out if k in _hay(c)]
    return out


def query(database: str, *, product: str = "", icp: str = "", keyword: str = "",
          limit: int = 20) -> list[dict]:
    """Best-effort normalized, optionally-filtered chunks from one Notion Brain
    database. Returns [] when Notion isn't configured, the database doesn't
    exist yet under the parent page, or any call fails — never raises."""
    if not available():
        return []
    try:
        chunks = NotionRetrieval().query_database(database, page_size=limit)
    except Exception as e:  # noqa: BLE001 - Notion is a bonus source, never fatal
        log.warning("notion_retrieval: query(%s) failed: %s", database, e)
        return []
    return filter_chunks(chunks, product=product, icp=icp, keyword=keyword)


def fetch_all(limit_per_db: int = 10) -> dict[str, list[dict]]:
    """Best-effort chunks from every known database, keyed by database title.
    Databases that don't exist yet or fail to query are simply absent from the
    result — never raises."""
    if not available():
        return {}
    out: dict[str, list[dict]] = {}
    try:
        client = NotionRetrieval()
        dbs = client._find_databases()
    except Exception as e:  # noqa: BLE001
        log.warning("notion_retrieval: fetch_all could not list databases: %s", e)
        return {}
    for title in DATABASES:
        if title not in dbs:
            continue
        try:
            field_map = _DB_FIELD_MAP.get(title, {})
            res = client.client.databases.query(database_id=dbs[title], page_size=limit_per_db)
            out[title] = [_normalize(title, page, field_map) for page in res.get("results", [])]
        except Exception as e:  # noqa: BLE001
            log.warning("notion_retrieval: fetch_all(%s) failed: %s", title, e)
    return out
