"""Central config loaded from environment (.env)."""
import base64
import os
import pathlib

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


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
_SA_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip()
if _SA_B64:
    _out = pathlib.Path(GOOGLE_SERVICE_ACCOUNT_JSON_PATH or "/tmp/service-account.json")
    _out.parent.mkdir(parents=True, exist_ok=True)
    _out.write_bytes(base64.b64decode(_SA_B64))
    GOOGLE_SERVICE_ACCOUNT_JSON_PATH = str(_out)

# Notion
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "").strip()
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID", "").strip()

# Web trigger
RUN_SECRET = os.getenv("RUN_SECRET", "").strip()

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
