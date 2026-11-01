"""CSV export (formula-safe) and v1 sheet migration."""

import csv

from media_embed_tracer.export import export, safe_cell
from media_embed_tracer.migrate import V1_HEADERS, convert_v1
from media_embed_tracer.models import ArticleCheck, Occurrence, Post
from media_embed_tracer.store import JsonlStore
from media_embed_tracer.store.jsonl import read_partitions

DID = "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"


def test_safe_cell():
    assert safe_cell("=1+1") == "'=1+1"
    assert safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert safe_cell("plain") == "plain"
    assert safe_cell(3) == "3"


def test_export(tmp_path):
    store = JsonlStore(tmp_path / "data", tmp_path / "state")
    store.load()
    store.save(occurrences=[Occurrence("bluesky", f"at://{DID}/app.bsky.feed.post/1", "u", DID, "iframe", "https://a",
                                       "https://a", "O", "a", "f", "=cmd|' /C calc'!A0", "", "2026-03-01T00:00:00Z")],
               articles=[ArticleCheck("https://a", "https://a", "O", "a", "f", "t", "", "2026-03-01T00:00:00Z", 1, 1,
                                      embeds={"bluesky": 1}, embed_types={"iframe": 1})],
               posts=[Post("bluesky", f"at://{DID}/app.bsky.feed.post/1", "u", DID, "2026-03-01T00:00:00Z")],
               seen=[], health=[], queue=[])

    class R:
        def verified_handle(self, did):
            return "alice.example.com"

    summary = export(JsonlStore(tmp_path / "data"), tmp_path / "out", R())
    assert summary["occurrences"] == 1 and summary["articles_with_embed_markup_first_check"] == 1
    rows = list(csv.reader((tmp_path / "out" / "occurrences.csv").open()))
    assert rows[1][Occurrence.field_names().index("article_title")].startswith("'=")
    posts = list(csv.DictReader((tmp_path / "out" / "posts.csv").open()))
    assert posts[0]["author_handle"] == "@alice.example.com"
    assert (tmp_path / "out" / "articles.csv").exists()


def test_convert_v1(tmp_path):
    rows = [V1_HEADERS,
            ["2026-01-02", "03:04:05", "twitter", "news.test", "@a", "https://news.test/s?utm_source=x", "https://twitter.com/a/status/9", "T1", "<p>s</p>", "2026-01-01 10:00", "n/a"],
            ["2026-01-02", "03:04:05", "twitter", "news.test", "@a", "https://news.test/s", "https://x.com/a/status/9", "T1", "", "", "n/a"],
            ["2026-01-02", "03:04:06", "bluesky", "news.test", "x", "https://news.test/s", f"https://bsky.app/profile/{DID}/post/3k", "T1", "", "", "pending"],
            ["2026-01-02", "03:04:07", "bluesky", "news.test", "x", "https://news.test/t", f"https://bsky.app/profile/{DID}/post/3j", "T2", "", "", "posted"],
            ["2026-01-02", "03:04:08", "bluesky", "news.test", "x", "https://news.test/t", "https://bsky.app/profile/handle.example.com/post/3h", "T2", "", "", "pending"],
            ["2026-01-02", "03:04:09", "facebook", "news.test", "", "https://news.test/t", "https://www.facebook.com/", "T2", "", "", "n/a"]]
    r = convert_v1(rows)
    assert len(r.occurrences) == 3          # twitter/x duplicate merged
    assert r.skipped_rows == 2              # unresolved handle + non-post URL
    status = {p.canonical_id.rsplit("/", 1)[-1]: p.repost_status for p in r.posts}
    assert status == {"9": "n/a", "3k": "skipped", "3j": "posted"}   # pending -> skipped: no flood
    assert all(o.embed_type == "unknown" and o.detector_version == "v1" for o in r.occurrences)
    assert r.occurrences[0].first_seen == "2026-01-02T03:04:05Z"
    assert all(s.done == "1" for s in r.seen) and {s.url for s in r.seen} == {"https://news.test/s", "https://news.test/t"}
    store = JsonlStore(tmp_path / "data", tmp_path / "state")
    store.load()
    store.save(occurrences=r.occurrences, articles=[], posts=r.posts, seen=r.seen, health=[], queue=[])
    assert len(read_partitions(tmp_path / "data" / "occurrences")) == 3
    assert (tmp_path / "data" / "occurrences" / "2026-01.jsonl").exists()


def test_export_dedupes_rows_from_a_failed_state_push(tmp_path):
    """Rows re-appended after a failed state push are deduplicated on export."""
    store = JsonlStore(tmp_path / "data", tmp_path / "state")
    store.load()

    def occ(ts, cid="1", art="https://a"):
        return Occurrence("x", cid, "u", "", "link", art, art, "O", "a", "f", "t", "", ts)

    def chk(ts, n=1, url="https://a"):
        return ArticleCheck(url, url, "O", "a", "f", "t", "", ts, n, 1)

    store.save(occurrences=[occ("2026-03-01T00:00:00Z"), occ("2026-03-01T00:00:00Z", "2")],
               articles=[chk("2026-03-01T00:00:00Z")], posts=[], seen=[], health=[], queue=[])
    store.save(occurrences=[occ("2026-03-01T00:30:00Z"), occ("2026-03-01T00:30:00Z", "1", "https://b")],
               articles=[chk("2026-03-01T00:30:00Z"), chk("2026-03-02T00:30:00Z", 2)],
               posts=[], seen=[], health=[], queue=[])
    summary = export(JsonlStore(tmp_path / "data"), tmp_path / "out")
    occ_rows = list(csv.DictReader((tmp_path / "out" / "occurrences.csv").open()))
    assert [(r["article_url"], r["canonical_id"], r["first_seen"]) for r in occ_rows] == [
        ("https://a", "1", "2026-03-01T00:00:00Z"), ("https://a", "2", "2026-03-01T00:00:00Z"),
        ("https://b", "1", "2026-03-01T00:30:00Z")]          # earliest kept; other article kept
    art_rows = list(csv.DictReader((tmp_path / "out" / "articles.csv").open()))
    assert [(r["check_n"], r["checked_at"]) for r in art_rows] == [
        ("1", "2026-03-01T00:00:00Z"), ("2", "2026-03-02T00:30:00Z")]
    assert summary["duplicates_removed"] == {"occurrences": 1, "article_checks": 1}


def test_migration_accepts_v1_formats_and_rows_written_by_earlier_v2():
    from .conftest import FakeHttp

    appview = "https://appview.test"
    http = FakeHttp({f"{appview}/xrpc/com.atproto.identity.resolveHandle?handle=name.bsky.social": {"did": DID}})
    from media_embed_tracer.resolve import Resolver

    rows = [V1_HEADERS,
            # v1: H:MM:SS, "twitter", @handle, "YYYY-MM-DD H:MM"
            ["2026-11-01", "0:27:38", "twitter", "n.test", "@name", "https://n.test/a",
             "https://x.com/name/status/1853", "T", "S", "2026-10-29 8:00", "n/a"],
            ["2026-11-01", "13:05:09", "bluesky", "n.test", "name.bsky.social", "https://n.test/b",
             "https://bsky.app/profile/name.bsky.social/post/3lx", "T", "S", "2026-10-31 23:55", "pending"],
            # earlier v2: zero-padded time, "x", ISO published date, v2 statuses
            ["2026-10-20", "07:01:02", "x", "n.test", "@name", "https://n.test/c",
             "https://x.com/name/status/99", "T", "", "2026-10-19T21:00:00Z", ""],
            ["2026-10-20", "07:01:02", "bluesky", "n.test", DID, "https://n.test/d",
             f"https://bsky.app/profile/{DID}/post/3ly", "T", "", "2026-10-19T21:00:00Z", "retry"]]
    result = convert_v1(rows, Resolver(http, appview))
    assert result.skipped_rows == 0
    occ = {o.article_url: o for o in result.occurrences}
    assert (occ["https://n.test/a"].first_seen, occ["https://n.test/a"].article_published) == \
        ("2026-11-01T00:27:38Z", "2026-10-29T08:00:00Z")
    assert occ["https://n.test/b"].canonical_id == f"at://{DID}/app.bsky.feed.post/3lx"
    assert occ["https://n.test/b"].article_published == "2026-10-31T23:55:00Z"
    assert (occ["https://n.test/c"].first_seen, occ["https://n.test/c"].article_published) == \
        ("2026-10-20T07:01:02Z", "2026-10-19T21:00:00Z")
    status = {p.canonical_id[-3:]: p.repost_status for p in result.posts}
    assert status == {"853": "n/a", "3lx": "skipped", "99": "n/a", "3ly": "skipped"}
