"""An in-memory stand-in for the parts of gspread the sheets store uses (no network).

Every API request is recorded in FakeSpreadsheet.calls as (op, tab). Failures can be
injected per (op, tab) with FakeSpreadsheet.fail, as an APIError carrying an HTTP status.

Grid behaviour follows the real Sheets API: each worksheet has a row/column count; a read
or write whose range starts outside the grid (or a write that runs past it) fails with a
400, appends grow the grid, deleting every row (or every non-frozen row) is refused, and
returned values drop trailing empty cells and rows (gspread's get_values pads them again).
"""

from __future__ import annotations

import re

_RANGE = re.compile(r"^([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?$")


def col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


class _Response:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class APIError(Exception):
    """Shaped like gspread.exceptions.APIError: carries .response.status_code."""

    def __init__(self, status: int, message: str = "", retry_after: str | None = None) -> None:
        super().__init__(message or f"HTTP {status} with cell text 'Minister quits'")
        self.response = _Response(status, {"Retry-After": retry_after} if retry_after else None)


class FakeWorksheet:
    _ids = 0

    def __init__(self, ss: FakeSpreadsheet, title: str, values=None) -> None:
        FakeWorksheet._ids += 1
        self.ss, self.title, self.id = ss, title, FakeWorksheet._ids
        self.values: list[list[str]] = [[str(c) for c in r] for r in (values or [])]
        self.options: list = []
        # The grid, as gspread reports it from the sheet metadata.
        self.row_count = max(len(self.values), 1)
        self.col_count = max([len(r) for r in self.values] + [1])
        self.frozen_row_count = 0

    # -- helpers -----------------------------------------------------------
    def _hit(self, op: str) -> None:
        self.ss.hit(op, self.title)

    @staticmethod
    def _store(v) -> str:
        return v if isinstance(v, str) else str(v)

    def rows(self) -> int:
        """Grid rows (tests may also edit .values directly, which grows the grid)."""
        return max(self.row_count, len(self.values))

    def _check(self, rng: str, row: int, col: int, last_row: int = 0, last_col: int = 0) -> None:
        """A 400 when (row, col) .. (last_row, last_col) is not inside the grid."""
        if max(row, last_row) > self.rows() or max(col, last_col) > self.col_count:
            raise APIError(400, f"Range ('{self.title}'!{rng}) exceeds grid limits. "
                                f"Max rows: {self.rows()}, max columns: {self.col_count}")

    def _slice(self, rng: str) -> list[list[str]]:
        m = _RANGE.match(rng.split("!")[-1])
        assert m, rng
        c1, r1, c2, r2 = m.groups()
        colon = c2 is not None
        r1 = int(r1) if r1 else 1
        r2 = (int(r2) if r2 else len(self.values)) if colon else r1
        a = col_number(c1) if c1 else 1
        b = (col_number(c2) if c2 else 10_000) if colon else (a if c1 else 10_000)
        self._check(rng, r1, a)      # reads: only the start must lie inside the grid
        out = [self._trim(row[a - 1:b]) for row in self.values[r1 - 1:r2]]
        while out and not out[-1]:
            out.pop()
        return out

    @staticmethod
    def _trim(row: list[str]) -> list[str]:
        row = list(row)
        while row and row[-1] == "":
            row.pop()
        return row

    def _write(self, row: int, col: int, cells: list) -> None:
        while len(self.values) < row:
            self.values.append([])
        cur = self.values[row - 1]
        cells = [self._store(c) for c in cells]
        cur.extend([""] * (col - 1 + len(cells) - len(cur)))
        cur[col - 1:col - 1 + len(cells)] = cells

    # -- gspread API ----------------------------------------------------------
    def resize(self, rows=None, cols=None):
        self._hit("resize")
        if cols:
            self.col_count = cols
            self.values = [r[:cols] for r in self.values]
        if rows:
            self.row_count = rows
            del self.values[rows:]

    def get_all_values(self):
        self._hit("get_all_values")
        return [list(r) for r in self.values]

    def get_values(self, rng):
        """gspread's get_values pads the rows to one width and returns [[]] when empty."""
        self._hit("get_values")
        out = self._slice(rng)
        width = max([len(r) for r in out] + [0])
        return [r + [""] * (width - len(r)) for r in out] or [[]]

    def batch_get(self, ranges, value_render_option=None):
        self._hit("batch_get")
        return [self._slice(r) for r in ranges]

    def row_values(self, n):
        self._hit("row_values")
        return list(self.values[n - 1]) if len(self.values) >= n else []

    def col_values(self, n):
        self._hit("col_values")
        return [r[n - 1] if len(r) >= n else "" for r in self.values]

    def update(self, values, rng, value_input_option=None):
        self._hit("update")
        self.options.append(value_input_option)
        m = re.match(r"([A-Z]+)(\d+)", rng)
        col, row = col_number(m.group(1)), int(m.group(2))
        self._check(rng, row, col, row + len(values) - 1, col + max(len(r) for r in values) - 1)
        for i, cells in enumerate(values):
            self._write(row + i, col, cells)

    def batch_update(self, data, value_input_option=None):
        self._hit("batch_update")
        self.options.append(value_input_option)
        for d in data:
            m = re.match(r"([A-Z]+)(\d+)", d["range"])
            col, row = col_number(m.group(1)), int(m.group(2))
            self._check(d["range"], row, col, row, col + len(d["values"][0]) - 1)
        for d in data:
            m = re.match(r"([A-Z]+)(\d+)", d["range"])
            self._write(int(m.group(2)), col_number(m.group(1)), d["values"][0])

    def append_rows(self, rows, value_input_option=None, insert_data_option=None, table_range=None):
        """Like values.append on the tab (gspread's default range is the whole sheet, so the
        table search starts at A1): the API finds the first contiguous table at the start of
        the range (leading empty rows skipped, then rows with data until the first empty one)
        and writes right after it. INSERT_ROWS inserts new rows there, so rows below the
        table (a cleared row, data after a gap) move down, and the grid grows by as many rows.

        Uncertain, to verify against the real API: with a cleared row 2 under the header
        (left by a drained tab), we assume the table is the header alone, so new rows go into
        row 2 and the blank row is pushed below them, not overwritten."""
        self._hit("append_rows")
        self.options.append(value_input_option)
        assert insert_data_option == "INSERT_ROWS" and table_range is None, (insert_data_option, table_range)
        start = 0
        while start < len(self.values) and not any(self.values[start]):
            start += 1
        end = start
        while end < len(self.values) and any(self.values[end]):
            end += 1
        if end == len(self.values) == start:
            end = 0                                   # an empty tab: write from row 1
        new = [[self._store(c) for c in r] for r in rows]
        self.row_count = self.rows() + len(new)
        self.values[end:end] = new
        self.col_count = max([self.col_count] + [len(r) for r in new])
        return {"updates": {"updatedRange": f"'{self.title}'!A{end + 1}:Z{end + len(new)}"}}


class FakeSpreadsheet:
    def __init__(self, tabs: dict | None = None, *, unreadable: bool = False) -> None:
        self.sheets: dict[str, FakeWorksheet] = {}
        self.calls: list[tuple[str, str]] = []
        # (op, tab) -> HTTP statuses (or APIError objects), one per request in order
        # (0 = succeed); empty = succeed
        self.fail: dict[tuple[str, str], list[int]] = {}
        self.unreadable = unreadable
        for title, values in (tabs or {}).items():
            self.sheets[title] = FakeWorksheet(self, title, values)

    def hit(self, op: str, tab: str = "") -> None:
        self.calls.append((op, tab))
        if self.unreadable:
            raise APIError(403, "The caller does not have permission")
        queue = self.fail.get((op, tab))
        status = queue.pop(0) if queue else 0
        if isinstance(status, Exception):
            raise status
        if status:
            raise APIError(status)

    def tab(self, title: str) -> list[list[str]]:
        return self.sheets[title].values

    # -- gspread API ----------------------------------------------------------
    def worksheets(self):
        self.hit("worksheets")
        return list(self.sheets.values())

    def add_worksheet(self, title, rows, cols):
        self.hit("add_worksheet", title)
        if title in self.sheets:
            raise APIError(400, f"A sheet with the name \"{title}\" already exists")
        ws = FakeWorksheet(self, title)
        ws.row_count, ws.col_count = rows, cols
        self.sheets[title] = ws
        return ws

    def values_batch_get(self, ranges):
        self.hit("values_batch_get")
        out = []
        for r in ranges:
            title, _, rng = r.partition("!")
            ws = self.sheets[title.strip("'").replace("''", "'")]
            values = ws._slice(rng or "A1:ZZ")
            out.append({"range": r, "values": values} if values else {"range": r})   # no key when empty
        return {"valueRanges": out}

    def batch_update(self, body):
        """deleteDimension (rows) and repeatCell (clearing values), applied in order and all
        or nothing, like the real batchUpdate."""
        self.hit("ss_batch_update")
        by_id = {ws.id: ws for ws in self.sheets.values()}
        snapshot = {ws.id: ([list(r) for r in ws.values], ws.row_count) for ws in by_id.values()}
        try:
            for req in body["requests"]:
                if "repeatCell" in req:
                    d = req["repeatCell"]
                    assert d["fields"] == "userEnteredValue" and not d.get("cell"), req
                    g = d["range"]
                    ws = by_id[g["sheetId"]]
                    if g["startRowIndex"] >= ws.rows():
                        raise APIError(400, "Invalid requests[0].repeatCell: range outside the grid")
                    for i in range(g["startRowIndex"], min(g["endRowIndex"], len(ws.values))):
                        ws.values[i] = []
                    continue
                d = req["deleteDimension"]["range"]
                assert d["dimension"] == "ROWS"
                ws = by_id[d["sheetId"]]
                rows = ws.rows()
                if d["endIndex"] > rows:
                    raise APIError(400, "Invalid requests[0].deleteDimension: range outside the grid")
                left = rows - (d["endIndex"] - d["startIndex"])
                if left < 1:
                    raise APIError(400, "Invalid requests[0].deleteDimension: You can't delete all the rows "
                                        "on the sheet.")
                if left <= ws.frozen_row_count:
                    raise APIError(400, "Invalid requests[0].deleteDimension: Sorry, it is not possible to "
                                        "delete all non-frozen rows.")
                del ws.values[d["startIndex"]:d["endIndex"]]
                ws.row_count = left
        except Exception:
            for ws in by_id.values():
                ws.values, ws.row_count = snapshot[ws.id]
            raise
