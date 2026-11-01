"""Sheets mirror with a fake gspread spreadsheet."""

import pytest

from media_embed_tracer.models import ArticleCheck, FeedHealth, Occurrence, Post, RunLog
from media_embed_tracer.store import StoreError
from media_embed_tracer.store.sheets import SCOPES, TABS, V1_HEADERS, V1_TAB, SheetsMirror, spreadsheet_key


class FakeWorksheet:
    def __init__(self, title, values=None, fail=False):
        self.title, self.values, self.fail = title, [list(r) for r in (values or [])], fail
        self.options = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def row_values(self, n):
        return list(self.values[n - 1]) if len(self.values) >= n else []

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.values]

    def get_values(self, rng):
        return [list(self.values[0][:1])] if self.values else []

    def update(self, values, rng, value_input_option=None):
        self.options.append(value_input_option)
        if rng == "A1":
            if self.values:
                self.values[0] = list(values[0])
            else:
                self.values.append(list(values[0]))

    def append_rows(self, rows, value_input_option=None, insert_data_option=None):
        if self.fail:
            raise RuntimeError("APIError 429 quota")
        self.options.append(value_input_option)
        self.values.extend([list(map(str, r)) for r in rows])

    def batch_update(self, data, value_input_option=None):
        self.options.append(value_input_option)
        for d in data:
            row = int(d["range"].split(":")[0][1:])
            self.values[row - 1] = [str(v) for v in d["values"][0]]


class FakeSpreadsheet:
    def __init__(self, sheets=None):
        self.sheets = {ws.title: ws for ws in (sheets or [])}

    def worksheets(self):
        return list(self.sheets.values())

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title)
        self.sheets[title] = ws
        return ws


def occ(title="=HYPERLINK(\"http://evil\")"):
    return Occurrence("x", "1", "https://x.com/i/status/1", "", "link", "https://a", "https://a", "o", "a", "f",
                      title, "", "2026-03-01T00:00:00Z")


def post(status="pending"):
    return Post("bluesky", "at://did:plc:a/app.bsky.feed.post/1", "u", "did:plc:a", "2026-03-01T00:00:00Z",
                repost_status=status)


def write(m, **kw):
    args = dict(occurrences=[occ()], articles=[ArticleCheck("https://a", "https://a", "o", "a", "f", "t", "",
                                                             "2026-03-01T00:00:00Z", 1, 1, embeds={"x": 1})],
                posts=[post()], health=[FeedHealth("f", "u")], run=RunLog("2026-03-01T00:00:00Z", "2"))
    args.update(kw)
    m.write(**args)


def test_creates_tabs_with_headers_and_writes_raw():
    """Writes are RAW, so feed text like '=HYPERLINK(...)' is stored as text."""
    ss = FakeSpreadsheet()
    write(SheetsMirror(ss))
    assert set(ss.sheets) == set(TABS)
    for name, ws in ss.sheets.items():
        assert ws.values[0] == TABS[name].field_names()
        assert set(ws.options) == {"RAW"}
    assert ss.sheets["occurrences"].values[1][10] == '=HYPERLINK("http://evil")'
    assert ss.sheets["articles"].values[1][12] == '{"x":1}'


def test_posts_are_upserted_by_key():
    ss = FakeSpreadsheet()
    m = SheetsMirror(ss)
    write(m)
    write(m, posts=[post("posted")])
    rows = ss.sheets["posts"].values
    assert len(rows) == 2 and rows[1][6] == "posted"


def test_unexpected_headers_refused():
    """v1 layout in a tab named posts must not be overwritten."""
    ss = FakeSpreadsheet([FakeWorksheet("posts", [["Date", "Time", "Platform"]])])
    with pytest.raises(StoreError):
        write(SheetsMirror(ss))


def test_api_failure_raises_store_error():
    """API failures surface as StoreError (exit 2)."""
    ss = FakeSpreadsheet([FakeWorksheet("occurrences", [Occurrence.field_names()], fail=True)])
    with pytest.raises(StoreError):
        write(SheetsMirror(ss))


def test_scope_is_spreadsheets_only():
    """Only the spreadsheets scope, no Drive."""
    assert SCOPES == ["https://www.googleapis.com/auth/spreadsheets"]


def test_spreadsheet_key_parsing():
    assert spreadsheet_key("https://docs.google.com/spreadsheets/d/abcDEF1234567890_-xyz/edit#gid=0") == "abcDEF1234567890_-xyz"
    assert spreadsheet_key("abcDEF1234567890_-xyz") == "abcDEF1234567890_-xyz"
    with pytest.raises(StoreError):
        spreadsheet_key("not a sheet")


def test_v1_tab_keeps_filling_when_present():
    """An existing v1 "All Embeds" tab gets new rows in the v1 layout, deduplicated by post URL."""
    old = ["2026-01-01", "10:00:00", "x", "a", "", "https://old", "https://x.com/i/status/9", "", "", "", ""]
    v1 = FakeWorksheet(V1_TAB, [V1_HEADERS, old])
    ss = FakeSpreadsheet([v1])
    dup = Occurrence("x", "9", "https://x.com/i/status/9", "", "embed", "https://b", "https://b", "o", "b", "f",
                     "t", "", "2026-03-01T00:00:00Z")
    write(SheetsMirror(ss), occurrences=[occ("Title"), occ("Title"), dup])
    assert v1.values[:2] == [V1_HEADERS, old]
    assert v1.values[2:] == [["2026-03-01", "0:00:00", "twitter", "a", "", "https://a", "https://x.com/i/status/1",
                              "Title", "", "", ""]]
    assert set(v1.options) == {"RAW"}


def test_v1_tab_never_created_or_written_with_wrong_headers():
    ss = FakeSpreadsheet()
    write(SheetsMirror(ss))
    assert V1_TAB not in ss.sheets
    odd = FakeWorksheet(V1_TAB, [["Something else"]])
    ss = FakeSpreadsheet([odd])
    write(SheetsMirror(ss))
    assert odd.values == [["Something else"]]


def test_mirror_adds_new_trailing_column_to_older_occurrences_tab():
    old = Occurrence.field_names()[:-1]
    ws = FakeWorksheet("occurrences", [old])
    ws.col_count, ws.resized = len(old), None

    def resize(cols=None):
        ws.resized = cols
    ws.resize = resize
    write(SheetsMirror(FakeSpreadsheet([ws])))
    assert ws.values[0] == Occurrence.field_names() and ws.resized == len(Occurrence.field_names())
    assert len(ws.values) == 2
