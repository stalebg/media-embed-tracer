"""Repost state machine transitions."""

from datetime import timedelta

import pytest

from media_embed_tracer import repost
from media_embed_tracer.models import Post
from media_embed_tracer.util import iso


def post(status="n/a", **kw):
    return Post("bluesky", "at://did:plc:a/app.bsky.feed.post/1", "u", "", "2026-03-01T00:00:00Z",
                repost_status=status, **kw)


def test_posting_disabled_means_skipped(now):
    p = post()
    assert repost.on_real_embed(p, now, False, "https://a", "t", "o")
    assert p.repost_status == "skipped"


def test_posting_enabled_means_pending(now):
    p = post()
    repost.on_real_embed(p, now, True, "https://a", "t", "o")
    assert (p.repost_status, p.first_embed_seen, p.via_outlet) == ("pending", iso(now), "o")


def test_only_bluesky_embeds_and_links_are_quotable():
    assert repost.is_quotable("bluesky", "blockquote")
    assert not repost.is_quotable("bluesky", "script-data")  # v1 did not scan script data either
    assert not repost.is_quotable("bluesky", "unknown")      # only migrated rows have it
    assert repost.is_quotable("bluesky", "link")             # as v1 (QUOTE_LINKS default on)
    assert not repost.is_quotable("bluesky", "link", quote_links=False)
    assert not repost.is_quotable("x", "blockquote")
    assert not repost.is_quotable("x", "link")


def test_real_embed_does_not_touch_settled_posts(now):
    for status in ("pending", "posted", "skipped", "failed", "retry"):
        p = post(status)
        assert not repost.on_real_embed(p, now, True, "a", "t", "o")
        assert p.repost_status == status


def test_stale_pending_becomes_skipped(now):
    p = post("pending", first_embed_seen=iso(now - timedelta(hours=30)))
    assert repost.expire_if_stale(p, now, 24)
    assert p.repost_status == "skipped"
    fresh = post("pending", first_embed_seen=iso(now - timedelta(hours=2)))
    assert not repost.expire_if_stale(fresh, now, 24)


def test_transient_retry_backoff_then_failed(now):
    p = post("pending")
    repost.mark_transient(p, "503", now, max_attempts=3)
    assert (p.repost_status, p.attempts, p.next_try) == ("retry", 1, iso(now + timedelta(minutes=30)))
    assert not repost.is_due(p, now)
    assert repost.is_due(p, now + timedelta(minutes=31))
    repost.mark_transient(p, "503", now, max_attempts=3)
    assert p.next_try == iso(now + timedelta(minutes=60))
    repost.mark_transient(p, "503", now, max_attempts=3)
    assert p.repost_status == "failed" and p.attempts == 3 and "gave up" in p.last_error


def test_backoff_is_capped(now):
    p = post("retry", attempts=20)
    repost.mark_transient(p, "x", now, max_attempts=99, cap_hours=12)
    assert p.next_try == iso(now + timedelta(hours=12))


def test_posted_records_uri(now):
    p = post("retry", attempts=2, next_try="x", last_error="e")
    repost.mark_posted(p, "at://did:plc:bot/app.bsky.feed.post/q", now)
    assert (p.repost_status, p.quoted_uri, p.next_try, p.last_error) == (
        "posted", "at://did:plc:bot/app.bsky.feed.post/q", "", "")


@pytest.mark.parametrize("status", ["posted", "skipped", "failed"])
def test_terminal_states_are_final(status, now):
    p = post(status)
    with pytest.raises(repost.TransitionError):
        repost.mark_posted(p, "x", now)
    with pytest.raises(repost.TransitionError):
        repost.mark_transient(p, "x", now)
    assert not repost.is_due(p, now)


def test_na_cannot_be_posted_directly(now):
    with pytest.raises(repost.TransitionError):
        repost.mark_posted(post("n/a"), "x", now)


def test_stale_retry_also_becomes_skipped(now):
    """A post in retry (e.g. through a login outage) expires like a pending one."""
    p = post("retry", first_embed_seen=iso(now - timedelta(hours=48)), next_try=iso(now - timedelta(hours=40)))
    assert repost.expire_if_stale(p, now, 24)
    assert p.repost_status == "skipped" and not repost.is_due(p, now)
    fresh = post("retry", first_embed_seen=iso(now - timedelta(hours=2)))
    assert not repost.expire_if_stale(fresh, now, 24) and fresh.repost_status == "retry"
