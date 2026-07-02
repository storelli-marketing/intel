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

# Slack strategist synthesis mode (src/social_strategist.py). When enabled,
# the Slack bot's answers are composed by Gemini from an already-retrieved,
# already-cited evidence pack (never raw/endless data) instead of just
# rendering the deterministic mode text verbatim — real judgment/tradeoffs,
# not a data dump. Defaults to on when Gemini is configured (explicit
# true/false always wins); every path falls back to the proven deterministic
# answer on any failure, invalid citation, invented number, or causal claim.
_STRATEGIST_ENV = os.getenv("SLACK_STRATEGIST_MODE_ENABLED", "").strip().lower()
if _STRATEGIST_ENV in ("true", "1", "yes", "on"):
    SLACK_STRATEGIST_MODE_ENABLED = True
elif _STRATEGIST_ENV in ("false", "0", "no", "off"):
    SLACK_STRATEGIST_MODE_ENABLED = False
else:
    SLACK_STRATEGIST_MODE_ENABLED = bool(GEMINI_API_KEY)

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
        # Mirror to env so require_sheets() (which reads os.getenv) sees it.
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON_PATH"] = str(_out)
    except Exception as _e:  # noqa: BLE001 - never crash import on a bad env value
        import sys
        print(f"WARNING: could not decode GOOGLE_SERVICE_ACCOUNT_JSON_B64 "
              f"({type(_e).__name__}: {_e}); Sheets actions will fail until fixed.",
              file=sys.stderr)

# yt-dlp cookies (optional). Instagram increasingly serves an empty/auth-walled
# response to anonymous downloads, so yt-dlp may need an authenticated session's
# exported cookies.txt (Netscape format) to fetch reel media. When neither var
# is set, download behavior is unchanged (anonymous, as before). Fail-soft: a
# malformed YTDLP_COOKIES_B64 must NOT crash app startup — only the Instagram
# download itself fails cleanly when it's actually used.
YTDLP_COOKIES_PATH = os.getenv("YTDLP_COOKIES_PATH", "").strip()

_YTDLP_COOKIES_B64 = os.getenv("YTDLP_COOKIES_B64", "").strip()
if _YTDLP_COOKIES_B64:
    try:
        _cookies_out = pathlib.Path(YTDLP_COOKIES_PATH or "/tmp/yt-dlp-cookies.txt")
        _cookies_out.parent.mkdir(parents=True, exist_ok=True)
        _cookies_out.write_bytes(base64.b64decode(_YTDLP_COOKIES_B64, validate=True))
        YTDLP_COOKIES_PATH = str(_cookies_out)
        os.environ["YTDLP_COOKIES_PATH"] = str(_cookies_out)
    except Exception as _e:  # noqa: BLE001 - never crash import on a bad env value
        import sys
        print(f"WARNING: could not decode YTDLP_COOKIES_B64 "
              f"({type(_e).__name__}: {_e}); yt-dlp will run without cookies until fixed.",
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

# Slack bot (optional). When both are set, /slack/events accepts app_mention
# events from a Slack app and the Marketing Brain replies in-thread. Missing
# values must not crash startup — /slack/events returns 503 cleanly instead.
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()

# Optional: let the Slack conversational bot ask Gemini to rephrase its
# deterministic, cited answers more conversationally. OFF by default —
# Gemini quota is scarce and shared with video tagging (~20 req/day per the
# free tier); every enabled Slack reply would spend one call from that same
# pool. The deterministic (grounded, cited) answer is always used verbatim
# when this is off, when Gemini fails, or when its output fails validation
# (dropped/invented citations, invented numbers, causal language).
SLACK_LLM_POLISH_ENABLED = os.getenv("SLACK_LLM_POLISH_ENABLED", "false").strip().lower() \
    in ("true", "1", "yes", "on")

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


def require_slack_bot() -> None:
    _require("SLACK_BOT_TOKEN")
    _require("SLACK_SIGNING_SECRET")
