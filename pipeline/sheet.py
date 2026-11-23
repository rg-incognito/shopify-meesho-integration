"""Google Sheet as the product tracker. One row per Meesho product, keyed by meesho_id.

Columns you can edit by hand: status, price_override, notes, collections (comma-separated titles).
"""
import logging
from datetime import datetime

import gspread
from gspread.utils import rowcol_to_a1

from .config import CFG, env

log = logging.getLogger(__name__)

COLUMNS = [
    "meesho_id", "status", "preview", "shopify_title", "my_price", "compare_at", "price_override",
    "meesho_price", "in_stock", "sizes", "category", "meesho_title", "meesho_url", "ai_images",
    "shopify_product_id", "shopify_admin_url", "added_by", "added_at", "last_scraped", "last_pushed",
    "notes", "error", "collections", "shopify_status",
]
STATUSES = ["NEW", "PROCESSING", "READY", "APPROVED", "PUSHED", "REJECTED", "UNAVAILABLE", "ERROR"]


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class Sheet:
    def __init__(self):
        gc = gspread.service_account(filename=env("GOOGLE_SERVICE_ACCOUNT_FILE"))
        book = gc.open_by_key(env("GOOGLE_SHEET_ID"))
        name = CFG["sheet"]["worksheet"]
        try:
            self.ws = book.worksheet(name)
        except gspread.WorksheetNotFound:
            self.ws = book.add_worksheet(name, rows=1000, cols=len(COLUMNS))
        self._ensure_header()

    def _ensure_header(self):
        header = self.ws.row_values(1)
        if header[: len(COLUMNS)] == COLUMNS:
            return
        if any(header) and COLUMNS[: len(header)] == header:
            # older sheet: append the newer columns on the right
            if self.ws.col_count < len(COLUMNS):
                self.ws.add_cols(len(COLUMNS) - self.ws.col_count)
            self.ws.update(range_name="A1", values=[COLUMNS])
            return
        if any(header):
            raise RuntimeError(f"Sheet header does not match the expected columns: {COLUMNS}")
        self.ws.update(range_name="A1", values=[COLUMNS])
        self.ws.freeze(rows=1, cols=2)
        col = COLUMNS.index("status")
        self.ws.spreadsheet.batch_update({"requests": [
            {"repeatCell": {
                "range": {"sheetId": self.ws.id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold"}},
            {"setDataValidation": {
                "range": {"sheetId": self.ws.id, "startRowIndex": 1, "startColumnIndex": col,
                          "endColumnIndex": col + 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": s} for s in STATUSES]},
                         "showCustomUi": True, "strict": True}}},
        ]})

    def _row_number(self, meesho_id: str) -> int | None:
        ids = self.ws.col_values(1)
        return ids.index(meesho_id) + 1 if meesho_id in ids else None

    def all(self) -> list[dict]:
        return self.ws.get_all_records(expected_headers=COLUMNS, value_render_option="FORMULA")

    def get(self, meesho_id: str) -> dict | None:
        r = self._row_number(meesho_id)
        if not r:
            return None
        values = self.ws.row_values(r, value_render_option="FORMULA")
        return dict(zip(COLUMNS, values + [""] * (len(COLUMNS) - len(values))))

    def by_status(self, status: str) -> list[dict]:
        return [row for row in self.all() if row["status"] == status]

    def upsert(self, meesho_id: str, **fields) -> None:
        """Writes only the given fields, leaving the rest of the row (and your edits) intact."""
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise KeyError(f"Unknown sheet columns: {unknown}")
        r = self._row_number(meesho_id)
        if r is None:
            row = {c: "" for c in COLUMNS}
            row.update(meesho_id=meesho_id, added_at=now(), **fields)
            self.ws.append_row([row[c] for c in COLUMNS], value_input_option="USER_ENTERED",
                               table_range="A1")
            return
        self.ws.batch_update(
            [{"range": rowcol_to_a1(r, COLUMNS.index(k) + 1), "values": [[v]]} for k, v in fields.items()],
            value_input_option="USER_ENTERED",
        )

    def counts(self) -> dict:
        out = {}
        for row in self.all():
            out[row["status"]] = out.get(row["status"], 0) + 1
        return out
