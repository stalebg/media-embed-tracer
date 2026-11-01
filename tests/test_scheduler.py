"""Scheduling: round-robin, caps, seen-skipping, rechecks."""

from datetime import timedelta

from media_embed_tracer.feeds import Entry, FeedConfig
from media_embed_tracer.models import SeenArticle
from media_embed_tracer.scheduler import plan as _plan
from media_embed_tracer.util import iso


def plan(*a, **kw):
    return _plan(*a, **kw).tasks


def feed_entries(name, n, now, cap=None):
    f = FeedConfig(name, f"https://{name}.test/rss", per_feed_cap=cap)
    return [Entry(f"https://{name}.test/a{i}", f"t{i}", now - timedelta(minutes=i), "", f) for i in range(n)]


def test_round_robin_reaches_every_feed(now):
    feeds = {n: feed_entries(n, 50, now) for n in ("a", "b", "c", "d")}
    tasks = plan(feeds, {}, now, max_articles=8, per_feed_cap=5, rotation=0)
    assert [t.url.split("//")[1].split(".")[0] for t in tasks] == list("abcdabcd")
    assert tasks[0].url == "https://a.test/a0"  # newest first


def test_per_feed_cap(now):
    feeds = {"a": feed_entries("a", 50, now), "b": feed_entries("b", 2, now)}
    tasks = plan(feeds, {}, now, max_articles=100, per_feed_cap=3, rotation=0)
    assert len(tasks) == 5
    feeds["a"] = feed_entries("a", 50, now, cap=1)
    assert len(plan(feeds, {}, now, max_articles=100, per_feed_cap=3, rotation=0)) == 3


def test_rotation_moves_start_feed(now):
    feeds = {n: feed_entries(n, 5, now) for n in ("a", "b", "c")}
    first = [plan(feeds, {}, now, max_articles=1, rotation=r)[0].url[8] for r in range(3)]
    assert first == ["a", "b", "c"]


def test_seen_articles_are_skipped_so_later_ones_get_processed(now):
    """Seen articles are skipped, so later ones get fetched."""
    feeds = {"a": feed_entries("a", 20, now)}
    seen = {}
    fetched = []
    for _ in range(4):
        tasks = plan(feeds, seen, now, max_articles=5, per_feed_cap=5, recheck_after_hours=0)
        for t in tasks:
            fetched.append(t.url)
            seen[t.url] = SeenArticle(t.url, "a", "", "", "", "", iso(now), iso(now), checks=1, done="1")
    assert len(fetched) == len(set(fetched)) == 20


def test_recheck_once_after_24h(now):
    s = SeenArticle("https://a.test/old", "a", "", "", "", "", iso(now - timedelta(hours=25)),
                    last_checked=iso(now - timedelta(hours=25)), checks=1)
    young = SeenArticle("https://a.test/young", "a", "", "", "", "", iso(now), last_checked=iso(now), checks=1)
    done = SeenArticle("https://a.test/done", "a", "", "", "", "", "", last_checked=iso(now - timedelta(days=2)),
                       checks=2, done="1")
    tasks = plan({}, {x.url: x for x in (s, young, done)}, now, max_articles=10, recheck_after_hours=24)
    assert [(t.url, t.kind) for t in tasks] == [("https://a.test/old", "recheck")]


def test_recheck_budget_shared_with_new_articles(now):
    old = iso(now - timedelta(hours=30))
    seen = {f"https://r/{i}": SeenArticle(f"https://r/{i}", "a", "", "", "", "", old, last_checked=old, checks=1)
            for i in range(20)}
    tasks = plan({"a": feed_entries("a", 20, now)}, seen, now, max_articles=8, per_feed_cap=8)
    assert sum(t.kind == "recheck" for t in tasks) == 4   # up to half first
    assert sum(t.kind == "new" for t in tasks) == 4
    # Few new articles: leftover budget goes to more rechecks.
    tasks = plan({"a": feed_entries("a", 1, now)}, seen, now, max_articles=8, per_feed_cap=8)
    assert sum(t.kind == "recheck" for t in tasks) == 7


def test_skipped_by_cap_counted_per_feed(now):
    """Articles left over by the caps are counted per feed."""
    feeds = {"a": feed_entries("a", 10, now), "b": feed_entries("b", 3, now)}
    p = _plan(feeds, {}, now, max_articles=100, per_feed_cap=4, rotation=0)
    assert p.skipped_by_cap == {"a": 6}
    p = _plan(feeds, {}, now, max_articles=2, per_feed_cap=4, rotation=0)
    assert p.skipped_by_cap == {"a": 9, "b": 2}


def test_default_caps_cover_typical_volume(now):
    """30 feeds x 5 new articles per run fit in one run with the default caps."""
    feeds = {f"f{i}": feed_entries(f"f{i}", 5, now) for i in range(30)}
    p = _plan(feeds, {}, now)
    assert len(p.tasks) == 150 and p.skipped_by_cap == {}


def test_failed_fetches_retried_up_to_limit(now):
    feeds = {"a": feed_entries("a", 2, now)}
    seen = {
        "https://a.test/a0": SeenArticle("https://a.test/a0", "a", "", "", "", "", "", fetch_errors=1),
        "https://a.test/a1": SeenArticle("https://a.test/a1", "a", "", "", "", "", "", fetch_errors=3),
    }
    tasks = plan(feeds, seen, now, max_fetch_errors=3)
    assert [(t.url, t.kind) for t in tasks] == [("https://a.test/a0", "retry")]


def test_old_entries_and_cross_feed_duplicates(now):
    f1, f2 = FeedConfig("a", "u1"), FeedConfig("b", "u2")
    feeds = {
        "a": [Entry("https://x/1", "", now, "", f1), Entry("https://x/old", "", now - timedelta(days=30), "", f1)],
        "b": [Entry("https://x/1", "", now, "", f2)],
    }
    tasks = plan(feeds, {}, now, max_age_hours=168, rotation=0)
    assert [t.url for t in tasks] == ["https://x/1"]
