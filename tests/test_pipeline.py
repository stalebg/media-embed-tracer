"""End-to-end runs with fake HTTP, a fake Bluesky client and a real JSONL store."""

from __future__ import annotations

from datetime import timedelta

import pytest

from media_embed_tracer.bsky import LoginError, TransientError
from media_embed_tracer.config import Config
from media_embed_tracer.feeds import FeedConfig, FeedSet
from media_embed_tracer.models import DETECTOR_VERSION
from media_embed_tracer.pipeline import EXIT_BLUESKY, EXIT_FETCH, EXIT_LOGIN, EXIT_OK, EXIT_STORE, run
from media_embed_tracer.resolve import Resolver
from media_embed_tracer.store import JsonlStore, StoreError
from media_embed_tracer.store.jsonl import read_jsonl, read_partitions

from .conftest import NOW, FakeHttp

DID_A = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"
DID_B = "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb"
BOT = "did:plc:zzzzzzzzzzzzzzzzzzzzzzzz"
APPVIEW = "https://appview.test"
FEED = "https://news.test/rss"


def rss(*urls: str) -> str:
    items = "".join(f"<item><title>Title &amp; {u[-2:]}</title><link>{u}</link>"
                    f"<description>&lt;p&gt;Summary &lt;b&gt;bold&lt;/b&gt;&lt;/p&gt;</description>"
                    f"<pubDate>Sun, 01 Mar 2026 10:00:00 GMT</pubDate></item>" for u in urls)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'


def bsky_quote(did: str, rkey: str) -> str:
    return (f'<blockquote class="bluesky-embed" data-bluesky-uri="at://{did}/app.bsky.feed.post/{rkey}">'
            f'<a href="https://bsky.app/profile/{did}/post/{rkey}">post</a></blockquote>')


def page(*parts: str) -> str:
    return "<html><body><article><p>Text</p>" + "".join(parts) + "</article></body></html>"


class FakeBsky:
    def __init__(self, *, fail_login=False, fail_quotes=False, create_errors=None, quotes=None, disabled=()):
        self.did = ""
        self.fail_login, self.fail_quotes = fail_login, fail_quotes
        self.create_errors = list(create_errors or [])
        self.quotes = dict(quotes or {})
        self.disabled = set(disabled)
        self.created: list[dict] = []

    def login(self, handle, password):
        if self.fail_login:
            raise LoginError("Bluesky login failed: 401 AuthenticationRequired")
        self.did = BOT
        return BOT

    def recent_quotes(self, limit=100):
        if self.fail_quotes:
            raise TransientError("getAuthorFeed HTTP 502")
        return dict(self.quotes)

    def get_posts(self, uris):
        return {u: {"uri": u, "cid": "bafy" + u[-4:], "viewer": {"embeddingDisabled": u in self.disabled}}
                for u in uris}

    def create_post(self, record):
        if self.create_errors:
            raise self.create_errors.pop(0)
        self.created.append(record)
        uri = f"at://{BOT}/app.bsky.feed.post/q{len(self.created)}"
        self.quotes[record["embed"]["record"]["uri"]] = uri
        return uri


class RecordingMirror:
    name = "sheets"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def write(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise StoreError("quota exceeded")


def cfg(**kw) -> Config:
    base = dict(article_delay=0, post_delay=0, max_articles=50, per_feed_cap=25)
    base.update(kw)
    return Config(**base)


def feedset(*names_urls) -> FeedSet:
    feeds = names_urls or (("News", FEED),)
    return FeedSet([FeedConfig(n, u, outlet="Example News") for n, u in feeds])


def do_run(tmp_path, http, conf=None, client=None, now=NOW, mirrors=None, store=None, **kw):
    store = store or JsonlStore(tmp_path / "data", tmp_path / "state", now=now)
    code, stats = run(conf or cfg(), kw.pop("fs", feedset()), store, http, Resolver(http, APPVIEW), client,
                      mirrors, now=now, sleep=lambda s: None, **kw)
    return code, stats, store


def occurrences(tmp_path):
    return read_partitions(tmp_path / "data" / "occurrences")


def posts(tmp_path):
    return {f"{p['platform']}:{p['canonical_id']}": p for p in read_jsonl(tmp_path / "data" / "posts.jsonl")}


def articles(tmp_path):
    return read_partitions(tmp_path / "data" / "articles")


# --- dataset -----------------------------------------------------------------

def test_occurrence_per_article_and_single_post(tmp_path):
    """A post embedded by two outlets is two occurrences and one post."""
    http = FakeHttp({FEED: rss("https://news.test/a1", "https://news.test/a2"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3kaaa")),
                     "https://news.test/a2": page(bsky_quote(DID_A, "3kaaa"), bsky_quote(DID_A, "3kaaa"))})
    code, _, _ = do_run(tmp_path, http)
    assert code == EXIT_OK
    occ = occurrences(tmp_path)
    assert sorted(o["article_url"] for o in occ) == ["https://news.test/a1", "https://news.test/a2"]
    assert len(posts(tmp_path)) == 1
    o = occ[0]
    assert o["embed_type"] == "blockquote" and o["author"] == DID_A and o["outlet"] == "Example News"
    assert o["article_title"] in ("Title & a1", "Title & a2")
    assert o["first_seen"].endswith("Z") and o["detector_version"] == DETECTOR_VERSION


def test_seen_articles_not_refetched(tmp_path):
    """The second run fetches nothing already checked."""
    urls = [f"https://news.test/a{i}" for i in range(6)]
    http = FakeHttp({FEED: rss(*urls), **{u: page() for u in urls}})
    do_run(tmp_path, http, cfg(max_articles=4))
    first = [c for c in http.calls if c != FEED]
    http.calls.clear()
    do_run(tmp_path, http, cfg(max_articles=4), now=NOW + timedelta(minutes=30))
    second = [c for c in http.calls if c != FEED]
    assert len(first) == 4 and len(second) == 2 and not set(first) & set(second)


def test_articles_log_is_the_denominator(tmp_path):
    """Every check is logged, including zero-embed pages and failed fetches."""
    http = FakeHttp({FEED: rss("https://news.test/a1", "https://news.test/a2", "https://news.test/gone"),
                     "https://news.test/a1": page(),
                     "https://news.test/a2": page(bsky_quote(DID_A, "3kaaa"),
                                                  '<a href="https://x.com/u/status/42">x</a>')})
    do_run(tmp_path, http)
    rows = {a["url"]: a for a in articles(tmp_path)}
    assert rows["https://news.test/a1"]["fetch_ok"] == 1 and rows["https://news.test/a1"]["embeds"] == {}
    assert rows["https://news.test/a2"]["embeds"] == {"bluesky": 1, "x": 1}
    assert rows["https://news.test/a2"]["embed_types"] == {"blockquote": 1, "link": 1}
    assert rows["https://news.test/gone"]["fetch_ok"] == 0 and rows["https://news.test/gone"]["http_status"] == 404
    assert all(r["check_n"] == 1 and r["detector_version"] == DETECTOR_VERSION for r in rows.values())
    assert (tmp_path / "data" / "articles" / "2026-03.jsonl").exists()


def test_final_url_sets_outlet_and_domain(tmp_path):
    """Outlet and domain come from the URL after redirects."""
    http = FakeHttp({FEED: rss("https://feedproxy.test/x1"), "https://feedproxy.test/x1": page(bsky_quote(DID_A, "3k"))},
                    final={"https://feedproxy.test/x1": "https://www.realnews.test/story"})
    fs = FeedSet([FeedConfig("News", FEED)], outlet_names={"realnews.test": "Real News"})
    do_run(tmp_path, http, fs=fs)
    o = occurrences(tmp_path)[0]
    assert (o["article_url"], o["final_url"], o["domain"], o["outlet"]) == (
        "https://feedproxy.test/x1", "https://www.realnews.test/story", "realnews.test", "Real News")


def test_recheck_after_24h_adds_only_new_embeds(tmp_path):
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k1"))})
    do_run(tmp_path, http)
    http.pages["https://news.test/a1"] = page(bsky_quote(DID_A, "3k1"), '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>')
    do_run(tmp_path, http, now=NOW + timedelta(hours=6))   # not yet due
    assert len(occurrences(tmp_path)) == 1
    do_run(tmp_path, http, now=NOW + timedelta(hours=25))
    assert [o["platform"] for o in occurrences(tmp_path)] == ["bluesky", "youtube"]
    assert [a["check_n"] for a in articles(tmp_path)] == [1, 2]
    http.calls.clear()
    do_run(tmp_path, http, now=NOW + timedelta(hours=50))  # rechecked once only
    assert http.calls == [FEED]


def test_unresolved_handle_is_queued_not_persisted(tmp_path):
    """Handle-form keys are queued and never reach the dataset."""
    art = "https://news.test/a1"
    html = page('<blockquote class="bluesky-embed"><a href="https://bsky.app/profile/alice.example.com/post/3kh">p</a></blockquote>')
    http = FakeHttp({FEED: rss(art), art: html})
    code, stats, _ = do_run(tmp_path, http)
    assert code == EXIT_OK and stats.queued == 1
    assert occurrences(tmp_path) == [] and posts(tmp_path) == {}
    assert len(read_jsonl(tmp_path / "state" / "resolve_queue.jsonl")) == 1
    http.pages[f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.example.com"] = {"did": DID_A}
    do_run(tmp_path, http, now=NOW + timedelta(minutes=30))
    occ = occurrences(tmp_path)
    assert [o["canonical_id"] for o in occ] == [f"at://{DID_A}/app.bsky.feed.post/3kh"]
    assert occ[0]["article_url"] == art and occ[0]["embed_type"] == "blockquote"
    assert read_jsonl(tmp_path / "state" / "resolve_queue.jsonl") == []


def test_unresolvable_queue_entries_are_dropped_after_max_attempts(tmp_path):
    art = "https://news.test/a1"
    http = FakeHttp({FEED: rss(art), art: page('<a href="https://bsky.app/profile/gone.example.com/post/3kh">p</a>')})
    conf = cfg(resolve_max_attempts=3)
    for i in range(4):
        _, stats, _ = do_run(tmp_path, http, conf, now=NOW + timedelta(minutes=30 * i))
    assert stats.dropped == 1 and read_jsonl(tmp_path / "state" / "resolve_queue.jsonl") == []


def test_skipped_by_cap_in_run_log_and_health(tmp_path):
    urls = [f"https://news.test/a{i}" for i in range(10)]
    http = FakeHttp({FEED: rss(*urls), **{u: page() for u in urls}})
    do_run(tmp_path, http, cfg(per_feed_cap=3))
    run_row = read_partitions(tmp_path / "data" / "runs")[0]
    assert run_row["skipped_by_cap"] == {"News": 7} and run_row["planned"] == 3
    import json
    health = json.loads((tmp_path / "state" / "feed_health.json").read_text())
    assert health[0]["last_skipped_by_cap"] == 7 and health[0]["consecutive_failures"] == 0


def test_time_budget_defers_remaining_fetches(tmp_path):
    urls = [f"https://news.test/a{i}" for i in range(5)]
    http = FakeHttp({FEED: rss(*urls), **{u: page() for u in urls}})
    ticks = iter([0, 0, 0, 10_000, 10_000, 10_000, 10_000])
    _, stats, _ = do_run(tmp_path, http, clock=lambda: next(ticks))
    assert stats.articles_ok == 2 and stats.skipped_by_cap == {"News": 3}


# --- exit codes ----------------------------------------------------------------

def test_majority_of_article_fetches_failing_turns_run_red(tmp_path):
    urls = [f"https://news.test/a{i}" for i in range(5)]
    http = FakeHttp({FEED: rss(*urls), urls[0]: page()})
    code, stats, _ = do_run(tmp_path, http)
    assert code == EXIT_FETCH and stats.articles_failed == 4
    assert len(articles(tmp_path)) == 5  # still saved


def test_all_feeds_failing_turns_run_red(tmp_path):
    code, _, _ = do_run(tmp_path, FakeHttp({}))
    assert code == EXIT_FETCH


class FailingStore(JsonlStore):
    def __init__(self, *a, fail_save=False, fail_update=False, **kw):
        super().__init__(*a, **kw)
        self.fail_save, self.fail_update = fail_save, fail_update

    def save(self, **kw):
        if self.fail_save:
            raise StoreError("disk full")
        return super().save(**kw)

    def update_post(self, post):
        if self.fail_update:
            raise StoreError("disk full")
        return super().update_post(post)


def test_store_failure_exits_2_and_never_posts(tmp_path):
    """A store write failure exits 2 and nothing is posted."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    store = FailingStore(tmp_path / "data", tmp_path / "state", fail_save=True)
    code, _, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot, store=store)
    assert code == EXIT_STORE and bot.created == []


# --- posting ----------------------------------------------------------------

def test_disabled_posting_marks_skipped_and_enabling_does_not_flood(tmp_path):
    """With posting off, embeds become skipped; turning it on later does not flood."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3kold"))})
    do_run(tmp_path, http, cfg(posting_enabled=False))
    assert posts(tmp_path)[f"bluesky:at://{DID_A}/app.bsky.feed.post/3kold"]["repost_status"] == "skipped"
    http.pages[FEED] = rss("https://news.test/a1", "https://news.test/a2")
    http.pages["https://news.test/a2"] = page(bsky_quote(DID_B, "3knew"))
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(hours=1))
    assert [r["embed"]["record"]["uri"] for r in bot.created] == [f"at://{DID_B}/app.bsky.feed.post/3knew"]


def test_quote_record_contents(tmp_path):
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    code, stats, _ = do_run(tmp_path, http, cfg(posting_enabled=True, quote_langs=["nb"]), bot)
    assert code == EXIT_OK and stats.posting.posted == 1
    rec = bot.created[0]
    assert rec["langs"] == ["nb"]
    assert rec["text"].startswith("Embedded by Example News: Title & a1 ")
    assert rec["facets"][0]["features"][0]["uri"] == "https://news.test/a1"
    p = posts(tmp_path)[f"bluesky:at://{DID_A}/app.bsky.feed.post/3k"]
    assert p["repost_status"] == "posted" and p["quoted_uri"] == f"at://{BOT}/app.bsky.feed.post/q1"


def test_same_post_in_two_articles_quoted_once(tmp_path):
    """A post in two articles gets one pending row and one quote."""
    http = FakeHttp({FEED: rss("https://news.test/a1", "https://news.test/a2"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k")),
                     "https://news.test/a2": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert len(bot.created) == 1


LINK = f'<a href="https://bsky.app/profile/{DID_A}/post/3klink">plain link</a>'
SCRIPT = f'<script>var d={{"u":"https:\\/\\/bsky.app\\/profile\\/{DID_A}\\/post\\/3klink"}}</script>'


@pytest.mark.parametrize(("markup", "quote_links"), [(SCRIPT, True), (SCRIPT, False), (LINK, False)])
def test_script_data_and_links_with_quote_links_off_are_recorded_but_not_quoted(tmp_path, markup, quote_links):
    """script-data, and links with QUOTE_LINKS off, are recorded but not quoted."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(markup)})
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True, quote_links=quote_links), bot)
    assert bot.created == []
    (p,) = posts(tmp_path).values()
    assert p["repost_status"] == "n/a"
    assert occurrences(tmp_path)[0]["embed_type"] in ("link", "script-data")


def test_linked_post_is_quoted_by_default_as_in_v1(tmp_path):
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(LINK)})
    bot = FakeBsky()
    code, stats, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_OK and stats.posting.posted == 1
    assert bot.created[0]["embed"]["record"]["uri"] == f"at://{DID_A}/app.bsky.feed.post/3klink"


def test_no_daily_caps_by_default_only_the_per_run_limit(tmp_path):
    arts = [f"https://news.test/a{i}" for i in range(12)]
    pages = {FEED: rss(*arts)}
    for i, a in enumerate(arts):
        pages[a] = page(bsky_quote(DID_A, f"3ka{i:02d}"))   # 12 posts by one author
    http = FakeHttp(pages)
    bot = FakeBsky()
    conf = cfg(posting_enabled=True, daily_cap=0, author_daily_cap=0)
    _, stats, _ = do_run(tmp_path, http, conf, bot)
    assert len(bot.created) == 10 and stats.posting.held_by_cap == 2   # MAX_POSTS_PER_RUN = 10
    do_run(tmp_path, http, conf, bot, now=NOW + timedelta(minutes=30))
    assert len(bot.created) == 12


def test_both_instances_may_quote_the_same_post(tmp_path):
    """Each instance has its own store and its own account: the same post embedded by
    outlets of both instances is quoted once by each bot."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bots = [FakeBsky(), FakeBsky()]
    for name, bot in zip(("international", "localized"), bots, strict=True):
        store = JsonlStore(tmp_path / name / "data", tmp_path / name / "state", now=NOW)
        code, _, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot, store=store)
        assert code == EXIT_OK
    assert [len(b.created) for b in bots] == [1, 1]


def test_login_failure_exits_3_and_leaves_rows_pending(tmp_path):
    """A failed login exits 3 and leaves rows pending."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    code, _, _ = do_run(tmp_path, http, cfg(posting_enabled=True), FakeBsky(fail_login=True))
    assert code == EXIT_LOGIN
    assert posts(tmp_path)[f"bluesky:at://{DID_A}/app.bsky.feed.post/3k"]["repost_status"] == "pending"
    assert len(occurrences(tmp_path)) == 1  # data saved before posting
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=30))
    assert len(bot.created) == 1


def test_recent_quotes_failure_exits_non_zero_and_posts_nothing(tmp_path):
    """If reading the bot's recent quotes fails, nothing is posted and the run exits non-zero."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky(fail_quotes=True)
    code, _, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_BLUESKY and bot.created == []
    assert posts(tmp_path)[f"bluesky:at://{DID_A}/app.bsky.feed.post/3k"]["repost_status"] == "pending"


def test_status_update_failure_then_guard_prevents_double_post(tmp_path):
    """A lost status write does not lead to a second quote on the next run."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    store = FailingStore(tmp_path / "data", tmp_path / "state", now=NOW, fail_update=True)
    code, _, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot, store=store)
    assert code == EXIT_STORE and len(bot.created) == 1
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=30))
    assert len(bot.created) == 1
    p = posts(tmp_path)[f"bluesky:at://{DID_A}/app.bsky.feed.post/3k"]
    assert p["repost_status"] == "posted" and p["quoted_uri"] == f"at://{BOT}/app.bsky.feed.post/q1"


def test_transient_create_error_retries_later(tmp_path):
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky(create_errors=[TransientError("createRecord HTTP 503")])
    do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    p = posts(tmp_path)[f"bluesky:at://{DID_A}/app.bsky.feed.post/3k"]
    assert (p["repost_status"], p["attempts"]) == ("retry", 1)
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=10))
    assert bot.created == []  # not due yet
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=40))
    assert len(bot.created) == 1


def test_quote_disabled_by_author_is_skipped(tmp_path):
    uri = f"at://{DID_A}/app.bsky.feed.post/3k"
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky(disabled={uri})
    do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert bot.created == [] and posts(tmp_path)[f"bluesky:{uri}"]["repost_status"] == "skipped"


def test_daily_author_caps_and_opt_out(tmp_path):
    """Daily and per-author caps hold posts back; opted-out authors are skipped."""
    arts = [f"https://news.test/a{i}" for i in range(6)]
    pages = {FEED: rss(*arts)}
    for i, a in enumerate(arts[:4]):
        pages[a] = page(bsky_quote(DID_A, f"3ka{i}"))      # 4 posts by one author
    pages[arts[4]] = page(bsky_quote(DID_B, "3kb0"))
    pages[arts[5]] = page(bsky_quote("did:plc:cccccccccccccccccccccccc", "3kc0"))
    http = FakeHttp(pages)
    bot = FakeBsky()
    conf = cfg(posting_enabled=True, author_daily_cap=2, daily_cap=30,
               opt_out_dids=frozenset({"did:plc:cccccccccccccccccccccccc"}))
    _, stats, _ = do_run(tmp_path, http, conf, bot)
    authors = [r["embed"]["record"]["uri"].split("/")[2] for r in bot.created]
    assert authors.count(DID_A) == 2 and authors.count(DID_B) == 1
    assert stats.posting.held_by_cap == 2 and stats.posting.skipped == 1
    st = {k.rsplit("/", 1)[1]: v["repost_status"] for k, v in posts(tmp_path).items()}
    assert st["3kc0"] == "skipped" and sorted(st.values()).count("pending") == 2

    conf.daily_cap = 3
    bot2 = FakeBsky(quotes=bot.quotes)
    do_run(tmp_path, http, conf, bot2, now=NOW + timedelta(minutes=30))
    assert bot2.created == []  # 3 already posted in the last 24 h


# --- mirrors -------------------------------------------------------------------

def test_mirror_written_once_at_end_with_final_post_state(tmp_path):
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    mirror = RecordingMirror()
    code, _, _ = do_run(tmp_path, http, cfg(posting_enabled=True), FakeBsky(), mirrors=[mirror])
    assert code == EXIT_OK and len(mirror.calls) == 1
    call = mirror.calls[0]
    assert [p.repost_status for p in call["posts"]] == ["posted"]
    assert len(call["occurrences"]) == 1 and len(call["articles"]) == 1 and call["run"].articles_ok == 1


def test_mirror_failure_keeps_data_but_exits_2(tmp_path):
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page()})
    code, _, _ = do_run(tmp_path, http, mirrors=[RecordingMirror(fail=True)])
    assert code == EXIT_STORE and len(articles(tmp_path)) == 1


def test_post_in_many_articles_of_one_outlet_is_page_furniture_and_never_quoted(tmp_path, caplog):
    """Safety net for teaser boxes the detector misses: the v1 bot quoted one post for 8-20
    articles of one outlet. Occurrences are kept (flagged), the post is never quoted."""
    arts = [f"https://news.test/a{i}" for i in range(8)]
    http = FakeHttp({FEED: rss(*arts), **{a: page(bsky_quote(DID_A, "3kbox")) for a in arts}})
    bot = FakeBsky()
    with caplog.at_level("WARNING"):
        code, _stats, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_OK and bot.created == []
    occ = occurrences(tmp_path)
    assert len(occ) == 8 and {o["embed_type"] for o in occ} == {"blockquote+template"}
    (p,) = posts(tmp_path).values()
    assert p["repost_status"] == "n/a"
    assert "1 post(s) found in 3+ articles of one outlet" in caplog.text and "news.test/a" not in caplog.text


def test_template_flag_also_cancels_an_earlier_pending_quote(tmp_path):
    http = FakeHttp({FEED: rss("https://news.test/a0"), "https://news.test/a0": page(bsky_quote(DID_A, "3kbox"))})
    do_run(tmp_path, http, cfg(posting_enabled=True, max_posts_per_run=0), FakeBsky())
    assert next(iter(posts(tmp_path).values()))["repost_status"] == "pending"
    arts = [f"https://news.test/b{i}" for i in range(3)]
    http.pages[FEED] = rss(*arts)
    http.pages.update({a: page(bsky_quote(DID_A, "3kbox")) for a in arts})
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=30))
    assert bot.created == [] and next(iter(posts(tmp_path).values()))["repost_status"] == "skipped"


def test_each_post_is_quoted_at_most_once_across_articles_and_runs(tmp_path):
    """Posts are keyed by canonical id: two articles (below the template threshold), then
    more articles in later runs, still give exactly one quote."""
    http = FakeHttp({FEED: rss("https://news.test/a1", "https://news.test/a2"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k")),
                     "https://news.test/a2": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    for i in range(3, 6):
        http.pages[FEED] = rss(f"https://news.test/a{i}")
        http.pages[f"https://news.test/a{i}"] = page(bsky_quote(DID_A, "3k"))
        do_run(tmp_path, http, cfg(posting_enabled=True), bot, now=NOW + timedelta(minutes=30 * i))
    assert len(bot.created) == 1 and len(occurrences(tmp_path)) == 5


def test_page_furniture_counts_only_this_runs_articles(tmp_path):
    """A post re-embedded in one article per run keeps its plain label (one label per post
    in the dataset); earlier runs' articles are not counted."""
    http = FakeHttp({})
    for i in range(4):
        http.pages[FEED] = rss(f"https://news.test/a{i}")
        http.pages[f"https://news.test/a{i}"] = page(bsky_quote(DID_A, "3kbox"))
        do_run(tmp_path, http, now=NOW + timedelta(minutes=30 * i))
    assert [o["embed_type"] for o in occurrences(tmp_path)] == ["blockquote"] * 4


def test_an_article_that_breaks_the_parser_is_retried_not_consumed(tmp_path, monkeypatch, caplog):
    """A crash is our bug, not a fetch error: no check row, no fetch_errors, the article is
    tried again next run, and the run exits non-zero so it stays visible."""
    import media_embed_tracer.pipeline as pipeline

    real = pipeline.detect

    def detect(html):
        if "BOOM" in html:
            raise ValueError("Minister quits: secret title")
        return real(html)

    monkeypatch.setattr(pipeline, "detect", detect)
    bad, good = "https://news.test/bad", "https://news.test/ok"
    http = FakeHttp({FEED: rss(bad, good), bad: page("BOOM"), good: page(bsky_quote(DID_A, "3k"))})
    with caplog.at_level("WARNING"):
        code, stats, _ = do_run(tmp_path, http)
    assert code == EXIT_FETCH and stats.crashed == 1 and (stats.articles_ok, stats.articles_failed) == (1, 0)
    assert [o["article_url"] for o in occurrences(tmp_path)] == [good]
    assert [a["url"] for a in articles(tmp_path)] == [good]
    assert "ValueError" in caplog.text and "Minister quits" not in caplog.text
    for i in range(1, 6):
        do_run(tmp_path, http, now=NOW + timedelta(minutes=30 * i))
    assert http.calls.count(bad) == 6                     # retried every run, never given up on
    http.pages[bad] = page(bsky_quote(DID_B, "3m"))       # fixed: checked normally
    code, _, _ = do_run(tmp_path, http, now=NOW + timedelta(hours=4))
    assert code == EXIT_OK and bad in [o["article_url"] for o in occurrences(tmp_path)]


def test_an_unexpected_fetch_exception_is_a_crash_not_a_failed_check(tmp_path):
    class Exploding(FakeHttp):
        def fetch_html(self, url):
            if url.endswith("/bad"):
                raise UnicodeError("label too long")
            return super().fetch_html(url)

    http = Exploding({FEED: rss("https://news.test/bad", "https://news.test/ok"),
                      "https://news.test/ok": page(bsky_quote(DID_A, "3k"))})
    code, stats, _ = do_run(tmp_path, http)
    assert code == EXIT_FETCH and (stats.articles_ok, stats.articles_failed, stats.crashed) == (1, 0, 1)
    assert [a["url"] for a in articles(tmp_path)] == ["https://news.test/ok"]


def test_an_unexpected_error_on_one_post_marks_it_failed_and_the_run_goes_on(tmp_path, caplog):
    class Flaky(FakeBsky):
        def get_posts(self, uris):
            if uris[0].endswith("/3k"):
                raise RuntimeError("Minister quits")
            return super().get_posts(uris)

    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))})
    bot = Flaky()
    with caplog.at_level("WARNING"):
        code, stats, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_OK and stats.posting.posted == 1 and stats.posting.failed == 1
    status = {p["canonical_id"][-2:]: (p["repost_status"], p["last_error"]) for p in posts(tmp_path).values()}
    assert status == {"3k": ("failed", "unexpected error: RuntimeError"), "3m": ("posted", "")}
    assert len(read_partitions(tmp_path / "data" / "runs")) == 1          # append_run reached
    assert "Minister quits" not in caplog.text


def test_an_error_after_a_successful_quote_never_marks_the_post_failed(tmp_path, monkeypatch, caplog):
    import media_embed_tracer.posting as posting

    def broken_mark_posted(post, uri, now):
        raise RuntimeError("Minister quits")

    monkeypatch.setattr(posting.repost, "mark_posted", broken_mark_posted)
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3k"))})
    bot = FakeBsky()
    with caplog.at_level("WARNING"):
        code, stats, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_OK and len(bot.created) == 1 and (stats.posting.posted, stats.posting.failed) == (1, 0)
    (p,) = posts(tmp_path).values()
    assert p["repost_status"] == "posted" and p["quoted_uri"].endswith("/q1")
    assert "RuntimeError" in caplog.text and "Minister quits" not in caplog.text


def test_an_unexpected_create_error_leaves_the_status_and_stops_posting(tmp_path, caplog):
    """createRecord may or may not have applied: not marked failed (the next run's own-posts
    check repairs it), no further quotes this run, the run exits non-zero."""
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(bsky_quote(DID_A, "3k"), bsky_quote(DID_B, "3m"))})
    bot = FakeBsky(create_errors=[RuntimeError("Minister quits")])
    with caplog.at_level("WARNING"):
        code, stats, _ = do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert code == EXIT_BLUESKY and bot.created == [] and stats.posting.failed == 0
    assert {p["repost_status"] for p in posts(tmp_path).values()} == {"pending"}
    assert len(read_partitions(tmp_path / "data" / "runs")) == 1
    assert "RuntimeError" in caplog.text and "Minister quits" not in caplog.text


# --- external review fixes ---------------------------------------------------------

def test_a_recovered_quote_counts_toward_the_daily_caps(tmp_path):
    """A quote whose status write was lost is repaired from the bot's own feed and counted
    before the caps are applied: with a daily cap of 1, no second quote follows."""
    http = FakeHttp({FEED: rss("https://news.test/a1"), "https://news.test/a1": page(bsky_quote(DID_A, "3kz"))})
    bot = FakeBsky()
    store = FailingStore(tmp_path / "data", tmp_path / "state", now=NOW, fail_update=True)
    conf = cfg(posting_enabled=True, daily_cap=1)
    do_run(tmp_path, http, conf, bot, store=store)
    assert len(bot.created) == 1
    # Next run: an earlier-sorted new post is due too (first_embed_seen ties: key order "3ka" < "3kz").
    http.pages[FEED] = rss("https://news.test/a2")
    http.pages["https://news.test/a2"] = page(bsky_quote(DID_A, "3ka"))
    _, stats, _ = do_run(tmp_path, http, conf, bot, now=NOW)
    assert len(bot.created) == 1 and stats.posting.already_quoted == 1 and stats.posting.held_by_cap == 1


def test_script_data_handle_and_official_embed_of_one_post_are_one_quotable_hit(tmp_path):
    """Handle-form URL in script data + DID blockquote of the same post: after resolution
    they are one hit with the stronger type (one occurrence, quoted, counted once)."""
    script = '<script>var d={"u":"https:\\/\\/bsky.app\\/profile\\/alice.test\\/post\\/3kh"}</script>'
    http = FakeHttp({FEED: rss("https://news.test/a1"),
                     "https://news.test/a1": page(script, bsky_quote(DID_A, "3kh")),
                     f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.test": {"did": DID_A}})
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot)
    assert [o["embed_type"] for o in occurrences(tmp_path)] == ["blockquote"]
    assert articles(tmp_path)[0]["embeds"] == {"bluesky": 1}
    assert len(bot.created) == 1


def test_page_furniture_of_one_outlet_does_not_suppress_a_real_embed_elsewhere(tmp_path):
    other = "https://other.test/rss"
    arts = [f"https://news.test/a{i}" for i in range(3)]
    script = f'<script>var d={{"u":"https:\\/\\/bsky.app\\/profile\\/{DID_A}\\/post\\/3kx"}}</script>'
    http = FakeHttp({FEED: rss(*arts), **{a: page(script) for a in arts},
                     other: rss("https://other.test/b1"), "https://other.test/b1": page(bsky_quote(DID_A, "3kx"))})
    fs = FeedSet([FeedConfig("News", FEED, outlet="Example News"), FeedConfig("Other", other, outlet="Other News")])
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, fs=fs)
    types = {o["outlet"]: o["embed_type"] for o in occurrences(tmp_path)}
    assert types == {"Example News": "script-data+template", "Other News": "blockquote"}
    assert len(bot.created) == 1


def test_furniture_in_one_outlet_does_not_cancel_a_pending_quote_from_another(tmp_path):
    other = "https://other.test/rss"
    fs = FeedSet([FeedConfig("News", FEED, outlet="Example News"), FeedConfig("Other", other, outlet="Other News")])
    http = FakeHttp({FEED: rss(), other: rss("https://other.test/b1"),
                     "https://other.test/b1": page(bsky_quote(DID_A, "3kx"))})
    do_run(tmp_path, http, cfg(posting_enabled=True, max_posts_per_run=0), FakeBsky(), fs=fs)
    arts = [f"https://news.test/a{i}" for i in range(3)]
    http.pages[FEED] = rss(*arts)
    http.pages.update({a: page(bsky_quote(DID_A, "3kx")) for a in arts})
    bot = FakeBsky()
    do_run(tmp_path, http, cfg(posting_enabled=True), bot, fs=fs, now=NOW + timedelta(minutes=30))
    assert len(bot.created) == 1
