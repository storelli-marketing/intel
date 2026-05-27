"""Google Sheets read/write via gspread service account.

Guardrails:
- RAW columns (user-provided) are never written.
- Only taxonomy signal columns + AI meta columns are ever written.
- Missing required columns -> clear error.
"""
from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

import config
import taxonomy
from logger import get_logger

log = get_logger()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

REQUIRED_COLUMNS = [
    "ig_link",
    "product",
    "icp",
    "views",
    "reach",
    "likes",
    "comments",
    "shares",
    "saves",
    "date_posted",
    "processed_status",
]

# Raw, user-owned columns we must never overwrite.
RAW_COLUMNS = set(REQUIRED_COLUMNS) - {"processed_status"}


class SheetsClient:
    def __init__(self):
        config.require_sheets()
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH, scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        self.ws = sh.worksheet(config.GOOGLE_WORKSHEET_NAME)
        self._header = None

    # ---- reading -------------------------------------------------------
    def header(self) -> list[str]:
        if self._header is None:
            self._header = self.ws.row_values(1)
        return self._header

    def validate_columns(self) -> None:
        header = self.header()
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise RuntimeError(
                "Google Sheet is missing required column(s): "
                + ", ".join(missing)
            )

    def read_rows(self) -> list[dict]:
        """Return list of row dicts, each augmented with a 1-based _row index."""
        records = self.ws.get_all_records()
        rows = []
        for i, rec in enumerate(records, start=2):  # row 1 is header
            rec["_row"] = i
            rows.append(rec)
        return rows

    @staticmethod
    def is_pending(row: dict) -> bool:
        status = str(row.get("processed_status", "")).strip().lower()
        return status in ("", "pending")

    # ---- writing -------------------------------------------------------
    def ensure_columns(self, columns: list[str]) -> None:
        """Append any missing output columns to the header (never raw cols)."""
        header = self.header()
        to_add = [c for c in columns if c not in header]
        # Never create a column that collides with a raw column name.
        to_add = [c for c in to_add if c not in RAW_COLUMNS]
        if not to_add:
            return
        new_header = header + to_add
        self.ws.update(
            range_name="A1",
            values=[new_header],
        )
        self._header = new_header
        log.info("Added %d output column(s) to sheet: %s", len(to_add), to_add)

    def _col_letter(self, col_name: str) -> str | None:
        header = self.header()
        if col_name not in header:
            return None
        return gspread.utils.rowcol_to_a1(1, header.index(col_name) + 1).rstrip("1")

    def write_row_outputs(self, row_index: int, values: dict) -> None:
        """Write a dict of {column_name: value} into a single sheet row.

        Refuses to write any raw user column. Builds a batch update so each
        run touches the sheet minimally (idempotent per row).
        """
        header = self.header()
        updates = []
        for col_name, val in values.items():
            if col_name in RAW_COLUMNS:
                log.warning("Refusing to overwrite raw column '%s'", col_name)
                continue
            if col_name not in header:
                log.warning("Column '%s' not in header, skipping", col_name)
                continue
            col_idx = header.index(col_name) + 1
            a1 = gspread.utils.rowcol_to_a1(row_index, col_idx)
            updates.append({"range": a1, "values": [[val]]})

        if updates:
            self.ws.batch_update(updates)
