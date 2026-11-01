"""Sheets storage mode: the Google Sheet as state and dataset, with a fake gspread (no network)."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from media_embed_tracer import cli
from media_embed_tracer.config import Config
from media_embed_tracer.models import (
    DETECTOR_VERSION,
    FeedHealth,
    Occurrence,
    Post,
    QueuedHit,
    RunLog,
    SeenArticle,
)
from media_embed_tracer.pipeline import EXIT_OK, EXIT_STORE
from media_embed_tracer.store import StoreError
from media_embed_tracer.store.sheets import V1_HEADERS, V1_TAB
from media_embed_tracer.store.sheets_store import ARTICLE_COUNT_HEADERS, META_TAB, TABS, SheetsStore

from .conftest import NOW, FakeHttp
from .sheets_fake import FakeSpreadsheet, FakeWorksheet
from .test_pipeline import APPVIEW, BOT, DID_A, DID_B, FEED, FakeBsky, bsky_quote, cfg, page, rss
from .test_pipeline import do_run as jsonl_run

KEY_A = f"bluesky:at://{DID_A}/app.bsky.feed.post/3k"


def store_for(ss, now=NOW, **kw) -> SheetsStore:
    kw.setdefault("min_interval", 0)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("rand", lambda: 0.0)
    return SheetsStore(ss, 30, now, **kw)


def run_sheets(ss, http, conf=None, client=None, now=NOW, **kw):
    store = store_for(ss, now)
    code, stats, _ = jsonl_run(None, http, conf, client, now=now, store=store, **kw)
    return code, stats, store


def rows(ss, tab) -> list[dict]:
    values = ss.tab(tab)
    return [dict(zip(values[0], r, strict=False)) for r in values[1:]]


def two_article_http():
    return FakeHttp({FEED: rss("https://news.test/a1", "https://news.test/a2"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k")),
                     "https://news.test/a2": page('<a href="https://x.com/u/status/12345">x</a>')})


# --- state round trip ------------------------------------------------------------

def test_state_round_trips_through_sheet_tabs():
    ss = FakeSpreadsheet()
    store = store_for(ss)
    store.load()
    seen = SeenArticle("https://news.test/a1", "News", "news.test", "Title", "2026-03-01T10:00:00Z", "long summary",
                       "2026-03-01T11:00:00Z", "2026-03-01T11:00:00Z", 1, 0, "bluesky:at://x x:1", "")
    post = Post("bluesky", f"at://{DID_A}/app.bsky.feed.post/3k", "https://bsky.app/p", DID_A,
                "2026-03-01T11:00:00Z", repost_status="pending", attempts=2)
    health = FeedHealth("News", FEED, last_ok="2026-03-01T11:00:00Z", last_entries=4)
    queued = QueuedHit("https://bsky.app/profile/a.example/post/1", "blockquote", "https://news.test/a1",
                       "https://news.test/a1", "Example", "news.test", "News", "T", "", "2026-03-01T11:00:00Z", 3)
    store.save(occurrences=[], articles=[], posts=[post], seen=[seen], health=[health], queue=[queued])

    st = store_for(ss).load()
    assert st.posts == {post.key: post}
    assert st.health == {"News": health}
    assert st.queue == [queued]
    assert st.seen["https://news.test/a1"].found_keys == seen.found_keys
    assert st.seen["https://news.test/a1"].summary == ""   # _seen stays lean
    for tab, (headers, _) in TABS.items():
        if tab in ss.sheets:
            assert ss.tab(tab)[0] == headers
    # RAW everywhere, so feed text is never evaluated as a formula.
    assert {o for ws in ss.sheets.values() for o in ws.options} == {"RAW"}


def test_pipeline_runs_against_the_sheet_and_resumes_state():
    ss = FakeSpreadsheet()
    http = two_article_http()
    code, stats, _ = run_sheets(ss, http)
    assert code == EXIT_OK and stats.occurrences == 2
    assert {r["canonical_id"] for r in rows(ss, "occurrences")} == {f"at://{DID_A}/app.bsky.feed.post/3k", "12345"}
    assert {r["url"] for r in rows(ss, "_seen")} == {"https://news.test/a1", "https://news.test/a2"}
    assert len(rows(ss, "runs")) == 1
    assert [r["url"] for r in rows(ss, "articles")] == ["https://news.test/a1", "https://news.test/a2"]
    # Second run with a fresh store: state comes from the sheet, so nothing is fetched again.
    http.calls.clear()
    code, stats, _ = run_sheets(ss, http, now=NOW + timedelta(minutes=30))
    assert code == EXIT_OK and http.calls == [FEED] and stats.occurrences == 0
    assert len(rows(ss, "occurrences")) == 2 and len(rows(ss, "posts")) == 2 and len(rows(ss, "runs")) == 2


def test_queue_tab_follows_the_queue():
    art = "https://news.test/a1"
    html = page('<blockquote class="bluesky-embed"><a href="https://bsky.app/profile/alice.example.com/post/3kh">p'
                '</a></blockquote>')
    ss = FakeSpreadsheet()
    http = FakeHttp({FEED: rss(art), art: html})
    run_sheets(ss, http)
    assert len(rows(ss, "_queue")) == 1 and "occurrences" not in ss.sheets
    http.pages[f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.example.com"] = {"did": DID_A}
    run_sheets(ss, http, now=NOW + timedelta(minutes=30))
    assert rows(ss, "_queue") == []
    assert [r["canonical_id"] for r in rows(ss, "occurrences")] == [f"at://{DID_A}/app.bsky.feed.post/3kh"]


# --- pruning -------------------------------------------------------------------------

def seen_row(url, last_checked):
    return SeenArticle(url, "News", "news.test", "T", "", "", last_checked, last_checked, 1).to_row()


def test_seen_tab_is_pruned_by_retention():
    old, fresh = "2026-01-01T00:00:00Z", "2026-02-27T00:00:00Z"
    ss = FakeSpreadsheet({"_seen": [SeenArticle.field_names(),
                                    seen_row("https://o/1", old), seen_row("https://o/2", old),
                                    seen_row("https://f/1", fresh), seen_row("https://o/3", old),
                                    seen_row("https://f/2", fresh)]})
    store = store_for(ss)
    assert len(store.load().seen) == 5
    store.save(occurrences=[], articles=[], posts=[], seen=[], health=[], queue=[])
    assert [r["url"] for r in rows(ss, "_seen")] == ["https://f/1", "https://f/2"]
    assert set(store_for(ss).load().seen) == {"https://f/1", "https://f/2"}
    # Two non-adjacent spans are deleted in one request.
    assert ss.calls.count(("ss_batch_update", "")) == 1


# --- header validation and unreadable sheets -------------------------------------------

@pytest.mark.parametrize(("tab", "values"), [
    ("_seen", [["Date", "Time", "Platform"], ["x", "y", "z"]]),
    ("posts", [["Date", "Time", "Platform"]]),
    ("runs", [["run_at", "something_else"]]),
    ("article_counts", [[*ARTICLE_COUNT_HEADERS[:3], "wrong"]]),
    ("_queue", [[], ["a", "b"]]),                     # rows but no header row
])
def test_unexpected_headers_refuse_to_run_before_scraping(tab, values):
    ss = FakeSpreadsheet({tab: values})
    http = two_article_http()
    code, _stats, _ = run_sheets(ss, http, cfg(posting_enabled=True), FakeBsky())
    assert code == EXIT_STORE and http.calls == []
    assert ss.tab(tab) == [[str(c) for c in r] for r in values]                 # untouched
    assert {op for op, _ in ss.calls} <= {"worksheets", "values_batch_get"}   # read only


def test_empty_tab_gets_headers_and_is_used():
    ss = FakeSpreadsheet({"posts": []})
    code, _, _ = run_sheets(ss, two_article_http())
    assert code == EXIT_OK and ss.tab("posts")[0] == Post.field_names() and len(rows(ss, "posts")) == 2


def test_unreadable_sheet_fails_before_scraping():
    ss = FakeSpreadsheet(unreadable=True)
    http = two_article_http()
    bot = FakeBsky()
    code, _, _ = run_sheets(ss, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_STORE and http.calls == [] and bot.created == [] and bot.did == ""


def test_cli_stops_before_scraping_when_the_sheet_cannot_be_opened(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise StoreError("cannot open spreadsheet: SpreadsheetNotFound")

    def no_run(*_a, **_k):
        raise AssertionError("pipeline must not start")

    monkeypatch.setattr("media_embed_tracer.store.sheets.open_spreadsheet", boom)
    monkeypatch.setattr(cli, "run", no_run)
    for name in ("DATA_REPO", "PUBLIC_DATA_BRANCH", "FEEDS_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPREADSHEET_URL", "placeholder-sheet-id-0000000000")
    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "{}")
    monkeypatch.setenv("FEEDS_FILE", "config/feeds.international.toml")
    with caplog.at_level(logging.ERROR):
        assert cli.cmd_run(None) == EXIT_STORE
    assert "nothing scraped or posted" in caplog.text


def test_v1_row_lookups_have_their_own_small_cap_and_the_run_deadline(monkeypatch):
    import time

    monkeypatch.setattr("media_embed_tracer.store.sheets.open_spreadsheet", lambda *_a: FakeSpreadsheet())
    conf = Config(storage_mode="sheets", spreadsheet_url="x", google_credentials_json="{}", max_run_minutes=10)
    store = cli.build_sheets_store(conf, FakeHttp({}))
    assert (store.resolver.max_lookups, store.v1_resolver.max_lookups) == (200, 20)
    assert store.resolver is not store.v1_resolver
    assert 0 < store.deadline - time.monotonic() <= 600


def test_sheets_mode_has_no_mirror_and_other_modes_keep_it():
    assert cli.build_mirrors(Config(storage_mode="sheets", spreadsheet_url="x", google_credentials_json="{}")) == []


# --- article_counts --------------------------------------------------------------------

def test_article_counts_upsert_per_day_and_feed():
    ss = FakeSpreadsheet()
    http = FakeHttp({FEED: rss("https://news.test/a1", "https://news.test/a2", "https://news.test/a3"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m")),
                     "https://news.test/a2": page('<a href="https://x.com/u/status/12345">x</a>')})
    run_sheets(ss, http, cfg(max_articles=2))
    [r] = rows(ss, "article_counts")
    assert (r["date"], r["feed"], r["outlet"]) == ("2026-03-01", "News", "Example News")
    assert (r["articles_checked"], r["fetch_ok"], r["articles_with_any_embed"]) == ("2", "2", "2")
    assert r["articles_with_embed_markup"] == "1" and r["embeds_bluesky"] == "2" and r["embeds_x"] == "1"
    assert r["detector_version"] == DETECTOR_VERSION
    # Same day: the row is updated in place (a3 is a 404).
    run_sheets(ss, http, now=NOW + timedelta(minutes=30))
    [r] = rows(ss, "article_counts")
    assert (r["articles_checked"], r["fetch_ok"], r["embeds_bluesky"]) == ("3", "2", "2")
    # Next day: a new row (rechecks of a1 and a2, third try of a3).
    run_sheets(ss, http, now=NOW + timedelta(days=1))
    counts = rows(ss, "article_counts")
    assert [c["date"] for c in counts] == ["2026-03-01", "2026-03-02"]
    assert (counts[1]["articles_checked"], counts[1]["fetch_ok"], counts[1]["embeds_bluesky"]) == ("3", "2", "2")


def test_article_counts_survive_a_sort_between_runs():
    ss = FakeSpreadsheet()
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(),
                     "https://other.test/rss": rss("https://other.test/b1"), "https://other.test/b1": page()})
    from media_embed_tracer.feeds import FeedConfig, FeedSet
    fs = FeedSet([FeedConfig("News", FEED), FeedConfig("Other", "https://other.test/rss")])
    run_sheets(ss, http, fs=fs)
    values = ss.tab("article_counts")
    values[1:] = sorted(values[1:], key=lambda r: r[1], reverse=True)    # a human sorts by feed
    http.pages[FEED] = rss("https://news.test/a1", "https://news.test/a2")
    http.pages["https://news.test/a2"] = page()
    run_sheets(ss, http, now=NOW + timedelta(minutes=30), fs=fs)
    by_feed = {r["feed"]: r["articles_checked"] for r in rows(ss, "article_counts")}
    assert by_feed == {"News": "2", "Other": "1"}


# --- posting: idempotency and status write-back ---------------------------------------

def test_failed_status_write_after_quote_does_not_double_post():
    """The quote succeeds, every write of the posts tab fails: next run sees the quote on
    Bluesky and marks the post posted instead of quoting it again."""
    ss = FakeSpreadsheet()
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    store = store_for(ss)
    store.load()
    real_flush = store._flush_posts
    calls = {"n": 0}

    def flaky_flush():
        calls["n"] += 1
        if calls["n"] > 1:                     # the save before posting succeeds, later writes fail
            ss.fail[("batch_update", "posts")] = [400] * 10
        real_flush()

    store._flush_posts = flaky_flush
    code, stats, _ = jsonl_run(None, http, cfg(posting_enabled=True), bot, store=store)
    assert code == EXIT_STORE and len(bot.created) == 1
    assert rows(ss, "posts")[0]["repost_status"] == "pending"     # the write was lost
    ss.fail.clear()
    code, stats, _ = run_sheets(ss, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=30))
    assert code == EXIT_OK and len(bot.created) == 1 and stats.posting.already_quoted == 1
    [p] = rows(ss, "posts")
    assert p["repost_status"] == "posted" and p["quoted_uri"] == f"at://{BOT}/app.bsky.feed.post/q1"


def test_posted_status_is_written_right_after_each_quote():
    ss = FakeSpreadsheet()
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))})
    states = []

    class Watching(FakeBsky):
        def create_post(self, record):
            states.append(sorted(r["repost_status"] for r in rows(ss, "posts")))
            return super().create_post(record)

    code, stats, _ = run_sheets(ss, http, cfg(posting_enabled=True), Watching())
    assert code == EXIT_OK and stats.posting.posted == 2
    # Before the second quote, the first one is already recorded in the sheet.
    assert states == [["pending", "pending"], ["pending", "posted"]]
    assert sorted(r["repost_status"] for r in rows(ss, "posts")) == ["posted", "posted"]


def test_daily_cap_and_opt_out_still_apply():
    ss = FakeSpreadsheet()
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))})
    bot = FakeBsky()
    code, stats, _ = run_sheets(ss, http, cfg(posting_enabled=True, daily_cap=5, opt_out_dids=frozenset({DID_B})), bot)
    assert code == EXIT_OK and len(bot.created) == 1 and stats.posting.skipped == 1
    status = {r["author"]: r["repost_status"] for r in rows(ss, "posts")}
    assert status == {DID_A: "posted", DID_B: "skipped"}


# --- quota, retries, error texts ---------------------------------------------------------

def test_transient_errors_are_retried_with_backoff_and_rate_limited():
    ss = FakeSpreadsheet()
    sleeps = []
    ticks = iter(range(10_000))
    store = SheetsStore(ss, 30, NOW, min_interval=1.1, sleep=sleeps.append, clock=lambda: next(ticks) * 0.01,
                        rand=lambda: 0.0)
    ss.fail[("worksheets", "")] = [429, 503]
    store.load()
    assert ss.calls.count(("worksheets", "")) == 3
    assert 5 in sleeps and 10 in sleeps                     # backoff 5 s, then 10 s
    assert all(s <= 1.1 for s in sleeps if s not in (5, 10))  # spacing between requests


def test_permanent_error_shows_only_the_class_name():
    ss = FakeSpreadsheet()
    store = store_for(ss)
    store.load()
    ss.fail[("append_rows", "occurrences")] = [400]
    occ = Occurrence("x", "1", "https://x.com/i/status/1", "", "link", "https://a", "https://a", "o", "a", "f",
                     "Minister quits", "", "2026-03-01T00:00:00Z")
    with pytest.raises(StoreError) as err:
        store.save(occurrences=[occ], articles=[], posts=[], seen=[], health=[], queue=[])
    assert str(err.value) == "sheets: append occurrences failed: APIError"
    assert ss.calls.count(("append_rows", "occurrences")) == 1   # 400 is not retried


def test_requests_per_run_stay_well_under_quota():
    ss = FakeSpreadsheet()
    run_sheets(ss, two_article_http(), cfg(posting_enabled=True), FakeBsky())
    first = len(ss.calls)
    ss.calls.clear()
    run_sheets(ss, two_article_http(), cfg(posting_enabled=True), FakeBsky(), now=NOW + timedelta(minutes=30))
    assert first <= 45 and len(ss.calls) <= 20, (first, ss.calls)


# --- v1 tab --------------------------------------------------------------------------------

def test_v1_tab_keeps_filling_in_sheets_mode_and_is_skipped_for_migration():
    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS]})
    run_sheets(ss, two_article_http())
    assert [r[6] for r in ss.tab(V1_TAB)[1:]] == [f"https://bsky.app/profile/{DID_A}/post/3k",
                                                  "https://x.com/u/status/12345"]
    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS]})
    store = store_for(ss, write_v1=False)
    store.load()
    occ = Occurrence("x", "1", "https://x.com/i/status/1", "", "unknown", "https://a", "https://a", "o", "a", "f",
                     "T", "", "2026-03-01T00:00:00Z", "v1")
    store.save(occurrences=[occ], articles=[], posts=[], seen=[], health=[], queue=[])
    store.append_run(RunLog("2026-03-01T00:00:00Z", "2"))
    assert ss.tab(V1_TAB) == [V1_HEADERS] and len(rows(ss, "occurrences")) == 1


def test_failed_prune_is_a_warning_never_retried_and_posting_still_runs(caplog):
    old = SeenArticle("https://o/1", "News", "news.test", "T", "", "", "2026-01-01T00:00:00Z",
                      "2026-01-01T00:00:00Z", 2, 0, "", "1").to_row()
    ss = FakeSpreadsheet({"_seen": [SeenArticle.field_names(), old]})
    ss.fail[("ss_batch_update", "")] = [503, 0]
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    with caplog.at_level(logging.WARNING):
        code, _stats, _ = run_sheets(ss, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_OK and len(bot.created) == 1                   # save did not fail
    assert ss.calls.count(("ss_batch_update", "")) == 1                # not retried
    assert "prune _seen failed" in caplog.text
    assert [r["url"] for r in rows(ss, "_seen")] == ["https://o/1", "https://news.test/a1"]   # pruned later
    run_sheets(ss, http, now=NOW + timedelta(minutes=30))
    assert [r["url"] for r in rows(ss, "_seen")] == ["https://news.test/a1"]


def test_malformed_api_response_becomes_store_error():
    ss = FakeSpreadsheet()
    ss.values_batch_get = lambda ranges: {"valueRanges": []}
    ss.sheets["posts"] = FakeWorksheet(ss, "posts")
    with pytest.raises(StoreError, match="incomplete response"):
        store_for(ss).load()
    ss.worksheets = lambda: None    # not iterable: an unexpected error inside load
    with pytest.raises(StoreError, match="load failed: TypeError"):
        store_for(ss).load()


# --- per-article log, summaries, layout upgrades, cell usage ---------------------------

def test_summary_reaches_occurrences_and_v1_tab_but_never_the_log(caplog):
    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS]})
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    with caplog.at_level(logging.DEBUG):
        run_sheets(ss, http)
    [o] = rows(ss, "occurrences")
    assert o["article_summary"] == "Summary bold"            # HTML stripped
    assert ss.tab(V1_TAB)[1][8] == "Summary bold"            # v1 "Article Summary"
    assert "Summary bold" not in caplog.text
    # A recheck the next day takes the summary from the feed entry (not stored in _seen).
    assert rows(ss, "_seen")[0]["summary"] == ""
    http.pages["https://news.test/a1"] = page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))
    run_sheets(ss, http, now=NOW + timedelta(days=1))
    assert [r["article_summary"] for r in rows(ss, "occurrences")] == ["Summary bold"] * 2


def test_summary_truncated_on_a_word_boundary():
    from media_embed_tracer.util import SUMMARY_LIMIT, plain_text

    text = plain_text("<p>" + "word " * 200 + "</p>", SUMMARY_LIMIT)
    assert len(text) <= SUMMARY_LIMIT and text.endswith("word…")


def test_older_occurrences_layout_gains_the_summary_column():
    old = Occurrence.field_names()[:-1]
    row = ["x", "1", "https://x.com/i/status/1", "", "link", "https://a", "https://a", "o", "a", "f", "T", "",
           "2026-02-01T00:00:00Z", "2.0.0"]
    ss = FakeSpreadsheet({"occurrences": [old, row], "_queue": [QueuedHit.field_names()[:-1]]})
    code, _, _ = run_sheets(ss, two_article_http())
    assert code == EXIT_OK
    assert ss.tab("occurrences")[0] == Occurrence.field_names() and ss.tab("occurrences")[1] == row
    assert ss.sheets["occurrences"].col_count >= len(Occurrence.field_names())
    assert ss.tab("_queue")[0] == QueuedHit.field_names()
    # Any other header is still refused.
    ss = FakeSpreadsheet({"occurrences": [old[:-1]]})
    assert run_sheets(ss, two_article_http())[0] == EXIT_STORE


def test_cell_usage_warning_above_80_percent(caplog, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    big = FakeSpreadsheet({"Archive": [["a"]]})
    big.sheets["Archive"].row_count, big.sheets["Archive"].col_count = 300_000, 27   # 8.1M cells
    with caplog.at_level(logging.WARNING):
        code, _, store = run_sheets(big, two_article_http())
    assert code == EXIT_OK and store.cells_used() > 8_100_000
    assert "spreadsheet at 81% of the 10M cell limit" in caplog.text
    assert "::warning::spreadsheet at 81%" in capsys.readouterr().out
    assert big.tab("Archive") == [["a"]]                    # nothing deleted or stopped
    caplog.clear()
    small = FakeSpreadsheet()
    with caplog.at_level(logging.WARNING):
        run_sheets(small, two_article_http())
    assert "cell limit" not in caplog.text


def test_cell_count_tracks_appends_and_deletes_without_extra_calls():
    ss = FakeSpreadsheet({"_seen": [SeenArticle.field_names(), seen_row("https://o/1", "2026-01-01T00:00:00Z"),
                                    seen_row("https://f/1", "2026-02-27T00:00:00Z")]})
    store = store_for(ss)
    store.load()
    before = store.cells_used()
    calls = len(ss.calls)
    store.save(occurrences=[], articles=[], posts=[], seen=[], health=[], queue=[])
    assert store.cells_used() == before - len(SeenArticle.field_names())
    assert ("worksheets", "") not in ss.calls[calls:]


# --- v1 statuses, write order, retries ----------------------------------------------

def v1_row(post_url, status="", article="https://old.test/a", date="2026-01-01"):
    return [date, "10:00:00", "x", "old.test", "", article, post_url, "Old title", "Old summary", "", status]


def test_v1_rows_written_after_posting_with_final_status():
    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS], META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    code, _, _ = run_sheets(ss, http, cfg(posting_enabled=True), FakeBsky())
    assert code == EXIT_OK
    assert [r[10] for r in ss.tab(V1_TAB)[1:]] == ["posted"]


def test_v1_status_of_earlier_rows_follows_later_posting():
    """Held back by MAX_POSTS_PER_RUN, a row starts as pending and turns posted next run."""
    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS], META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))})
    bot = FakeBsky()
    run_sheets(ss, http, cfg(posting_enabled=True, max_posts_per_run=1), bot)
    assert sorted(r[10] for r in ss.tab(V1_TAB)[1:]) == ["pending", "posted"]
    run_sheets(ss, http, cfg(posting_enabled=True, max_posts_per_run=1), bot, now=NOW + timedelta(minutes=30))
    assert [r[10] for r in ss.tab(V1_TAB)[1:]] == ["posted", "posted"] and len(ss.tab(V1_TAB)) == 3


def test_upsert_deletes_first_then_updates_and_appends_from_one_read():
    old = "2026-01-01T00:00:00Z"
    ss = FakeSpreadsheet({"_seen": [SeenArticle.field_names(), seen_row("https://o/1", old),
                                    seen_row("https://f/1", "2026-02-27T00:00:00Z"), seen_row("https://o/2", old),
                                    seen_row("https://f/2", "2026-02-27T00:00:00Z")]})
    store = store_for(ss)
    store.load()
    changed = SeenArticle("https://f/2", "News", "news.test", "T2", "", "", "2026-02-27T00:00:00Z",
                          "2026-03-01T00:00:00Z", 2)
    added = SeenArticle("https://n/1", "News", "news.test", "N", "", "", "2026-03-01T00:00:00Z",
                        "2026-03-01T00:00:00Z", 1)
    store.save(occurrences=[], articles=[], posts=[], seen=[changed, added], health=[], queue=[])
    got = {r["url"]: r for r in rows(ss, "_seen")}
    assert list(got) == ["https://f/1", "https://f/2", "https://n/1"]
    assert got["https://f/2"]["title"] == "T2" and got["https://f/2"]["checks"] == "2"
    seen_calls = [op for op, tab in ss.calls if tab in ("_seen", "")][2:]
    assert seen_calls == ["get_values", "ss_batch_update", "batch_update", "append_rows"]


def test_appends_retry_on_429_only_and_updates_on_5xx():
    ss = FakeSpreadsheet()
    store = store_for(ss)
    store.load()
    occ = Occurrence("x", "1", "https://x.com/i/status/1", "", "link", "https://a", "https://a", "o", "a", "f",
                     "T", "", "2026-03-01T00:00:00Z")
    ss.fail[("append_rows", "occurrences")] = [429]
    store.save(occurrences=[occ], articles=[], posts=[], seen=[], health=[], queue=[])
    assert len(rows(ss, "occurrences")) == 1 and ss.calls.count(("append_rows", "occurrences")) == 2
    ss.fail[("append_rows", "occurrences")] = [503]
    with pytest.raises(StoreError):
        store.save(occurrences=[occ], articles=[], posts=[], seen=[], health=[], queue=[])
    assert ss.calls.count(("append_rows", "occurrences")) == 3      # no retry after a 5xx
    store = store_for(ss)
    store.load()
    ss.fail[("batch_update", "feed_health")] = [503]
    store.save(occurrences=[], articles=[], posts=[], seen=[], health=[FeedHealth("News", FEED)], queue=[])
    store.save(occurrences=[], articles=[], posts=[], seen=[], health=[FeedHealth("News", FEED, last_entries=3)],
               queue=[])
    assert rows(ss, "feed_health")[0]["last_entries"] == "3"


def test_backoff_honours_retry_after_with_jitter_and_a_90s_budget():
    from .sheets_fake import APIError

    ss = FakeSpreadsheet()
    sleeps = []
    store = SheetsStore(ss, 30, NOW, min_interval=0, sleep=sleeps.append, rand=lambda: 0.5)
    ss.fail[("worksheets", "")] = [APIError(429, retry_after="30")]
    store.load()
    assert sleeps == [31.0]                      # Retry-After 30 s + 1 s jitter
    sleeps.clear()
    ss.fail[("worksheets", "")] = [503] * 20
    with pytest.raises(StoreError):
        store_for(ss, sleep=sleeps.append, rand=lambda: 0.5).load()
    assert sum(sleeps) <= 90 and sleeps == [6.0, 11.0, 21.0, 41.0]


def test_gspread_client_gets_a_timeout(monkeypatch):
    import gspread

    from media_embed_tracer.store.sheets import HTTP_TIMEOUT, open_spreadsheet

    class Client:
        timeout = None

        def set_timeout(self, t):
            Client.timeout = t

        def open_by_key(self, key):
            return key

    monkeypatch.setattr(gspread, "service_account_from_dict", lambda info, scopes: Client())
    assert open_spreadsheet("placeholder-sheet-id-0000000000", "{}") == "placeholder-sheet-id-0000000000"
    assert Client.timeout == HTTP_TIMEOUT == 60


def test_instance_marker_written_once_and_a_foreign_sheet_refused():
    ss = FakeSpreadsheet()
    store = store_for(ss, instance="international")
    store.load()
    store.save(occurrences=[], articles=[], posts=[], seen=[], health=[], queue=[])
    assert ss.tab(META_TAB) == [["key", "value"], ["instance", "international"]]
    store_for(ss, instance="international").load()
    http = two_article_http()
    code, _, _ = jsonl_run(None, http, None, None, store=store_for(ss, instance="localized"))
    assert code == EXIT_STORE and http.calls == []
    with pytest.raises(StoreError) as err:              # a cell value is never echoed
        store_for(ss, instance="localized").load()
    assert "international" not in str(err.value) and "localized" not in str(err.value)


def test_v1_tab_seeds_state_once_without_adding_dataset_rows():
    quoted = f"https://bsky.app/profile/{DID_A}/post/3kold"
    waiting = f"https://bsky.app/profile/{DID_B}/post/3kwait"
    art = "https://news.test/a1"
    v1 = [V1_HEADERS, v1_row(quoted, "posted", art, "2026-02-28"), v1_row(waiting, "pending", art, "2026-02-28"),
          v1_row("https://twitter.com/u/status/77", "", "https://old.test/b", "2026-02-28")]
    ss = FakeSpreadsheet({V1_TAB: v1})
    # The same article is still in the feed and now embeds the already-quoted post.
    http = FakeHttp({FEED: rss(art), art: page(bsky_quote(DID_A, "3kold"))})
    bot = FakeBsky()
    code, _stats, _ = run_sheets(ss, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_OK and bot.created == [] and art not in http.calls   # seen, so not fetched
    status = {r["canonical_id"].rsplit("/", 1)[-1]: r["repost_status"] for r in rows(ss, "posts")}
    assert status == {"3kold": "posted", "3kwait": "skipped", "77": "n/a"}
    assert "occurrences" not in ss.sheets and len(ss.tab(V1_TAB)) == 4       # state only
    assert dict(map(tuple, ss.tab(META_TAB)[1:]))["v1_imported"]
    assert {r["url"] for r in rows(ss, "_seen")} == {art, "https://old.test/b"}
    # Second run: no second import.
    ss.calls.clear()
    run_sheets(ss, http, now=NOW + timedelta(minutes=30))
    assert len(rows(ss, "posts")) == 3 and ("append_rows", "posts") not in ss.calls


def test_v1_dedup_by_canonical_id_including_resolved_handles():
    from media_embed_tracer.resolve import Resolver

    v1 = [V1_HEADERS, v1_row("https://twitter.com/u/status/12345"),
          v1_row("https://bsky.app/profile/alice.example.com/post/3k")]
    ss = FakeSpreadsheet({V1_TAB: v1, META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1", "https://news.test/a2"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k")),
                     "https://news.test/a2": page('<a href="https://x.com/u/status/12345">x</a>'),
                     f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.example.com": {"did": DID_A}})
    store = store_for(ss, v1_resolver=Resolver(http, APPVIEW, max_lookups=5))
    jsonl_run(None, http, None, None, store=store)
    assert len(rows(ss, "occurrences")) == 2 and len(ss.tab(V1_TAB)) == 3    # both already in v1
    # Offline (no resolver), or the lookup fails: a handle row with the same rkey counts as
    # known, so no handle-vs-DID duplicate is appended.
    for v1_resolver in (None, Resolver(FakeHttp({}), APPVIEW, max_lookups=5),
                        Resolver(http, APPVIEW, max_lookups=0)):
        ss = FakeSpreadsheet({V1_TAB: [list(r) for r in v1], META_TAB: [["key", "value"], ["v1_imported", "x"]]})
        jsonl_run(None, http, None, None, store=store_for(ss, v1_resolver=v1_resolver))
        assert len(rows(ss, "occurrences")) == 2 and len(ss.tab(V1_TAB)) == 3


def test_v1_handle_row_of_another_author_with_the_same_rkey_does_not_block_a_new_row():
    from media_embed_tracer.resolve import Resolver

    v1 = [V1_HEADERS, v1_row("https://bsky.app/profile/bob.example.com/post/3k")]
    ss = FakeSpreadsheet({V1_TAB: v1, META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k")),
                     f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=bob.example.com": {"did": DID_B}})
    jsonl_run(None, http, None, None, store=store_for(ss, v1_resolver=Resolver(http, APPVIEW, max_lookups=5)))
    assert [r[6] for r in ss.tab(V1_TAB)[1:]] == ["https://bsky.app/profile/bob.example.com/post/3k",
                                                   f"https://bsky.app/profile/{DID_A}/post/3k"]


def test_v1_handle_lookups_stop_past_the_deadline():
    from media_embed_tracer.resolve import Resolver

    v1 = [V1_HEADERS, v1_row("https://bsky.app/profile/alice.example.com/post/3k")]
    ss = FakeSpreadsheet({V1_TAB: v1, META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m")),
                     **plc(DID_B, "name.bsky.social"),
                     f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.example.com": {"did": DID_A}})
    store = store_for(ss, v1_resolver=Resolver(http, APPVIEW, max_lookups=20), deadline=0.0, clock=lambda: 1.0)
    jsonl_run(None, http, None, None, store=store)
    assert not [u for u in http.calls if "resolveHandle" in u or "plc.directory" in u]
    assert [r[4:7:2] for r in ss.tab(V1_TAB)[1:]] == [
        ["", "https://bsky.app/profile/alice.example.com/post/3k"],                  # 3k: known, not re-added
        [DID_B, f"https://bsky.app/profile/{DID_B}/post/3m"]]                          # no handle: the DID


def test_v1_post_url_form_did_keeps_the_handle_in_author_and_matches_offline(monkeypatch):
    from media_embed_tracer.resolve import Resolver
    from media_embed_tracer.store import sheets

    monkeypatch.setattr(sheets, "V1_POST_URL_FORM", "did")
    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS], META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_B, "3m")),
                     **plc(DID_B, "name.bsky.social")})
    bot = FakeBsky()
    conf = cfg(posting_enabled=True, max_posts_per_run=0)
    jsonl_run(None, http, conf, bot, store=store_for(ss, v1_resolver=Resolver(http, APPVIEW, max_lookups=20)))
    (row,) = ss.tab(V1_TAB)[1:]
    assert row[4] == "name.bsky.social" and row[6] == f"https://bsky.app/profile/{DID_B}/post/3m"
    assert row[10] == "pending"
    # Next run, offline: the DID URL is matched without lookups and its status updated.
    jsonl_run(None, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=30),
              store=store_for(ss, NOW + timedelta(minutes=30)))
    assert [r[10] for r in ss.tab(V1_TAB)[1:]] == ["posted"]


def test_v1_status_update_follows_rows_reordered_after_load():
    """A sort of "All Embeds" between load and append_run must not put statuses on wrong rows."""
    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS], META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))})
    bot = FakeBsky()
    run_sheets(ss, http, cfg(posting_enabled=True, max_posts_per_run=1), bot)
    tab = ss.tab(V1_TAB)
    pending_row = next(r for r in tab[1:] if r[10] == "pending")
    posted_row = next(r for r in tab[1:] if r[10] == "posted")
    new_row = v1_row("https://x.com/u/status/1", "")
    store = store_for(ss, NOW + timedelta(minutes=30))
    real_save = store.save

    def save_then_sort(**kw):
        real_save(**kw)
        # Someone inserts a row and sorts while the run posts: the pending post's row number
        # from load now holds the new row.
        old_number = tab.index(pending_row) + 1
        order = [pending_row, new_row, posted_row] if old_number == 3 else [new_row, posted_row, pending_row]
        tab[1:] = order
        assert tab.index(new_row) + 1 == old_number

    store.save = save_then_sort
    code, _, _ = jsonl_run(None, http, cfg(posting_enabled=True, max_posts_per_run=1), bot,
                           now=NOW + timedelta(minutes=30), store=store)
    assert code == EXIT_OK
    assert pending_row[10] == "posted" and posted_row[10] == "posted"
    assert (new_row + [""] * 11)[10] == ""                # untouched


# --- grid size: ranges from row 1, room in new tabs, drained tabs ----------------------

def test_fake_refuses_a_range_below_the_grid_like_the_api():
    from .sheets_fake import APIError

    ss = FakeSpreadsheet({"_queue": [QueuedHit.field_names()]})      # header only: a 1-row grid
    with pytest.raises(APIError) as err:
        ss.sheets["_queue"].get_values("A2:C")
    assert err.value.response.status_code == 400 and "exceeds grid limits" in str(err.value)
    assert ss.sheets["_queue"].get_values("A1:C") == [QueuedHit.field_names()[:3]]


def test_header_only_tabs_in_a_one_row_grid_are_read_and_written():
    tabs = {t: [headers] for t, (headers, _) in TABS.items()}
    tabs[V1_TAB] = [V1_HEADERS]
    tabs[META_TAB] = [["key", "value"]]
    ss = FakeSpreadsheet(tabs)
    assert all(ws.row_count == 1 for ws in ss.sheets.values())
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))})
    code, stats, _ = run_sheets(ss, http, cfg(posting_enabled=True, max_posts_per_run=1), FakeBsky())
    assert code == EXIT_OK and stats.posting.posted == 1
    code, stats, _ = run_sheets(ss, http, cfg(posting_enabled=True), FakeBsky(), now=NOW + timedelta(minutes=30))
    assert code == EXIT_OK and stats.posting.posted == 1
    assert sorted(r[10] for r in ss.tab(V1_TAB)[1:]) == ["posted", "posted"]


def test_tabs_created_by_the_store_have_room_below_the_header():
    from media_embed_tracer.store.sheets_store import NEW_TAB_ROWS

    ss = FakeSpreadsheet()
    run_sheets(ss, two_article_http())
    assert ss.sheets["_seen"].row_count == NEW_TAB_ROWS + 2      # appends insert rows


def handle_queue_run(ss, http, now, rkey):
    http.pages[FEED] = rss(f"https://news.test/{rkey}")
    http.pages[f"https://news.test/{rkey}"] = page(
        f'<blockquote class="bluesky-embed"><a href="https://bsky.app/profile/alice.example.com/post/{rkey}">p'
        '</a></blockquote>')
    return run_sheets(ss, http, now=now)


def test_drained_queue_with_a_frozen_header_keeps_a_cleared_row_and_runs_on(caplog):
    """Deleting every data row is refused by the API when the header is frozen, and would
    leave a 1-row grid: the last row is cleared instead, and later runs keep working."""
    ss = FakeSpreadsheet({"_queue": [QueuedHit.field_names()]})
    ss.sheets["_queue"].frozen_row_count = 1
    http = FakeHttp({})
    resolve = f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.example.com"
    with caplog.at_level(logging.WARNING):
        for n, rkey in enumerate(("3ka", "3kb")):
            t = NOW + timedelta(hours=2 * n)
            http.pages.pop(resolve, None)
            code, _, _ = handle_queue_run(ss, http, t, rkey)                       # queued
            assert code == EXIT_OK and len(store_for(ss).load().queue) == 1
            http.pages[resolve] = {"did": DID_A}
            code, _, _ = run_sheets(ss, http, now=t + timedelta(minutes=30))       # drained
            assert code == EXIT_OK and store_for(ss, t).load().queue == []
            assert ss.sheets["_queue"].row_count >= 2
    assert "prune _queue failed" not in caplog.text
    assert [r[0] for r in ss.tab("_queue")[1:] if r] == []                      # cleared, not stale
    assert len(rows(ss, "occurrences")) == 2


def test_all_seen_rows_expired_with_a_frozen_header():
    old = "2026-01-01T00:00:00Z"
    ss = FakeSpreadsheet({"_seen": [SeenArticle.field_names(), seen_row("https://o/1", old),
                                    seen_row("https://o/2", old)]})
    ss.sheets["_seen"].frozen_row_count = 1
    store = store_for(ss)
    store.load()
    store.save(occurrences=[], articles=[], posts=[], seen=[], health=[], queue=[])
    assert store_for(ss).load().seen == {}
    assert ss.sheets["_seen"].row_count == 2 and ss.tab("_seen")[1:] == [[]]
    assert store.cells_used() == 2 * len(SeenArticle.field_names())


# --- v1 rows in the v1 formats, append retries ------------------------------------------

def plc(did, handle):
    return {f"https://plc.directory/{did}": {"alsoKnownAs": [f"at://{handle}"]},
            f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle={handle}": {"did": did}}


def test_v1_rows_use_v1_formats_and_handles_and_statuses_follow_handle_rows():
    from media_embed_tracer.resolve import Resolver

    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS], META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"),
                                                  '<a href="https://x.com/someone/status/77">x</a>'),
                     **plc(DID_B, "name.bsky.social")})   # DID_A has no verifiable handle
    bot = FakeBsky()
    conf = cfg(posting_enabled=True, max_posts_per_run=1)
    t = NOW.replace(hour=0, minute=27, second=38)
    store = store_for(ss, t, v1_resolver=Resolver(http, APPVIEW, max_lookups=20))
    jsonl_run(None, http, conf, bot, now=t, store=store)
    by_post = {r[6]: r for r in ss.tab(V1_TAB)[1:]}
    assert set(by_post) == {f"https://bsky.app/profile/{DID_A}/post/3k",
                            "https://bsky.app/profile/name.bsky.social/post/3m",
                            "https://x.com/someone/status/77"}
    b = by_post["https://bsky.app/profile/name.bsky.social/post/3m"]
    assert b[:5] == ["2026-03-01", "0:27:38", "bluesky", "news.test", "name.bsky.social"]
    assert b[9] == "2026-03-01 10:00" and b[10] == "pending"
    x = by_post["https://x.com/someone/status/77"]
    assert (x[2], x[4], x[10]) == ("twitter", "@someone", "n/a")
    assert by_post[f"https://bsky.app/profile/{DID_A}/post/3k"][4] == DID_A and \
        by_post[f"https://bsky.app/profile/{DID_A}/post/3k"][10] == "posted"
    # Next run: the pending post is quoted; its row (a handle URL) is found and updated.
    store = store_for(ss, t + timedelta(minutes=30), v1_resolver=Resolver(http, APPVIEW, max_lookups=20))
    code, _, _ = jsonl_run(None, http, conf, bot, now=t + timedelta(minutes=30), store=store)
    assert code == EXIT_OK and len(ss.tab(V1_TAB)) == 4
    assert {r[6]: r[10] for r in ss.tab(V1_TAB)[1:]}["https://bsky.app/profile/name.bsky.social/post/3m"] == "posted"


def test_v1_row_formats_exactly():
    from media_embed_tracer.store.sheets import v1_row

    x = Occurrence("x", "1853", "https://x.com/name/status/1853", "@name", "blockquote", "https://n.test/a",
                   "https://n.test/a", "N", "n.test", "f", "T", "2026-10-29T08:00:00Z", "2026-11-01T00:27:38Z")
    assert v1_row(x, "n/a") == ["2026-11-01", "0:27:38", "twitter", "n.test", "@name", "https://n.test/a",
                                "https://x.com/name/status/1853", "T", "", "2026-10-29 8:00", "n/a"]
    b = Occurrence("bluesky", f"at://{DID_A}/app.bsky.feed.post/3lx", f"https://bsky.app/profile/{DID_A}/post/3lx",
                   DID_A, "blockquote", "https://n.test/b", "https://n.test/b", "N", "n.test", "f", "T",
                   "2026-10-31T23:55:00Z", "2026-11-01T13:05:09Z")
    row = v1_row(b, "posted", "name.bsky.social")
    assert row[:2] == ["2026-11-01", "13:05:09"] and row[2] == "bluesky" and row[9] == "2026-10-31 23:55"
    assert row[4] == "name.bsky.social" and row[6] == "https://bsky.app/profile/name.bsky.social/post/3lx"
    assert v1_row(b, "pending")[4:7:2] == [DID_A, f"https://bsky.app/profile/{DID_A}/post/3lx"]   # no handle


def test_failed_v1_append_keeps_the_run_row_and_is_only_a_warning(caplog):
    from .sheets_fake import APIError

    ss = FakeSpreadsheet({V1_TAB: [V1_HEADERS], META_TAB: [["key", "value"], ["v1_imported", "x"]]})
    store = store_for(ss)
    store.load()
    occ = Occurrence("x", "1", "https://x.com/u/status/1", "@u", "link", "https://a", "https://a", "o", "a", "f",
                     "Minister quits", "", "2026-03-01T00:00:00Z")
    store.save(occurrences=[occ], articles=[], posts=[], seen=[], health=[], queue=[])
    ss.fail[("append_rows", V1_TAB)] = [APIError(503, "Minister quits")]
    with caplog.at_level(logging.WARNING):
        store.append_run(RunLog("2026-03-01T00:00:00Z", "2"))           # no raise
    assert len(rows(ss, "runs")) == 1 and len(ss.tab(V1_TAB)) == 1
    assert ss.calls.index(("append_rows", "runs")) < ss.calls.index(("append_rows", V1_TAB))
    assert "All Embeds not updated, 1 row(s) not written" in caplog.text and "Minister quits" not in caplog.text
    store.append_run(RunLog("2026-03-01T00:30:00Z", "2"))               # not retried later
    assert len(ss.tab(V1_TAB)) == 1 and len(rows(ss, "runs")) == 2


def test_fake_append_writes_after_the_first_table_like_the_api():
    ws = FakeWorksheet(FakeSpreadsheet(), "t", [["h"], ["a"], [], ["stray"]])
    ws.append_rows([["b"]], value_input_option="RAW", insert_data_option="INSERT_ROWS")
    assert ws.values == [["h"], ["a"], ["b"], [], ["stray"]]
    drained = FakeWorksheet(FakeSpreadsheet(), "d", [["h"], []])                # cleared row 2
    drained.append_rows([["x"]], value_input_option="RAW", insert_data_option="INSERT_ROWS")
    assert drained.values == [["h"], ["x"], []]


# --- safe error messages in redact mode -------------------------------------------------

def test_store_error_keeps_its_own_message_but_never_library_text_in_redact_mode():
    from media_embed_tracer.redact import RedactingFormatter, private_error

    def fmt(msg, *args):
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, msg, args, None)
        return RedactingFormatter("%(message)s", redact=True).format(rec)

    ss = FakeSpreadsheet()
    store = store_for(ss)
    store.load()
    ss.fail[("append_rows", "occurrences")] = [400]      # the fake's message holds cell text
    occ = Occurrence("x", "1", "https://x.com/i/status/1", "", "link", "https://a", "https://a", "o", "a", "f",
                     "Minister quits", "", "2026-03-01T00:00:00Z")
    with pytest.raises(StoreError) as err:
        store.save(occurrences=[occ], articles=[], posts=[], seen=[], health=[], queue=[])
    assert fmt("store save failed: %s", err.value) == "store save failed: sheets: append occurrences failed: APIError"
    assert fmt("problem: %s", private_error(err.value, "store: ")).endswith("append occurrences failed: APIError")
    assert "Minister quits" not in str(err.value)
    # Messages that may hold findings (not marked safe), and library errors: class name only.
    assert fmt("%s", StoreError("cannot write JSONL store: /data/Minister quits.jsonl")) == "StoreError"
    assert fmt("%s", private_error(StoreError("Minister quits"), "store: ")) == "store: StoreError"
    from .sheets_fake import APIError
    assert fmt("%s", APIError(400, "Minister quits")) == "APIError"


def test_mirror_error_never_embeds_library_text():
    from media_embed_tracer.store.sheets import SheetsMirror

    class Broken:
        def worksheets(self):
            raise ValueError("cell 'Minister quits' https://news.test/a1")

    with pytest.raises(StoreError) as err:
        SheetsMirror(Broken()).write(occurrences=[], articles=[], posts=[], health=[], run=RunLog("x", "2"))
    assert str(err.value) == "sheets mirror write failed: ValueError" and err.value.log_safe
