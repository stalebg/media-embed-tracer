"""Feed config and fetching."""

import json

import pytest

from media_embed_tracer.feeds import FeedConfig, FeedConfigError, fetch_feed, load_feeds, parse_feed_bytes

from .conftest import FakeHttp

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>A &lt;b&gt;bold&lt;/b&gt; &amp;amp; title</title><link>https://news.test/a?utm_source=rss&amp;id=1#top</link>
<description>&lt;p&gt;Hello &lt;a href="x"&gt;world&lt;/a&gt;&lt;/p&gt;&lt;script&gt;x&lt;/script&gt;</description>
<pubDate>Sun, 01 Mar 2026 10:00:00 +0100</pubDate></item>
<item><title>no link</title></item>
</channel></rss>"""


def test_feeds_fetched_with_http_client_not_feedparser():
    """Bytes come from the timeout-bounded client; feedparser does no network I/O."""
    http = FakeHttp({"https://news.test/rss": RSS})
    entries, err = fetch_feed(http, FeedConfig("N", "https://news.test/rss"))
    assert err == "" and http.calls == ["https://news.test/rss"]
    e = entries[0]
    assert e.url == "https://news.test/a?id=1"           # tracking params + fragment stripped
    assert e.title == "A bold & title"
    assert "<" not in e.summary and e.summary.startswith("Hello world")   # summary stripped of HTML
    assert e.published.isoformat() == "2026-03-01T09:00:00+00:00"        # UTC


def test_feed_errors_are_reported():
    entries, err = fetch_feed(FakeHttp({}), FeedConfig("N", "https://down.test/rss"))
    assert entries == [] and "404" in err
    with pytest.raises(ValueError):
        parse_feed_bytes(b"\x00\x01 not a feed <<<", FeedConfig("N", "u"))


def test_toml_file(tmp_path):
    p = tmp_path / "f.toml"
    p.write_text('[[feeds]]\nname = "A"\nurl = "https://a.test/rss"\noutlet = "A News"\nper_feed_cap = 3\n'
                 '[outlet_names]\n"www.a.test" = "A!"\n')
    fs = load_feeds(feeds_file=str(p))
    assert fs.feeds == [FeedConfig("A", "https://a.test/rss", "A News", 3)]
    assert fs.outlet_for(fs.feeds[0], "https://www.a.test/x") == "A!"
    assert fs.outlet_for(fs.feeds[0], "https://other.test/x") == "A News"


def test_feeds_json_overrides_file(tmp_path):
    p = tmp_path / "f.toml"
    p.write_text('[[feeds]]\nurl = "https://file.test/rss"\n')
    fs = load_feeds(json.dumps([{"name": "J", "url": "https://json.test/rss"}]), str(p))
    assert [f.name for f in fs.feeds] == ["J"]


@pytest.mark.parametrize("bad", ["not json", "[]", '[{"name": "x"}]', '[{"url": "u", "name": "a"}, {"url": "v", "name": "a"}]'])
def test_invalid_feed_json(bad):
    with pytest.raises(FeedConfigError):
        load_feeds(bad)


def test_shipped_config_files():
    fs = load_feeds(feeds_file="config/feeds.international.toml")
    assert len(fs.feeds) >= 5
    with pytest.raises(FeedConfigError, match="empty"):
        load_feeds(feeds_file="config/feeds.localized.toml")


def test_feed_names_json_v1_compat(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"feeds": [{"name": "G", "url": "https://g.test/rss", "outlet": "Gee"}],
                             "outlet_names": {"g.test": "From file"}}))
    fs = load_feeds(feeds_file=str(p), feed_names_json='{"www.G.test": "From secret", "h.test": "H"}')
    assert fs.outlet_for(fs.feeds[0], "https://g.test/a") == "From secret"
    assert fs.outlet_for(None, "https://www.h.test/b") == "H"
    with pytest.raises(FeedConfigError):
        load_feeds(feeds_file=str(p), feed_names_json="[1]")
