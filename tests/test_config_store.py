"""Config parsing and JSONL store layout."""

from datetime import timedelta

import pytest

from media_embed_tracer.config import ConfigError, from_env, load_opt_out
from media_embed_tracer.models import FeedHealth, Occurrence, Post, QueuedHit, RunLog, SeenArticle
from media_embed_tracer.store import JsonlStore
from media_embed_tracer.util import iso


def test_defaults():
    c = from_env({})
    # As v1: no daily caps (0 = unlimited), 10 quotes per run, linked posts are quoted too.
    assert (c.max_articles, c.per_feed_cap, c.posting_enabled, c.daily_cap, c.author_daily_cap) == (150, 25, False, 0, 0)
    assert c.max_posts_per_run == 10 and c.quote_links is True
    assert from_env({"QUOTE_LINKS": "false"}).quote_links is False
    assert c.data_dir == "data" and c.state_dir == "data/state"
    assert c.quote_langs == ["en"]
    assert "app_password" not in repr(c) or "secret" not in repr(c)


def test_secrets_not_in_repr():
    c = from_env({"BLUESKY_APP_PASSWORD": "hunter2", "GOOGLE_CREDENTIALS_JSON": "{\"private_key\": 1}"})
    assert "hunter2" not in repr(c) and "private_key" not in repr(c)


@pytest.mark.parametrize("env", [
    {"MAX_ARTICLES": "lots"},
    {"FAIL_THRESHOLD": "0"},
    {"QUOTE_TEMPLATE": "{outlet} {secret}"},
    {"SPREADSHEET_URL": "https://docs.google.com/spreadsheets/d/x"},
    {"BLUESKY_OPT_OUT_DIDS": "someone.bsky.social"},
])
def test_invalid(env):
    with pytest.raises(ConfigError):
        from_env(env)


def test_opt_out_file(tmp_path):
    f = tmp_path / "o.txt"
    f.write_text("# comment\ndid:plc:aaaaaaaaaaaaaaaaaaaaaaaa  # asked 2026-01\n\n")
    assert load_opt_out("did:web:example.com", str(f)) == {"did:plc:aaaaaaaaaaaaaaaaaaaaaaaa", "did:web:example.com"}
    assert load_opt_out("", "config/bluesky-opt-out.txt") == frozenset()


def test_jsonl_layout_and_roundtrip(tmp_path, now):
    s = JsonlStore(tmp_path / "data", tmp_path / "state", seen_retention_days=30, now=now)
    s.load()
    old = SeenArticle("https://old", "f", "", "", "", "", iso(now - timedelta(days=40)), iso(now - timedelta(days=40)))
    new = SeenArticle("https://new", "f", "", "", "", "", iso(now), iso(now), checks=1, found_keys="x:1")
    s.save(occurrences=[Occurrence("x", "1", "u", "", "link", "a", "a", "o", "d", "f", "t", "", "2026-02-28T23:59:59Z"),
                        Occurrence("x", "2", "u", "", "link", "a", "a", "o", "d", "f", "t", "", "2026-03-01T00:00:00Z")],
           articles=[], posts=[Post("x", "1", "u", "", "t")], seen=[old, new], health=[FeedHealth("f", "u")],
           queue=[QueuedHit("https://bsky.app/profile/h.test/post/1", "link", "a", "a", "o", "d", "f", "t", "", "")])
    s.append_run(RunLog(iso(now), "2.0.0", skipped_by_cap={"f": 3}))
    assert sorted(p.name for p in (tmp_path / "data" / "occurrences").iterdir()) == ["2026-02.jsonl", "2026-03.jsonl"]
    assert (tmp_path / "data" / "posts.jsonl").exists() and (tmp_path / "data" / "runs" / "2026-03.jsonl").exists()
    for name in ("seen_articles.jsonl", "feed_health.json", "resolve_queue.jsonl"):
        assert (tmp_path / "state" / name).exists()
    st = JsonlStore(tmp_path / "data", tmp_path / "state").load()
    assert set(st.seen) == {"https://new"}           # pruned by retention
    assert st.seen["https://new"].keys() == {"x:1"} and st.seen["https://new"].checks == 1
    assert len(st.queue) == 1 and "x:1" in st.posts and "f" in st.health


def test_truncated_last_line_is_skipped_not_fatal(tmp_path, caplog):
    """A partial last line from a killed run is skipped with a warning, not fatal."""
    data = tmp_path / "data"
    s = JsonlStore(data, tmp_path / "state")
    s.load()
    occ = Occurrence("x", "1", "u", "", "link", "a", "a", "o", "d", "f", "t", "", "2026-03-01T00:00:00Z")
    s.save(occurrences=[occ], articles=[], posts=[], seen=[], health=[], queue=[])
    part = data / "occurrences" / "2026-03.jsonl"
    with part.open("a") as fh:
        fh.write('{"platform":"x","canonical_id":"2","post_')   # truncated, no newline
    (data / "posts.jsonl").write_text('{not json\n{"platform":"x","canonical_id":"9","post_url":"u","author":"","first_seen":"t"}\n')
    s2 = JsonlStore(data, tmp_path / "state")
    assert set(s2.load().posts) == {"x:9"}                 # bad line skipped, good line kept
    assert [o.canonical_id for o in s2.read_occurrences()] == ["1"]
    assert "malformed" in caplog.text
    occ3 = Occurrence("x", "3", "u", "", "link", "a", "a", "o", "d", "f", "t", "", "2026-03-01T01:00:00Z")
    s2.save(occurrences=[occ3], articles=[], posts=[], seen=[], health=[], queue=[])
    assert [o.canonical_id for o in JsonlStore(data).read_occurrences()] == ["1", "3"]   # new row intact
    assert part.read_text().endswith("\n")


def test_append_lines_single_write(tmp_path):
    from media_embed_tracer.store.jsonl import append_lines
    p = tmp_path / "a.jsonl"
    append_lines(p, ['{"a":1}', '{"a":2}'])
    append_lines(p, [])
    append_lines(p, ['{"a":3}'])
    assert p.read_text() == '{"a":1}\n{"a":2}\n{"a":3}\n'


def test_max_run_minutes_default_is_10():
    """The default fetch budget fits inside the 15-minute offset between instances."""
    assert from_env({}).max_run_minutes == 10
