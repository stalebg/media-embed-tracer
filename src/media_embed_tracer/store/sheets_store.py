"""Google Sheets as the primary store (sheets storage mode).

The spreadsheet is both the bot's memory and the research dataset. Nothing is pushed to git.

Tabs (created with a header row when missing; headers are validated at load):
  occurrences     one row per (article, post), appended                       dataset
  articles        one row per article check (the denominator), appended       dataset
  posts           one row per canonical post, upserted; repost_status is the  dataset + state
                  source of truth for posting
  article_counts  one row per (UTC date, feed), upserted: daily denominator    dataset
  runs            one row per run, appended                                    dataset
  feed_health     one row per feed, upserted                                   state
  _seen           seen articles, upserted; rows older than SEEN_RETENTION_DAYS state
                  are deleted
  _queue          unresolved hits waiting for a lookup, kept equal to the queue state
  _meta           key/value: instance marker, v1 import marker                state
  All Embeds      the v1 tab: appended in the v1 layout at the end of the run, after the runs
                  row, if it already exists (never created); Repost Status of its rows
                  follows later changes. A failed write is a warning: occurrences is the
                  source of truth

Load reads all state tabs in one batched request. It refuses to run on unexpected headers
or when `_meta` names another instance. On the first run with a v1 tab, its rows seed the
state once (no dataset rows), marked in `_meta`.

Writes are batched per tab and RAW: one key read, then deletes (bottom-up), updates and
appends, all placed from that read. Ranges always start at row 1 (a range that starts below
the last grid row is a 400), new tabs get NEW_TAB_ROWS rows, and a delete never removes the
last data row (it is cleared instead), so a drained tab keeps a row below its header.
Transient errors are retried with backoff and jitter, honouring Retry-After, for about 90 s
per request. Appends retry on 429 only, deletes never.

A `posted` status is written at once; other status changes wait for `append_run`.
Above 80 % of the 10M cell limit a warning is logged. Errors carry the exception class only
(StoreError(..., safe=True), so redact mode still shows which request failed).
"""

from __future__ import annotations

import bisect
import functools
import logging
import math
import os
import random
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

import requests

from .. import repost
from ..migrate import convert_v1
from ..models import (
    DETECTOR_VERSION,
    EMBED_MARKUP_TYPES,
    ArticleCheck,
    FeedHealth,
    Occurrence,
    Post,
    QueuedHit,
    Record,
    RunLog,
    SeenArticle,
    StoreState,
)
from ..platforms import PLATFORM_NAMES, bluesky, match_url
from ..util import iso, utcnow
from .base import StoreError, seen_expired
from .sheets import V1_HEADERS, V1_TAB, _cell, _col, v1_rows

log = logging.getLogger(__name__)

SEEN_TAB, QUEUE_TAB, COUNTS_TAB, META_TAB = "_seen", "_queue", "article_counts", "_meta"
ARTICLE_COUNT_HEADERS = ["date", "feed", "outlet", "articles_checked", "fetch_ok", "articles_with_any_embed",
                         "articles_with_embed_markup", *(f"embeds_{p}" for p in PLATFORM_NAMES),
                         "detector_version"]
_COUNT_FIELDS = ARTICLE_COUNT_HEADERS[3:-1]
# tab -> (headers, number of leading key columns)
TABS: dict[str, tuple[list[str], int]] = {
    "occurrences": (Occurrence.field_names(), 0),
    "articles": (ArticleCheck.field_names(), 0),
    "posts": (Post.field_names(), 2),
    COUNTS_TAB: (ARTICLE_COUNT_HEADERS, 2),
    "runs": (RunLog.field_names(), 0),
    "feed_health": (FeedHealth.field_names(), 1),
    SEEN_TAB: (SeenArticle.field_names(), 1),
    QUEUE_TAB: (QueuedHit.field_names(), 3),   # url, embed_type, article_url
    META_TAB: (["key", "value"], 1),
}
# Tabs read in full at load (the state); the others only have their header row checked.
STATE_TABS: dict[str, type[Record] | None] = {"posts": Post, "feed_health": FeedHealth, SEEN_TAB: SeenArticle,
                                              QUEUE_TAB: QueuedHit, META_TAB: None}
# Columns added to a model after its first layout (always at the end). A tab whose header is
# the older layout gets the new header cells instead of being refused.
ADDED_COLUMNS: dict[str, list[str]] = {"occurrences": ["article_summary"], QUEUE_TAB: ["article_summary"]}
NEW_TAB_ROWS = 100   # grid rows of a tab created by the store (header + room)
CELL_LIMIT = 10_000_000
CELL_WARN_SHARE = 0.8
RETRY_BUDGET_SECONDS = 90.0
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_V1_STATUS_COL = "K"
_V1_URL_COL = 6        # 0-based index of "Post URL" (column G)

# _call retry policies
RETRY_ALL, RETRY_429, RETRY_NONE = "all", "429", "none"


def _status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _transient(exc: Exception) -> bool:
    return _status(exc) in _TRANSIENT_STATUS or isinstance(exc, (ConnectionError, TimeoutError,
                                                                requests.ConnectionError, requests.Timeout))


def _retry_after(exc: Exception) -> float:
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return max(float(headers.get("Retry-After", 0) or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0   # an HTTP date: fall back to the backoff


def _key(row: list, n: int) -> tuple:
    return tuple(str(v) for v in (list(row) + [""] * n)[:n])


def _guarded(method):
    """Turn unexpected exceptions (a malformed API response, say) into StoreError with the
    class name only, so the pipeline maps them to exit 2 instead of crashing mid-run."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"sheets: {method.__name__} failed: {type(exc).__name__}", safe=True) from exc
    return wrapper


def _quote(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _rkey(canonical_id: str) -> str:
    return canonical_id.rstrip("/").rsplit("/", 1)[-1]


class SheetsStore:
    name = "sheets"

    def __init__(self, spreadsheet, seen_retention_days: float = 30, now: datetime | None = None, *,
                 instance: str = "", resolver=None, min_interval: float = 1.1, max_attempts: int = 8,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 rand: Callable[[], float] = random.random, write_v1: bool = True, v1_resolver=None,
                 deadline: float = math.inf) -> None:
        self.ss = spreadsheet
        self.seen_retention = timedelta(days=seen_retention_days)
        self._now = now
        self.instance = instance.strip()
        self.resolver = resolver   # capped Resolver for the one-time v1 import (None: offline only)
        # Own small cap for All Embeds handle lookups (new rows, dedup); none after `deadline`
        # (a time.monotonic() value, via `clock`). None: offline only.
        self.v1_resolver = v1_resolver
        self.deadline = deadline
        self.min_interval, self.max_attempts = min_interval, max_attempts
        self._sleep, self._clock, self._rand = sleep, clock, rand
        self.write_v1 = write_v1
        self._last_call = float("-inf")
        self._ws: dict = {}
        self._needs_header: set[str] = set()
        self._grid: dict[str, list[int]] = {}   # tab -> [rows, cols], metadata + rows added
        self._state: StoreState | None = None
        self._dirty: dict[str, Post] = {}
        self._touched: set[str] = set()         # post keys changed this run (for v1 statuses)
        self._meta: dict[str, str] = {}
        self._meta_pending: dict[str, str] = {}
        self._seen_extra: list[SeenArticle] = []   # seeded from the v1 tab, written by save
        self._v1_values: list[list[str]] | None = None
        self._v1_pending: list[Occurrence] = []   # this run's occurrences, for append_run
        self._v1_unwritten = 0                    # rows a failed All Embeds write left out
        self._handle_did: dict[str, str] = {}     # handle -> DID resolved this run ('' = not)

    # --- API calls -----------------------------------------------------------
    def _call(self, op: str, fn, *args, retry: str = RETRY_ALL, **kwargs):
        """One Sheets request, rate-limited. Transient failures are retried (RETRY_ALL), only
        on 429 (RETRY_429: for appends, which a 5xx may have applied), or never (RETRY_NONE:
        row deletes). Backoff: max(Retry-After, 5 s x 2^n) plus jitter, ~90 s in total."""
        slept = 0.0
        for attempt in range(1, self.max_attempts + 1):
            wait = self.min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                allowed = (retry == RETRY_ALL and _transient(exc)) or (retry == RETRY_429 and _status(exc) == 429)
                delay = max(_retry_after(exc), 5.0 * 2 ** (attempt - 1)) + self._rand() * 2
                if allowed and attempt < self.max_attempts and slept + delay <= RETRY_BUDGET_SECONDS:
                    log.warning("sheets: %s failed (%s); retry %d in %.0fs", op, type(exc).__name__, attempt, delay)
                    self._sleep(delay)
                    slept += delay
                    continue
                raise StoreError(f"sheets: {op} failed: {type(exc).__name__}", safe=True) from exc
            finally:
                self._last_call = self._clock()
        raise AssertionError("unreachable")  # pragma: no cover

    def _tab(self, tab: str):
        """The worksheet for a known tab, created with its header row if missing."""
        ws = self._ws.get(tab)
        headers = TABS[tab][0]
        if ws is None:
            ws = self._call(f"create {tab}", self.ss.add_worksheet, title=tab, rows=NEW_TAB_ROWS,
                            cols=len(headers), retry=RETRY_429)
            self._ws[tab] = ws
            self._grid[tab] = [NEW_TAB_ROWS, len(headers)]
            self._needs_header.add(tab)
        if tab in self._needs_header:
            grid = self._grid.setdefault(tab, [1, len(headers)])
            if grid[1] < len(headers):   # an older layout: make room for the new columns
                self._call(f"widen {tab}", ws.resize, cols=len(headers))
                grid[1] = len(headers)
            self._call(f"header {tab}", ws.update, [headers], "A1", value_input_option="RAW")
            self._needs_header.discard(tab)
        return ws

    def _grow(self, tab: str, rows: int) -> None:
        default_cols = len(TABS[tab][0]) if tab in TABS else len(V1_HEADERS)
        grid = self._grid.setdefault(tab, [0, default_cols])
        grid[0] = max(grid[0] + rows, 0)

    def cells_used(self) -> int:
        """Grid cells across all tabs: metadata from load plus rows added or deleted since."""
        return sum(r * c for r, c in self._grid.values())

    def check_cells(self) -> None:
        used = self.cells_used()
        if used <= CELL_LIMIT * CELL_WARN_SHARE:
            return
        msg = (f"spreadsheet at {100 * used // CELL_LIMIT}% of the 10M cell limit ({used} cells); "
               "archive or start a new sheet")
        log.warning("%s", msg)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning::{msg}", flush=True)

    # --- reading -----------------------------------------------------------
    @_guarded
    def load(self) -> StoreState:
        self._ws = {ws.title: ws for ws in self._call("list tabs", self.ss.worksheets)}
        self._grid = {t: [int(getattr(ws, "row_count", 0) or 0), int(getattr(ws, "col_count", 0) or 0)]
                      for t, ws in self._ws.items()}
        full = [t for t in STATE_TABS if t in self._ws]
        header_only = [t for t in TABS if t in self._ws and t not in STATE_TABS]
        v1 = [V1_TAB] if V1_TAB in self._ws and self.write_v1 else []
        names = full + header_only + v1
        ranges = ([_quote(t) for t in full] + [f"{_quote(t)}!1:1" for t in header_only]
                  + [f"{_quote(t)}!A1:K" for t in v1])
        values: dict[str, list[list]] = {}
        if ranges:
            resp = self._call("read state", self.ss.values_batch_get, ranges)
            got = resp.get("valueRanges", []) if isinstance(resp, dict) else []
            if len(got) != len(ranges):
                raise StoreError("sheets: read state failed: incomplete response", safe=True)
            values = {t: (vr.get("values") or []) for t, vr in zip(names, got, strict=True)}
        self._v1_values = values.pop(V1_TAB, None)
        self._needs_header = set()
        for tab, rows in values.items():
            headers = TABS[tab][0]
            if not rows or not any(str(c).strip() for c in rows[0]):
                if len(rows) > 1:
                    raise StoreError(f"sheets: tab {tab} has rows but no header row; refusing to run", safe=True)
                self._needs_header.add(tab)    # an empty tab: the header is written on first use
            elif [str(c) for c in rows[0]] == headers[: len(headers) - len(ADDED_COLUMNS.get(tab, []))] \
                    and ADDED_COLUMNS.get(tab):
                self._needs_header.add(tab)    # an older layout: the new header cells are added
            elif [str(c) for c in rows[0][: len(headers)]] != headers:
                raise StoreError(f"sheets: tab {tab} has unexpected headers; refusing to run "
                                 "(rename the tab or restore its header row)", safe=True)
        self._meta = {str(r[0]): str(r[1]) if len(r) > 1 else "" for r in values.get(META_TAB, [])[1:] if r and r[0]}
        theirs = self._meta.get("instance", "")
        if theirs and self.instance and theirs != self.instance:
            raise StoreError("sheets: this spreadsheet holds the state of another instance (INSTANCE_NAME "
                             "differs from _meta); refusing to run (give each instance its own sheet)", safe=True)
        if self.instance and not theirs:
            self._meta_pending["instance"] = self.instance
        st = StoreState()
        for tab, cls in STATE_TABS.items():
            if cls is None:
                continue
            n = TABS[tab][1]
            for row in values.get(tab, [])[1:]:
                if not any(_key(row, n)):
                    continue
                rec = cls.from_row(row)
                if isinstance(rec, Post):
                    st.posts[rec.key] = rec
                elif isinstance(rec, FeedHealth):
                    st.health[rec.feed] = rec
                elif isinstance(rec, SeenArticle):
                    st.seen[rec.url] = rec
                else:
                    st.queue.append(rec)
        self._state = st
        self._import_v1(st)
        return st

    def _v1_ok(self) -> bool:
        if not self._v1_values:
            return False
        if [str(c).strip() for c in self._v1_values[0][: len(V1_HEADERS)]] != V1_HEADERS:
            log.warning("sheets: %r has unexpected headers; not using it", V1_TAB)
            return False
        return True

    def _import_v1(self, st: StoreState) -> None:
        """First run on a sheet with a v1 tab: seed posts (posted stays posted, pending becomes
        skipped) and seen articles from it, once. State only: no occurrences are added."""
        if self._meta.get("v1_imported") or not self._v1_ok():
            return
        result = convert_v1(self._v1_values, self.resolver, st.posts)
        for p in result.posts:
            st.posts[p.key] = p
            self._dirty[p.key] = p
        for s in result.seen:
            if s.url not in st.seen:
                s.summary = ""
                st.seen[s.url] = s
                self._seen_extra.append(s)
        self._meta_pending["v1_imported"] = iso(self._now or utcnow())
        log.info("v1 import: %d posts and %d articles seeded from %r (%d rows skipped)",
                 len(result.posts), len(self._seen_extra), V1_TAB, result.skipped_rows)

    def _ensure(self) -> StoreState:
        return self._state if self._state is not None else self.load()

    # --- writing -----------------------------------------------------------
    def _append(self, tab: str, rows: list[list]):
        if not rows:
            return None
        ws = self._tab(tab)
        out = self._call(f"append {tab}", ws.append_rows, [[_cell(v) for v in r] for r in rows],
                         value_input_option="RAW", insert_data_option="INSERT_ROWS", retry=RETRY_429)
        self._grow(tab, len(rows))
        return out

    def _read_index(self, ws, tab: str, n: int) -> dict[tuple, int]:
        # From row 1 (header dropped here): a range starting below the grid is an API error.
        keys = self._call(f"read {tab} keys", ws.get_values, f"A1:{_col(n)}")
        index: dict[tuple, int] = {}
        for i, r in enumerate((keys or [])[1:], start=2):
            k = _key(r, n)
            if any(k):
                index[k] = i
        return index

    def _upsert(self, tab: str, rows: dict[tuple, list], *, delete: Iterable[tuple] = (), exclusive: bool = False,
                merge: Callable[[list, list], list] | None = None) -> None:
        """Delete rows by key (all rows not in `rows` when `exclusive`), then update rows
        whose key exists and append the rest, all placed from one fresh read of the key
        columns (a sort or edit between runs cannot misplace a write). Deletes go first and
        bottom-up so no other write shifts them; a failed delete is only a warning. When the
        deletes would leave only the header row, the top doomed row is cleared instead of
        deleted (the API refuses to delete all non-frozen rows), in the same request."""
        delete = set(delete)
        if not rows and not delete and not exclusive:
            return
        ws = self._tab(tab)
        headers, n = TABS[tab]
        index = self._read_index(ws, tab, n)
        width = _col(len(headers))
        if merge is not None:
            hits = [k for k in rows if k in index]
            if hits:
                got = self._call(f"read {tab} rows", ws.batch_get, [f"A{index[k]}:{width}{index[k]}" for k in hits],
                                 value_render_option="UNFORMATTED_VALUE")
                if len(got) != len(hits):
                    raise StoreError(f"sheets: read {tab} rows failed: incomplete response", safe=True)
                for k, vr in zip(hits, got, strict=True):
                    rows[k] = merge(list(vr[0]) if vr else [], rows[k])
        if exclusive:
            delete |= set(index) - set(rows)
        delete -= set(rows)
        doomed = sorted({index[k] for k in delete if k in index})
        if doomed:
            deleted, requests_ = doomed, []
            if self._grid.get(tab, [0])[0] - len(doomed) <= 1:
                # Header only afterwards (or grid unknown): keep the top row, emptied.
                deleted = doomed[1:]
                requests_.append({"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": doomed[0] - 1,
                                                           "endRowIndex": doomed[0]},
                                                 "cell": {}, "fields": "userEnteredValue"}})
            spans: list[list[int]] = []          # [start, end) row numbers, bottom-up
            for r in reversed(deleted):
                if spans and spans[-1][0] == r + 1:
                    spans[-1][0] = r
                else:
                    spans.append([r, r + 1])
            requests_ += [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                                         "startIndex": a - 1, "endIndex": b - 1}}}
                          for a, b in spans]
            try:
                # Never retried: a delete applied despite an error would shift rows.
                self._call(f"prune {tab}", self.ss.batch_update, {"requests": requests_}, retry=RETRY_NONE)
            except StoreError as exc:
                log.warning("sheets: %s; rows stay until a later run (%s)", f"prune {tab} failed", exc)
                index = self._read_index(ws, tab, n)   # it may or may not have been applied
            else:
                self._grow(tab, -len(deleted))
                gone = set(doomed)
                index = {k: i - bisect.bisect_left(deleted, i) for k, i in index.items() if i not in gone}
        updates, new = [], []
        for k, row in rows.items():
            cells = [_cell(v) for v in row]
            if k in index:
                updates.append({"range": f"A{index[k]}:{width}{index[k]}", "values": [cells]})
            else:
                new.append(cells)
        if updates:
            self._call(f"update {tab}", ws.batch_update, updates, value_input_option="RAW")
        if new:
            self._call(f"append {tab}", ws.append_rows, new, value_input_option="RAW",
                       insert_data_option="INSERT_ROWS", retry=RETRY_429)
            self._grow(tab, len(new))

    def _article_counts(self, articles: list[ArticleCheck]) -> None:
        agg: dict[tuple, dict] = {}
        for a in articles:
            k = (a.checked_at[:10], a.feed)
            c = agg.setdefault(k, {"outlet": a.outlet, **dict.fromkeys(_COUNT_FIELDS, 0)})
            c["outlet"] = a.outlet or c["outlet"]
            c["articles_checked"] += 1
            c["fetch_ok"] += 1 if a.fetch_ok else 0
            c["articles_with_any_embed"] += 1 if any(a.embeds.values()) else 0
            c["articles_with_embed_markup"] += 1 if any(a.embed_types.get(t) for t in EMBED_MARKUP_TYPES) else 0
            for platform, num in a.embeds.items():
                if f"embeds_{platform}" in c:
                    c[f"embeds_{platform}"] += int(num)
        rows = {k: [k[0], k[1], c["outlet"], *(c[f] for f in _COUNT_FIELDS), DETECTOR_VERSION]
                for k, c in agg.items()}

        def merge(old: list, new: list) -> list:
            out = list(new)
            for i in range(3, 3 + len(_COUNT_FIELDS)):
                try:
                    prev = str(old[i]).strip() if i < len(old) else ""
                    out[i] = int(new[i]) + (int(float(prev)) if prev else 0)
                except ValueError:
                    pass   # a hand-edited cell: keep this run's count rather than fail
            return out

        self._upsert(COUNTS_TAB, rows, merge=merge)

    @staticmethod
    def _seen_row(s: SeenArticle) -> list:
        row = s.to_row()
        row[SeenArticle.field_names().index("summary")] = ""   # kept lean: summaries go to occurrences
        return row

    @_guarded
    def save(self, *, occurrences, articles, posts, seen, health, queue) -> None:
        """Order: dataset (occurrences, articles, posts), then state (_queue, _seen, _meta),
        then article_counts. A failure before _seen causes a re-check (duplicate rows are
        possible, lost ones are not); article_counts after _seen can never double-count."""
        st = self._ensure()
        occurrences, articles, posts, seen = list(occurrences), list(articles), list(posts), list(seen)
        for p in posts:
            st.posts[p.key] = p
            self._dirty[p.key] = p
            self._touched.add(p.key)
        for s in seen:
            st.seen[s.url] = s
        cutoff = (self._now or utcnow()) - self.seen_retention
        pruned = {u for u, s in st.seen.items() if seen_expired(s, cutoff)}
        st.seen = {u: s for u, s in st.seen.items() if u not in pruned}
        for h in health:
            st.health[h.feed] = h
        st.queue = list(queue)
        changed_seen = {s.url: s for s in [*self._seen_extra, *seen] if s.url not in pruned}

        self._append("occurrences", [o.to_row() for o in occurrences])
        self._append("articles", [a.to_row() for a in articles])
        self._flush_posts()
        self._upsert(QUEUE_TAB, {_key(q.to_row(), 3): q.to_row() for q in st.queue},
                     exclusive=QUEUE_TAB in self._ws or bool(st.queue))
        self._upsert(SEEN_TAB, {(u,): self._seen_row(s) for u, s in changed_seen.items()},
                     delete={(u,) for u in pruned})
        self._seen_extra = []
        if self._meta_pending:
            self._upsert(META_TAB, {(k,): [k, v] for k, v in self._meta_pending.items()})
            self._meta.update(self._meta_pending)
            self._meta_pending = {}
        self._article_counts(articles)
        self._upsert("feed_health", {(h.feed,): h.to_row() for h in health})
        if self.write_v1:
            self._v1_pending.extend(occurrences)

    def _flush_posts(self) -> None:
        if not self._dirty:
            return
        self._upsert("posts", {(p.platform, p.canonical_id): p.to_row() for p in self._dirty.values()})
        self._dirty.clear()

    @_guarded
    def update_post(self, post: Post) -> None:
        self._ensure().posts[post.key] = post
        self._dirty[post.key] = post
        self._touched.add(post.key)
        if post.repost_status == repost.POSTED:
            self._flush_posts()

    # --- v1 tab ----------------------------------------------------------------
    def _v1_key(self, url: str) -> tuple[str, str]:
        """(canonical key, rkey of an unresolved Bluesky handle URL or '')."""
        ref = match_url(url)
        if ref is None:
            return url, ""
        if ref.unresolved:
            return url, _rkey(ref.canonical_id) if ref.platform == "bluesky" else ""
        return ref.key, ""

    def _v1_lookup_ok(self) -> bool:
        return self.v1_resolver is not None and self._clock() <= self.deadline

    def _v1_handle(self, did: str) -> str:
        """Verified Bluesky handle for v1 rows (v1 wrote handles), '' without a v1 resolver,
        past the deadline, or when the capped, cached lookup fails (the row then names the
        DID). A verified handle is remembered for matching handle URLs in this run."""
        if not self._v1_lookup_ok():
            return ""
        try:
            handle = self.v1_resolver.verified_handle(did)
        except Exception as exc:          # class name only: never the message
            log.debug("sheets: handle lookup failed: %s", type(exc).__name__)
            return ""
        if handle:
            self._handle_did[handle] = did
        return handle

    def _did_of(self, handle: str) -> str:
        """DID of a handle from a Post URL: this run's cache (including the import's), then
        a capped lookup while time remains; '' when it cannot be resolved now."""
        h = handle.lower()
        if h in self._handle_did:
            return self._handle_did[h]
        did = ((self.resolver.cached_did(h) if self.resolver is not None else "")
               or (self.v1_resolver.cached_did(h) if self.v1_resolver is not None else ""))
        if not did and self._v1_lookup_ok():
            try:
                did = self.v1_resolver.handle_to_did(h)
            except Exception as exc:      # class name only: never the message
                log.debug("sheets: handle lookup failed: %s", type(exc).__name__)
        self._handle_did[h] = did
        return did

    def _write_v1(self, occurrences: list[Occurrence]) -> None:
        """Append this run's occurrences to "All Embeds" in the v1 layout, deduplicated by
        canonical post id against the existing rows, with the final repost status; then
        update the Repost Status of existing rows whose post changed this run (as v1 did)."""
        self._v1_unwritten = len(occurrences)
        if not self._v1_ok():
            self._v1_unwritten = 0
            return
        st = self._ensure()
        ws = self._ws[V1_TAB]
        rows_by_key: dict[str, list[int]] = {}
        handle_rows: dict[str, list[tuple[int, str]]] = {}   # rkey -> [(row, handle)] of handle URLs
        for i, row in enumerate(self._v1_values[1:], start=2):
            url = str(row[_V1_URL_COL]) if len(row) > _V1_URL_COL else ""
            if not url:
                continue
            key, rkey = self._v1_key(url)
            rows_by_key.setdefault(key, []).append(i)
            if rkey:
                handle_rows.setdefault(rkey, []).append((i, bluesky.actor_of(match_url(url).canonical_id)))

        def locate(key: str) -> list[int]:
            """Rows of a post. A Bluesky post may sit under a handle URL (v1, and v2 rows in the
            "handle" form): only handle rows with the same rkey are resolved, each at most once."""
            if not rows_by_key.get(key) and key.startswith("bluesky:"):
                rkey, left = _rkey(key), []
                for i, handle in handle_rows.get(rkey, []):
                    did = self._did_of(handle)
                    if did:
                        rows_by_key.setdefault(bluesky.make(did, rkey).key, []).append(i)
                    else:
                        left.append((i, handle))
                handle_rows[rkey] = left
            return rows_by_key.get(key, [])

        def known(o: Occurrence) -> bool:
            # Matching never depends on the network succeeding: a handle URL with the same
            # rkey whose handle cannot be resolved now (lookup cap, deadline, error) counts as
            # this post. rkeys only collide between different authors, so at worst another
            # author's post with the same rkey gets no All Embeds row (occurrences has it);
            # a missing v1 row is preferred over a duplicate.
            if o.key in rows_by_key or locate(o.key):
                return True
            return o.platform == bluesky.NAME and bool(handle_rows.get(_rkey(o.canonical_id)))

        status = {(p.platform, p.canonical_id): p.repost_status for p in st.posts.values()}
        fresh = []
        for o in occurrences:
            if not known(o):
                rows_by_key[o.key] = []
                fresh.append(o)
        new_rows = v1_rows(V1_HEADERS, set(), fresh, status, self._v1_handle) or []
        self._v1_unwritten = len(new_rows)
        # Status of earlier rows for posts that changed this run. Row numbers from load may be
        # stale (a sort in the meantime), so column G:K is re-read and rows located by key.
        wanted = {key: st.posts[key] for key in self._touched
                  if key in st.posts and st.posts[key].repost_status != "n/a" and locate(key)}
        if wanted:
            resolved = {self._v1_values[i - 1][_V1_URL_COL]: key for key, rows in rows_by_key.items()
                        for i in rows if len(self._v1_values[i - 1]) > _V1_URL_COL}
            current = self._call("read v1 urls", ws.get_values, "G1:K")
            updates, moved = [], 0
            for i, row in enumerate((current or [])[1:], start=2):
                url = str(row[0]) if row else ""
                key = self._v1_key(url)[0] if url else ""
                if key not in wanted and url in resolved:
                    key = resolved[url]          # a handle URL resolved earlier in this run
                p = wanted.get(key)
                if p is None:
                    continue
                if i not in rows_by_key.get(key, []):
                    moved += 1
                old = str(row[4]) if len(row) > 4 else ""
                if old != p.repost_status:
                    updates.append({"range": f"{_V1_STATUS_COL}{i}", "values": [[p.repost_status]]})
            if moved:
                log.debug("sheets: %d v1 rows moved since load; status written at their new place", moved)
            if updates:
                self._call("update v1 status", ws.batch_update, updates, value_input_option="RAW")
        if new_rows:
            self._call("append v1", ws.append_rows, [[_cell(v) for v in r] for r in new_rows],
                       value_input_option="RAW", insert_data_option="INSERT_ROWS", retry=RETRY_429)
            self._grow(V1_TAB, len(new_rows))
        self._v1_unwritten = 0

    @_guarded
    def append_run(self, run: RunLog) -> None:
        """Run row first, so a failed All Embeds write never loses the run log. All Embeds
        is a convenience copy: a failure is a warning (rows are not retried later; the
        occurrences tab has every one)."""
        self._flush_posts()
        self._append("runs", [run.to_row()])
        if self.write_v1:
            pending, self._v1_pending = self._v1_pending, []
            try:
                self._write_v1(pending)
            except Exception as exc:
                log.warning("sheets: %s not updated, %d row(s) not written (the occurrences tab has them): %s",
                            V1_TAB, self._v1_unwritten, exc if isinstance(exc, StoreError) else type(exc).__name__)
        self.check_cells()
