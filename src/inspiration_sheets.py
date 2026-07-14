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
APIFY_DISCOVERY_QUERIES_TAB = "APIFY_DISCOVERY_QUERIES"
WINNING_FORMAT_PROFILES_TAB = "WINNING_FORMAT_PROFILES"
INSPIRATION_IDEAS_TAB = "INSPIRATION_IDEAS"

# Rated-idea columns appended to INSPIRATION_IDEAS on first idea-gen run (the
# base idea-structure columns already exist in the template).
IDEA_SCORE_COLUMNS = [
    "IDEA_SCORE", "EVIDENCE_FIT_SCORE", "INSPIRATION_FIT_SCORE", "NOVELTY_SCORE",
    "PRODUCT_FIT_SCORE", "ICP_FIT_SCORE", "EXECUTION_CLARITY_SCORE",
    "FEASIBILITY_SCORE", "COPYRIGHT_SAFETY_SCORE", "STRATEGIC_PRIORITY_SCORE",
    "SOURCE_PROFILE_ID", "SOURCE_PROFILE_NAME", "EXTERNAL_SOURCE_IDS",
    "EXTERNAL_REFERENCE_URLS", "INTERNAL_EVIDENCE_URLS", "IDEA_RATIONALE",
    "SELF_CRITIQUE", "RISK_NOTES", "RECOMMENDED_SHOOT_PRIORITY",
]

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

# Human curation context carried from a queue row onto its INSPIRATION_CONTENT
# row (appended to that tab's header if absent). Metadata-only — no analysis.
CONTENT_CURATION_COLUMNS = [
    "QUEUE_ID", "ADDED_BY", "REASON_FOR_ADDING", "TARGET_PRODUCT", "TARGET_ICP",
]

# Apify discovery + ranking columns added to INSPIRATION_CONTENT on first run.
# VIEW_COUNT already exists in the base template, so it is not repeated here.
CONTENT_DISCOVERY_COLUMNS = [
    "DISCOVERY_QUERY_ID", "DISCOVERY_PLATFORM", "DISCOVERY_QUERY", "RESEARCH_RING",
    "SEMANTIC_DISTANCE", "REASON_FOR_QUERY", "SHOULD_FIND", "SHOULD_AVOID",
    "FOLLOWER_COUNT", "VIEW_FOLLOWER_RATIO", "ABSOLUTE_VIEW_SCORE", "RATIO_SCORE",
    "MECHANISM_RELEVANCE_SCORE", "COPYRIGHT_SAFETY_SCORE", "PRIORITY_SCORE",
    "SAFETY_STATUS", "REJECTION_REASON",
]

# Match columns added to INSPIRATION_CONTENT on first match run (the base
# MATCH_SCORE/NOVELTY/FINAL/SHORTLISTED/MATCHED_* columns already exist).
CONTENT_MATCH_COLUMNS = [
    "BEST_MATCHED_PROFILE_ID", "BEST_MATCHED_PROFILE_NAME", "MATCH_CONFIDENCE",
    "MATCH_EXPLANATION",
]

# Quality-review columns added on first quality-review run.
CONTENT_QUALITY_COLUMNS = [
    "QUALITY_REVIEW_STATUS", "REVIEW_METHOD", "CREATIVE_MECHANISM",
    "ADAPTABILITY_SCORE", "STORELLI_RELEVANCE_SCORE", "COPYRIGHT_RISK_SCORE",
    "FAMOUS_PLAYER_RISK", "MATCH_FOOTAGE_RISK", "OFF_DOMAIN_RISK",
    "INSPIRATION_QUALITY_SCORE", "QUALITY_REVIEW_NOTES", "USE_FOR_IDEA_GEN",
]

# APIFY_DISCOVERY_QUERIES tab header (created if missing).
DISCOVERY_QUERY_HEADERS = [
    "QUERY_ID", "PLATFORM", "QUERY_TYPE", "QUERY", "RESEARCH_RING",
    "SEMANTIC_DISTANCE", "MACRO_INDUSTRY", "SUBCATEGORY", "TARGET_PRODUCT",
    "TARGET_ICP", "REASON_FOR_QUERY", "SHOULD_FIND", "SHOULD_AVOID", "ACTIVE",
    "MAX_RESULTS", "LOOKBACK_DAYS", "MIN_VIEW_COUNT", "MIN_VIEW_FOLLOWER_RATIO",
    "MAX_FOLLOWER_COUNT", "MAX_RUN_COST_USD", "LAST_RUN_AT", "LAST_RUN_STATUS",
    "RESULTS_ADDED", "RESULTS_SKIPPED", "ERROR_MESSAGE",
]

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

    def ensure_content_columns(self, columns: list[str]) -> list[str]:
        """Append any of `columns` that are not already in the INSPIRATION_CONTENT
        header, at the end (never reorders/overwrites existing columns). Returns
        the list of columns actually added. Idempotent."""
        ws = self._ws(INSPIRATION_CONTENT_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        missing = [c for c in columns if c not in headers]
        if not missing:
            return []
        start = len(headers) + 1
        needed = len(headers) + len(missing)
        if ws.col_count < needed:
            ws.add_cols(needed - ws.col_count)
        updates = [{"range": gspread.utils.rowcol_to_a1(1, start + i),
                    "values": [[name]]} for i, name in enumerate(missing)]
        ws.batch_update(updates)
        return missing

    def read_content_rows(self) -> list[dict]:
        """All INSPIRATION_CONTENT data rows (dicts keyed by header + `_row`)."""
        _, rows = self._read_table(self._ws(INSPIRATION_CONTENT_TAB))
        return rows

    def update_content_cells(self, row_index: int, values: dict) -> None:
        """Write specific named cells on one INSPIRATION_CONTENT row (aligned to
        the live header). Unknown column names are ignored. Used by the external
        inspiration analyzer to write tags/status — never touches other tabs."""
        if not values:
            return
        ws = self._ws(INSPIRATION_CONTENT_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        col = {name: i + 1 for i, name in enumerate(headers) if name}
        updates = [{"range": gspread.utils.rowcol_to_a1(row_index, col[name]),
                    "values": [[val]]}
                   for name, val in values.items() if name in col]
        if updates:
            ws.batch_update(updates)

    def update_content_cells_bulk(self, updates: list) -> None:
        """Write named cells across MANY INSPIRATION_CONTENT rows in ONE batched
        request (reads the header once). `updates` is a list of (row_index,
        values_dict). Avoids the per-row header re-read that trips the Sheets
        read-quota on large batches."""
        updates = [u for u in updates if u and u[1]]
        if not updates:
            return
        ws = self._ws(INSPIRATION_CONTENT_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        col = {name: i + 1 for i, name in enumerate(headers) if name}
        cells = []
        for row_index, values in updates:
            for name, val in values.items():
                if name in col:
                    cells.append({"range": gspread.utils.rowcol_to_a1(row_index, col[name]),
                                  "values": [[val]]})
        # Chunk to stay well under request-size limits.
        for i in range(0, len(cells), 5000):
            ws.batch_update(cells[i:i + 5000])

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

    # ---- Apify discovery queries -----------------------------------------
    def ensure_queries_tab(self) -> bool:
        """Create APIFY_DISCOVERY_QUERIES with the canonical header if missing.
        Returns True if created. Never modifies an existing tab's contents."""
        titles = [ws.title for ws in self._sh.worksheets()]
        if APIFY_DISCOVERY_QUERIES_TAB in titles:
            return False
        ws = self._sh.add_worksheet(
            title=APIFY_DISCOVERY_QUERIES_TAB, rows=1000,
            cols=len(DISCOVERY_QUERY_HEADERS))
        ws.update(range_name="A1", values=[DISCOVERY_QUERY_HEADERS],
                  value_input_option="RAW")
        self._ws_cache[APIFY_DISCOVERY_QUERIES_TAB] = ws
        return True

    def read_active_queries(self) -> list[dict]:
        """ACTIVE discovery queries that carry a QUERY value."""
        try:
            _, rows = self._read_table(self._ws(APIFY_DISCOVERY_QUERIES_TAB))
        except gspread.WorksheetNotFound:
            return []
        return [r for r in rows
                if _is_true(r.get("ACTIVE")) and str(r.get("QUERY", "")).strip()]

    def update_query_row(self, row_index: int, *, last_run_at: str = "",
                         last_run_status: str = "", results_added=None,
                         results_skipped=None, error_message: str = "") -> None:
        ws = self._ws(APIFY_DISCOVERY_QUERIES_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        col = {name: i + 1 for i, name in enumerate(headers) if name}
        updates = []

        def _set(name, val):
            if name in col:
                updates.append({
                    "range": gspread.utils.rowcol_to_a1(row_index, col[name]),
                    "values": [[val]]})

        if last_run_at:
            _set("LAST_RUN_AT", last_run_at)
        if last_run_status:
            _set("LAST_RUN_STATUS", last_run_status)
        if results_added is not None:
            _set("RESULTS_ADDED", results_added)
        if results_skipped is not None:
            _set("RESULTS_SKIPPED", results_skipped)
        _set("ERROR_MESSAGE", error_message)
        if updates:
            ws.batch_update(updates)

    # ---- winning format profiles (internal evidence only) ----------------
    # ---- rated creative ideas --------------------------------------------
    def ensure_idea_columns(self, columns: list[str]) -> list[str]:
        ws = self._ws(INSPIRATION_IDEAS_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        missing = [c for c in columns if c not in headers]
        if not missing:
            return []
        start = len(headers) + 1
        needed = len(headers) + len(missing)
        if ws.col_count < needed:
            ws.add_cols(needed - ws.col_count)
        updates = [{"range": gspread.utils.rowcol_to_a1(1, start + i), "values": [[name]]}
                   for i, name in enumerate(missing)]
        ws.batch_update(updates)
        return missing

    def append_ideas(self, idea_dicts: list[dict]) -> int:
        if not idea_dicts:
            return 0
        ws = self._ws(INSPIRATION_IDEAS_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        matrix = [[str(d.get(h, "")) for h in headers] for d in idea_dicts]
        ws.append_rows(matrix, value_input_option="RAW")
        return len(matrix)

    def read_ideas(self) -> list[dict]:
        _, rows = self._read_table(self._ws(INSPIRATION_IDEAS_TAB))
        return [r for r in rows if str(r.get("IDEA_ID", "")).strip()]

    # ---- winning format profiles (internal evidence only) ----------------
    def read_profiles(self) -> list[dict]:
        try:
            _, rows = self._read_table(self._ws(WINNING_FORMAT_PROFILES_TAB))
        except gspread.WorksheetNotFound:
            return []
        return [r for r in rows if str(r.get("PROFILE_ID", "")).strip()]

    def upsert_profiles(self, profiles: list[dict]) -> tuple[int, int]:
        """Insert or update WINNING_FORMAT_PROFILES rows keyed by PROFILE_ID.
        Existing IDs are updated in place (idempotent — no duplicates); new IDs
        are appended. Returns (created, updated)."""
        if not profiles:
            return 0, 0
        ws = self._ws(WINNING_FORMAT_PROFILES_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        col = {name: i + 1 for i, name in enumerate(headers) if name}
        _, existing = self._read_table(ws)
        id_to_row = {str(r.get("PROFILE_ID", "")).strip(): r["_row"]
                     for r in existing if str(r.get("PROFILE_ID", "")).strip()}

        updates, appends = [], []
        created = updated = 0
        for p in profiles:
            pid = str(p.get("PROFILE_ID", "")).strip()
            if not pid:
                continue
            if pid in id_to_row:
                ri = id_to_row[pid]
                for name, val in p.items():
                    if name in col:
                        updates.append({
                            "range": gspread.utils.rowcol_to_a1(ri, col[name]),
                            "values": [[val]]})
                updated += 1
            else:
                appends.append([str(p.get(h, "")) for h in headers])
                created += 1
        if updates:
            ws.batch_update(updates)
        if appends:
            ws.append_rows(appends, value_input_option="RAW")
        return created, updated

    # ---- run log ----------------------------------------------------------
    def append_run(self, run: dict) -> None:
        ws = self._ws(INSPIRATION_RUNS_TAB)
        headers = [h.strip() for h in ws.row_values(1)]
        ws.append_row([str(run.get(h, "")) for h in headers],
                      value_input_option="RAW")
