"""Google Sheets I/O for the Inspiration Layer tabs.

These tabs use a SIMPLE single-row header (row 1 = column names, row 2+ = data),
unlike the internal POC sheet's two-row header handled by sheets_client.py.

Hard isolation guarantee
------------------------
This module writes ONLY to the inspiration tabs (INSPIRATION_CONTENT,
INSPIRATION_RUNS) and the monitored-channels bookkeeping columns. It refuses to
target the internal POC worksheet (config.GOOGLE_WORKSHEET_NAME). External
inspiration therefore lives in physically separate worksheets and can never be
read by the internal learning pipeline (correlations, synthesis, Notion Brain),
which only ever opens GOOGLE_WORKSHEET_NAME.
"""
from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

import config
from logger import get_logger

log = get_logger()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Tab names exactly as they exist in the imported inspiration-layer template.
# Note MONITORED CHANNELS uses a space (not an underscore) in the live sheet.
MONITORED_CHANNELS_TAB = "MONITORED CHANNELS"
INSPIRATION_CONTENT_TAB = "INSPIRATION_CONTENT"
INSPIRATION_RUNS_TAB = "INSPIRATION_RUNS"
INSPIRATION_CONFIG_TAB = "INSPIRATION_CONFIG"
INSPIRATION_URL_QUEUE_TAB = "INSPIRATION_URL_QUEUE"

# Human-in-the-loop queue: paste promising individual reel/post URLs here and
# process-inspiration-queue ingests each one via yt-dlp (single-URL, cookie
# auth) — no profile enumeration, no Apify.
QUEUE_HEADERS = [
    "QUEUE_ID", "ADDED_AT", "ADDED_BY", "CHANNEL_HANDLE", "POST_URL",
    "MACRO_INDUSTRY", "SUBCATEGORY", "REASON_FOR_ADDING", "TARGET_PRODUCT",
    "TARGET_ICP", "STATUS", "PROCESSED_AT", "SOURCE_ID", "ERROR_MESSAGE",
]
# Queue rows eligible for processing (case-insensitive).
QUEUE_PENDING_STATUSES = {"", "queued"}

# The one immutable invariant: the SOURCE_TYPE every ingested row carries.
SOURCE_TYPE_EXTERNAL = "EXTERNAL_INSPIRATION"

# Truthy spellings for the ACTIVE / boolean columns.
_TRUE = {"true", "1", "yes", "y", "active", "on"}


def _is_true(value) -> bool:
    return str(value or "").strip().lower() in _TRUE


class InspirationSheets:
    """Thin accessor over the inspiration tabs. Reads headers dynamically so a
    column reorder in the template does not break writes."""

    def __init__(self):
        config.require_sheets()
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH, scopes=SCOPES)
        self._gc = gspread.authorize(creds)
        self._sh = self._gc.open_by_key(config.GOOGLE_SHEET_ID)
        self._ws_cache: dict[str, gspread.Worksheet] = {}

    @staticmethod
    def is_internal_tab(tab: str) -> bool:
        """True if `tab` is the internal POC worksheet the learning pipeline
        reads. The inspiration layer must never write there."""
        return str(tab).strip().lower() == str(config.GOOGLE_WORKSHEET_NAME).strip().lower()

    # ---- worksheet access with a hard internal-sheet guard ----------------
    def _ws(self, tab: str) -> gspread.Worksheet:
        # Guardrail: never let inspiration I/O touch the internal POC sheet.
        if self.is_internal_tab(tab):
            raise RuntimeError(
                f"Refusing to access internal worksheet {tab!r} from the "
                f"inspiration layer — external inspiration must stay isolated.")
        if tab not in self._ws_cache:
            self._ws_cache[tab] = self._sh.worksheet(tab)
        return self._ws_cache[tab]

    def tab_names(self) -> list[str]:
        return [ws.title for ws in self._sh.worksheets()]

    @staticmethod
    def _read_table(ws: gspread.Worksheet) -> tuple[list[str], list[dict]]:
        """Return (headers, rows) where each row is a dict keyed by header plus
        `_row` (1-based sheet row number)."""
        values = ws.get_all_values()
        if not values:
            return [], []
        headers = [h.strip() for h in values[0]]
        rows = []
        for offset, raw in enumerate(values[1:]):
            rec: dict = {"_row": offset + 2}
            for i, name in enumerate(headers):
                if name:
                    rec[name] = raw[i] if i < len(raw) else ""
            rows.append(rec)
        return headers, rows

    # ---- monitored channels ----------------------------------------------
    def read_active_channels(self) -> list[dict]:
        """ACTIVE monitored channels with a usable PROFILE_URL or HANDLE."""
        _, rows = self._read_table(self._ws(MONITORED_CHANNELS_TAB))
        out = []
        for r in rows:
            if not _is_true(r.get("ACTIVE")):
                continue
            if not (str(r.get("PROFILE_URL", "")).strip()
                    or str(r.get("HANDLE", "")).strip()):
                continue
            out.append(r)
        return out

    def update_channel_status(self, row_index: int, *, last_scanned_at: str = "",
                              last_post_id: str = "", scan_status: str = "",
                              error_message: str = "") -> None:
        ws = self._ws(MONITORED_CHANNELS_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        col = {name: i + 1 for i, name in enumerate(headers) if name}
        updates = []

        def _set(name, val):
            if name in col:
                updates.append({
                    "range": gspread.utils.rowcol_to_a1(row_index, col[name]),
                    "values": [[val]]})

        if last_scanned_at:
            _set("LAST_SCANNED_AT", last_scanned_at)
        if last_post_id:
            _set("LAST_POST_ID", last_post_id)
        if scan_status:
            _set("SCAN_STATUS", scan_status)
        # ERROR_MESSAGE is always written (blank clears a previous error).
        _set("ERROR_MESSAGE", error_message)
        if updates:
            ws.batch_update(updates)

    # ---- inspiration config ----------------------------------------------
    def read_config(self) -> dict[str, str]:
        """ACTIVE key/value pairs from INSPIRATION_CONFIG."""
        try:
            _, rows = self._read_table(self._ws(INSPIRATION_CONFIG_TAB))
        except gspread.WorksheetNotFound:
            return {}
        out = {}
        for r in rows:
            key = str(r.get("KEY", "")).strip()
            if key and _is_true(r.get("ACTIVE", "TRUE")):
                out[key] = str(r.get("VALUE", "")).strip()
        return out

    # ---- inspiration content ---------------------------------------------
    def content_headers(self) -> list[str]:
        headers, _ = self._read_table(self._ws(INSPIRATION_CONTENT_TAB))
        return headers

    def existing_content_keys(self) -> dict[str, set]:
        """Existing dedup keys already in INSPIRATION_CONTENT."""
        _, rows = self._read_table(self._ws(INSPIRATION_CONTENT_TAB))
        keys = {"SOURCE_ID": set(), "POST_ID": set(), "POST_URL": set()}
        for r in rows:
            for k in keys:
                v = str(r.get(k, "")).strip()
                if v:
                    keys[k].add(v)
        return keys

    def append_content_rows(self, row_dicts: list[dict]) -> int:
        """Append fully-formed post dicts to INSPIRATION_CONTENT, aligned to the
        live header order. Every row MUST carry SOURCE_TYPE=EXTERNAL_INSPIRATION
        — this is asserted, not assumed."""
        if not row_dicts:
            return 0
        ws = self._ws(INSPIRATION_CONTENT_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        matrix = []
        for d in row_dicts:
            if str(d.get("SOURCE_TYPE", "")).strip() != SOURCE_TYPE_EXTERNAL:
                raise ValueError(
                    "Inspiration row missing SOURCE_TYPE=EXTERNAL_INSPIRATION; "
                    "refusing to write unlabeled external content.")
            matrix.append([str(d.get(h, "")) for h in headers])
        ws.append_rows(matrix, value_input_option="RAW")
        return len(matrix)

    # ---- inspiration URL queue (human-in-the-loop) ------------------------
    def ensure_queue_tab(self) -> bool:
        """Create INSPIRATION_URL_QUEUE with the canonical header if it does not
        exist. Returns True if it was created, False if it already existed.
        Never modifies an existing tab's contents."""
        titles = [ws.title for ws in self._sh.worksheets()]
        if INSPIRATION_URL_QUEUE_TAB in titles:
            return False
        ws = self._sh.add_worksheet(
            title=INSPIRATION_URL_QUEUE_TAB, rows=1000, cols=len(QUEUE_HEADERS))
        ws.update(range_name="A1", values=[QUEUE_HEADERS], value_input_option="RAW")
        self._ws_cache[INSPIRATION_URL_QUEUE_TAB] = ws
        return True

    def read_queued_urls(self) -> list[dict]:
        """Queue rows whose STATUS is blank or 'Queued' and that carry a URL."""
        try:
            _, rows = self._read_table(self._ws(INSPIRATION_URL_QUEUE_TAB))
        except gspread.WorksheetNotFound:
            return []
        out = []
        for r in rows:
            status = str(r.get("STATUS", "")).strip().lower()
            if status not in QUEUE_PENDING_STATUSES:
                continue
            if not str(r.get("POST_URL", "")).strip():
                continue
            out.append(r)
        return out

    def update_queue_row(self, row_index: int, *, status: str = "",
                         processed_at: str = "", source_id: str = "",
                         error_message: str = "") -> None:
        ws = self._ws(INSPIRATION_URL_QUEUE_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        col = {name: i + 1 for i, name in enumerate(headers) if name}
        updates = []

        def _set(name, val):
            if name in col:
                updates.append({
                    "range": gspread.utils.rowcol_to_a1(row_index, col[name]),
                    "values": [[val]]})

        if status:
            _set("STATUS", status)
        if processed_at:
            _set("PROCESSED_AT", processed_at)
        if source_id:
            _set("SOURCE_ID", source_id)
        _set("ERROR_MESSAGE", error_message)  # always written (clears on success)
        if updates:
            ws.batch_update(updates)

    # ---- run log ----------------------------------------------------------
    def append_run(self, run: dict) -> None:
        ws = self._ws(INSPIRATION_RUNS_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        ws.append_row([str(run.get(h, "")) for h in headers],
                      value_input_option="RAW")
