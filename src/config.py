"""Central config loaded from environment (.env)."""
import base64
import os
import pathlib
import re

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def clean_notion_id(raw: str) -> str:
    """Extract a clean dashed Notion UUID from whatever the user pasted — a bare
    id, a `...-32hex` page URL, or a `32hex?v=...` database-view URL."""
    if not raw:
        return raw
    head = raw.split("?")[0]                       # drop ?v=... view query
    compact = re.sub(r"[^0-9a-fA-F]", "", head)     # strip URL/title/dashes
    if len(compact) >= 32:
        h = compact[-32:].lower()                   # Notion id sits at the end
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return head.strip()


# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

# Google Sheets
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "").strip()
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Sheet1").strip()

# Optional Railway helper: if you can't mount the service-account JSON file,
# base64-encode it and set GOOGLE_SERVICE_ACCOUNT_JSON_B64. We decode it to
# disk on import so the existing path-based loader keeps working unchanged.
# Fail-soft: a malformed value must NOT crash app startup — it's logged and the
# Sheets-dependent actions simply error cleanly when used.
_SA_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
if _SA_B64:
    try:
        _out = pathlib.Path(GOOGLE_SERVICE_ACCOUNT_JSON_PATH or "/tmp/service-account.json")
        _out.parent.mkdir(parents=True, exist_ok=True)
        _out.write_bytes(base64.b64decode(_SA_B64, validate=True))
        GOOGLE_SERVICE_ACCOUNT_JSON_PATH = str(_out)
    except Exception as _e:  # noqa: BLE001 - never crash import on a bad env value
        import sys
        print(f"WARNING: could not decode GOOGLE_SERVICE_ACCOUNT_JSON_B64 "
              f"({type(_e).__name__}: {_e}); Sheets actions will fail until fixed.",
              file=sys.stderr)

# Notion
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PARENT_PAGE_ID = clean_notion_id(os.getenv("NOTION_PARENT_PAGE_ID", "").strip())
# Optional: a Notion page/dashboard URL the "Open Notion Dashboard" button links to.
NOTION_DASHBOARD_URL = os.getenv("NOTION_DASHBOARD_URL", "").strip()

# Web trigger
RUN_SECRET = os.getenv("RUN_SECRET", "").strip()

# Slack reporting (optional). If set, the dashboard can post a run summary and
# `python src/main.py slack-report` works.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()

# Public dashboard URL, shown in the Slack report (optional).
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()

# QA compiler pass. On by default (2 Gemini calls/row). Set false to skip it
# (1 call/row) — useful to stretch a limited free-tier quota.
QA_COMPILER_ENABLED = os.getenv("QA_COMPILER_ENABLED", "true").strip().lower() \
    not in ("false", "0", "no", "off")

# Default Storelli IG follower count, used to compute views/followers ratio
# when the sheet has no per-row Followers column.
try:
    STORELLI_IG_FOLLOWER_COUNT = int(os.getenv("STORELLI_IG_FOLLOWER_COUNT", "170000") or 170000)
except ValueError:
    STORELLI_IG_FOLLOWER_COUNT = 170000


def require_sheets() -> None:
    _require("GOOGLE_SHEET_ID")
    _require("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")


def require_gemini() -> None:
    _require("GEMINI_API_KEY")


def require_notion() -> None:
    _require("NOTION_API_KEY")
    _require("NOTION_PARENT_PAGE_ID")
