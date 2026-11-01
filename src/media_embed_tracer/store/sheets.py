"""Google Sheets helpers and the mirror used in the JSONL storage modes.

In private and public mode `SheetsMirror` is an optional copy, written once at the end of
each run and never read for decisions. In sheets mode `SheetsStore` (sheets_store.py)
uses the same spreadsheet as the bot's memory and dataset instead.

Mirror:

Tabs (created with a header row if missing): occurrences, articles, posts, feed_health,
runs. Headers are the model field names. occurrences/articles/runs are appended; posts
and feed_health are upserted by key. All writes use valueInputOption=RAW, so text from
feeds or articles is never interpreted as a formula. OAuth scope: spreadsheets only
(the sheet is opened by key and must be shared with the service account).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from ..models import TEMPLATE_SUFFIX, ArticleCheck, FeedHealth, Occurrence, Post, Record, RunLog
from ..util import canonical_article_url, parse_iso
from .base import StoreError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HTTP_TIMEOUT = 60   # seconds per Sheets request
TABS: dict[str, type[Record]] = {
    "occurrences": Occurrence,
    "articles": ArticleCheck,
    "posts": Post,
    "feed_health": FeedHealth,
    "runs": RunLog,
}
_CELL_LIMIT = 49_000
# Fields added to a model after its first layout (always last); older tabs gain the column.
MIRROR_ADDED_COLUMNS = {"occurrences": ["article_summary"]}
# v1 layout, kept filling so existing sheets, formulas and charts keep working.
V1_TAB = "All Embeds"
V1_HEADERS = ["Date", "Time", "Platform", "Domain", "Author Handle", "Article URL", "Post URL",
              "Article Title", "Article Summary", "Published Date", "Repost Status"]
# v1 cell formats (UTC): Date 2026-11-01, Time 0:27:38, Published Date 2026-10-29 8:00 (hours
# without a leading zero), Platform "twitter" for X, Bluesky author as a bare handle.
V1_PLATFORMS = {"x": "twitter"}
# Form of a Bluesky "Post URL" (column G) in new All Embeds rows. "did" (default):
# always https://bsky.app/profile/<did>/post/<rkey> (stable, matched offline). "handle" (as
# v1): https://bsky.app/profile/<verified handle>/post/<rkey>, the DID when no handle is
# verified. The verified handle goes to Author Handle (column E) in both forms, and existing
# rows in either form are matched. Flip it here.
V1_POST_URL_FORM = "did"   # or "handle"
_KEY_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def spreadsheet_key(url_or_key: str) -> str:
    m = _KEY_RE.search(url_or_key)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", url_or_key.strip()):
        return url_or_key.strip()
    raise StoreError("SPREADSHEET_URL is neither a Google Sheets URL nor a spreadsheet id", safe=True)


def _cell(value):
    if isinstance(value, int):
        return value
    return ("" if value is None else str(value))[:_CELL_LIMIT]


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def open_spreadsheet(url_or_key: str, credentials_json: str):
    """Authorise with a service account JSON string and open the spreadsheet by key."""
    import gspread  # optional dependency: pip install '.[sheets]'

    try:
        info = json.loads(credentials_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StoreError("GOOGLE_CREDENTIALS_JSON is not valid JSON", safe=True) from exc
    try:
        client = gspread.service_account_from_dict(info, scopes=SCOPES)
        client.set_timeout(HTTP_TIMEOUT)   # a hung request must not eat the job's time limit
        return client.open_by_key(spreadsheet_key(url_or_key))
    except StoreError:
        raise
    except Exception as exc:
        # Class name only: the message can echo the sheet id or cell values.
        raise StoreError(f"cannot open spreadsheet: {type(exc).__name__}", safe=True) from exc


def _v1_datetime(value: str, seconds: bool) -> tuple[str, str]:
    """(date, time) in the v1 formats from an ISO timestamp; other values are kept as date."""
    dt = parse_iso(value)
    if dt is None:
        return value, ""
    return dt.strftime("%Y-%m-%d"), f"{dt.hour}:{dt:%M:%S}" if seconds else f"{dt.hour}:{dt:%M}"


def v1_row(o: Occurrence, status: str, handle: str = "") -> list:
    """One "All Embeds" row in the v1 formats. `handle`: a verified Bluesky handle for the
    post's DID, used for Author Handle and, with V1_POST_URL_FORM "handle", Post URL (v1
    wrote handles); else the DID."""
    date, time = _v1_datetime(o.first_seen, seconds=True)
    pub_date, pub_time = _v1_datetime(o.article_published, seconds=False)
    author, post_url = o.author, o.post_url
    if o.platform == "bluesky" and handle:
        author = handle
        if V1_POST_URL_FORM == "handle":
            post_url = f"https://bsky.app/profile/{handle}/post/{o.canonical_id.rstrip('/').rsplit('/', 1)[-1]}"
    return [date, time, V1_PLATFORMS.get(o.platform, o.platform), o.domain, author, o.article_url, post_url,
            o.article_title, o.article_summary, f"{pub_date} {pub_time}".strip(), status]


def v1_article(url: str) -> str:
    """Article URL as compared for All Embeds dedup (column F, or an occurrence's URLs)."""
    return canonical_article_url(url) if url else ""


def v1_wanted(o: Occurrence) -> bool:
    """Page furniture (embed_type "<type>+template") gets no All Embeds row."""
    return TEMPLATE_SUFFIX not in o.embed_type


def v1_rows(header: list[str], seen: set[tuple[str, str]], occurrences, status: dict,
            handle_of: Callable[[str], str] | None = None) -> list[list] | None:
    """Rows to append to an existing v1 "All Embeds" tab, in the v1 layout: one row per
    (article, post URL), as v1, deduplicated against `seen` ((v1_article(F), G) pairs) and
    without page furniture. None when the headers do not match. `handle_of(did)` returns a
    Bluesky handle or ''."""
    if header[: len(V1_HEADERS)] != V1_HEADERS:
        return None
    rows = []
    for o in occurrences:
        if not v1_wanted(o):
            continue
        articles = {v1_article(o.article_url), v1_article(o.final_url)}
        if any((a, o.post_url) in seen for a in articles):
            continue
        seen.update((a, o.post_url) for a in articles)
        handle = handle_of(o.author) if handle_of and o.platform == "bluesky" and o.author else ""
        rows.append(v1_row(o, status.get((o.platform, o.canonical_id), ""), handle))
    return rows


class SheetsMirror:
    name = "sheets"

    def __init__(self, spreadsheet) -> None:
        self.ss = spreadsheet

    def _worksheet(self, tab: str, existing: dict):
        headers = TABS[tab].field_names()
        ws = existing.get(tab)
        if ws is None:
            ws = self.ss.add_worksheet(title=tab, rows=100, cols=len(headers))
            ws.update([headers], "A1", value_input_option="RAW")
            return ws, []
        values = ws.get_all_values() if tab in ("posts", "feed_health") else [ws.row_values(1)]
        if not values or not values[0]:
            ws.update([headers], "A1", value_input_option="RAW")
            return ws, []
        added = MIRROR_ADDED_COLUMNS.get(tab, [])
        if added and values[0] == headers[: len(headers) - len(added)]:
            # An older layout: add the new header cells (and grid columns) at the end.
            if getattr(ws, "col_count", len(headers)) < len(headers):
                ws.resize(cols=len(headers))
            ws.update([headers], "A1", value_input_option="RAW")
            return ws, values[1:]
        if tab in ("posts", "feed_health") and values[0][: len(headers)] != headers:
            raise StoreError(f"sheets: tab {tab} has unexpected headers; refusing to write (see README)", safe=True)
        return ws, values[1:]

    def _append(self, ws, rows: list[list]) -> None:
        if rows:
            ws.append_rows([[_cell(v) for v in r] for r in rows], value_input_option="RAW",
                           insert_data_option="INSERT_ROWS")

    def _upsert(self, tab: str, ws, existing_rows: list[list], items: list, key_of, key_cols: int) -> None:
        index = {tuple(r[:key_cols]): i for i, r in enumerate(existing_rows, start=2)}
        width = len(TABS[tab].field_names())
        updates, new = [], []
        for item in items:
            row = item.to_row()
            k = key_of(item)
            if k in index:
                updates.append({"range": f"A{index[k]}:{_col(width)}{index[k]}", "values": [[_cell(v) for v in row]]})
            else:
                new.append(row)
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        self._append(ws, new)

    def _append_v1(self, ws, occurrences, posts) -> None:
        """Append new occurrences to an existing v1 "All Embeds" tab (never created here).
        Like v1: one row per (article, post URL), deduplicated against columns F and G."""
        status = {(p.platform, p.canonical_id): p.repost_status for p in posts}
        values = ws.get_all_values()
        seen = {(v1_article(str(r[5])), str(r[6])) for r in values[1:] if len(r) > 6}
        rows = v1_rows(values[0] if values else [], seen, occurrences, status)
        if rows is None:
            log.warning("sheets: %r has unexpected headers; not writing v1 rows", V1_TAB)
            return
        self._append(ws, rows)

    def write(self, *, occurrences, articles, posts, health, run) -> None:
        try:
            existing = {ws.title: ws for ws in self.ss.worksheets()}
            for tab, rows in (("occurrences", [o.to_row() for o in occurrences]),
                              ("articles", [a.to_row() for a in articles]),
                              ("runs", [run.to_row()])):
                ws, _ = self._worksheet(tab, existing)
                self._append(ws, rows)
            ws, rows = self._worksheet("posts", existing)
            self._upsert("posts", ws, rows, list(posts), lambda p: (p.platform, p.canonical_id), 2)
            ws, rows = self._worksheet("feed_health", existing)
            self._upsert("feed_health", ws, rows, list(health), lambda h: (h.feed,), 1)
            if V1_TAB in existing:
                self._append_v1(existing[V1_TAB], occurrences, posts)
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"sheets mirror write failed: {type(exc).__name__}", safe=True) from exc
