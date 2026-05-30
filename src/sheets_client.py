"""Google Sheets I/O for the Storelli POC sheet (two-row header).

Sheet shape:
  row 1 = category group headers (HOOK, FORMAT, ... forward-filled across cols)
  row 2 = actual column names (metadata names + bare taxonomy option labels)
  row 3+ = data

A taxonomy column is identified by the (category, option) PAIR, because bare
option labels collide across categories (e.g. "None" under both CONVERSION
and PRODUCT PRESENCE). We map only the 9 AI-tagged layers; the sheet's ICP
and PRODUCT one-hot groups are left untouched (ICP/Product stay grouping-only).

Guardrails:
- Only taxonomy (1/0) cells, Status, and blank ICP/Product are ever written.
- Raw human columns (ID, LINK, PERFORMANCE, Storytelling structure) are never
  written.
- Taxonomy cells are written only when currently empty, unless reprocess=True.
"""
from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

import config
import taxonomy
from logger import get_logger

log = get_logger()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Required metadata columns (row 2, no category in row 1).
REQUIRED_META = ["LINK", "PERFORMANCE", "Status"]
STATUS_DONE = "completed"


class SheetsClient:
    def __init__(self):
        config.require_sheets()
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON_PATH, scopes=SCOPES
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
        self.ws = sh.worksheet(config.GOOGLE_WORKSHEET_NAME)
        self.values = self.ws.get_all_values()
        self.meta_col: dict[str, int] = {}      # name -> 1-based col
        self.signal_col: dict[str, int] = {}    # signal key -> 1-based col
        self._build_model()

    # ---- header model --------------------------------------------------
    def _build_model(self) -> None:
        if len(self.values) < 2:
            raise RuntimeError("Sheet has fewer than 2 header rows; expected the "
                               "POC two-row header (categories + options).")
        categories = list(self.values[0])
        headers = list(self.values[1])
        ffilled = self._ffill(categories)

        for i, header in enumerate(headers):
            name = header.strip()
            if not name:
                continue
            category = ffilled[i]
            col = i + 1  # 1-based
            layer = taxonomy.category_to_layer(category) if category else None
            if not category:
                # metadata column
                self.meta_col[name] = col
            elif layer:
                label = self._match_label(layer, name)
                if label:
                    self.signal_col[taxonomy.column_for(layer, label)] = col
                else:
                    log.warning("Unmapped option '%s' under category '%s'", name, category)
            # else: ICP / PRODUCT taxonomy groups -> intentionally ignored

    @staticmethod
    def _ffill(categories: list[str]) -> list[str]:
        out, last = [], ""
        for c in categories:
            cs = (c or "").strip()
            if cs:
                last = cs
            out.append(last)
        # leading columns before the first category must stay blank
        first_idx = next((i for i, c in enumerate(categories) if (c or "").strip()), len(categories))
        for i in range(first_idx):
            out[i] = ""
        return out

    @staticmethod
    def _match_label(layer: str, header: str) -> str | None:
        target = taxonomy.slug(header)
        for canonical in taxonomy.LAYERS[layer]:
            if taxonomy.slug(canonical) == target:
                return canonical
        return None

    def validate_columns(self) -> None:
        missing = [c for c in REQUIRED_META if c not in self.meta_col]
        if missing:
            raise RuntimeError("POC sheet is missing required column(s): "
                               + ", ".join(missing))
        expected = set(taxonomy.all_signal_columns())
        found = set(self.signal_col)
        gap = expected - found
        if gap:
            log.warning("%d taxonomy column(s) not found in sheet (will skip): %s",
                        len(gap), sorted(gap))

    # ---- reading -------------------------------------------------------
    def read_rows(self) -> list[dict]:
        """Each row: metadata by name + signal keys (raw cell text) + _row."""
        rows = []
        for offset, raw in enumerate(self.values[2:]):
            sheet_row = offset + 3
            rec: dict = {"_row": sheet_row}
            for name, col in self.meta_col.items():
                rec[name] = raw[col - 1] if col - 1 < len(raw) else ""
            for key, col in self.signal_col.items():
                rec[key] = raw[col - 1] if col - 1 < len(raw) else ""
            rows.append(rec)
        return rows

    @staticmethod
    def should_process(row: dict, reprocess: bool = False) -> bool:
        link = str(row.get("LINK", "")).strip()
        if not link:
            return False
        perf = str(row.get("PERFORMANCE", "")).strip().lower()
        if perf == "non classified":
            return False  # explicit human skip; never reprocess these
        # Blank PERFORMANCE is now eligible — the runner will try to compute it
        # from views/followers; if that fails the row is flagged needs_review.
        if reprocess:
            return True
        status = str(row.get("Status", "")).strip().lower()
        return status in ("", "pending")

    @staticmethod
    def is_analyzed(row: dict) -> bool:
        """True if any taxonomy cell is already tagged 1."""
        return any(str(row.get(c, "")).strip() == "1"
                   for c in taxonomy.all_signal_columns())

    # ---- writing -------------------------------------------------------
    def plan_writes(self, row_index: int, existing_row: dict, signal_values: dict,
                    reprocess: bool = False, icp_fill: str = "",
                    product_fill: str = "", status_value: str = STATUS_DONE,
                    performance_value: str = "") -> list[dict]:
        """Pure: compute the batch_update payload without touching the network.

        Writes: taxonomy (1/0) cells (empty-only unless reprocess), Status,
        ICP/Product only when blank, and PERFORMANCE only when blank or
        reprocess. Never touches ID / LINK / Storytelling structure.
        """
        updates = []

        def _cell(col, value):
            updates.append({"range": gspread.utils.rowcol_to_a1(row_index, col),
                            "values": [[value]]})

        for key, val in signal_values.items():
            col = self.signal_col.get(key)
            if not col:
                continue  # meta keys (primary_*, suggestions) are not signal cols
            if not reprocess and str(existing_row.get(key, "")).strip() != "":
                continue  # only fill empty taxonomy cells
            _cell(col, val)

        if "Status" in self.meta_col:
            _cell(self.meta_col["Status"], status_value)

        if icp_fill and "ICP" in self.meta_col and not str(existing_row.get("ICP", "")).strip():
            _cell(self.meta_col["ICP"], icp_fill)
        if product_fill and "Product" in self.meta_col and not str(existing_row.get("Product", "")).strip():
            _cell(self.meta_col["Product"], product_fill)

        if performance_value and "PERFORMANCE" in self.meta_col:
            existing_perf = str(existing_row.get("PERFORMANCE", "")).strip()
            if not existing_perf or reprocess:
                _cell(self.meta_col["PERFORMANCE"], performance_value)

        return updates

    def write_row(self, row_index: int, existing_row: dict, signal_values: dict,
                  reprocess: bool = False, icp_fill: str = "",
                  product_fill: str = "", status_value: str = STATUS_DONE,
                  performance_value: str = "") -> None:
        updates = self.plan_writes(row_index, existing_row, signal_values,
                                   reprocess, icp_fill, product_fill,
                                   status_value, performance_value)
        if updates:
            self.ws.batch_update(updates)

    def set_status(self, row_index: int, value: str) -> None:
        if "Status" in self.meta_col:
            self.ws.update(
                range_name=gspread.utils.rowcol_to_a1(row_index, self.meta_col["Status"]),
                values=[[value]],
            )
