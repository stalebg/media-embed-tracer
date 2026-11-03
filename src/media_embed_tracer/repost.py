"""Bluesky repost state machine (pure functions, no I/O).

    n/a ──embed markup seen──> pending (posting on) | skipped (posting off)
    pending ──> posted | retry | failed | skipped
    retry   ──> posted | retry | failed | skipped
    posted, skipped, failed: terminal

* Only Bluesky posts found as embed markup (blockquote, iframe, widget) inside the
  article body, or as a plain link in it when QUOTE_LINKS is on (the default, as in v1),
  ever leave n/a. script-data (page-wide script JSON, which v1 did not scan) never does.
* Posting disabled => new real embeds become `skipped`, never `pending`, so turning
  posting on later does not flood the account with a backlog.
* A pending or retry post is only eligible within REPOST_MAX_AGE_HOURS of first_embed_seen;
  older ones become `skipped` ("too old").
* Transient errors -> `retry` with exponential backoff (next_try); after
  max_attempts the post becomes `failed`. Permanent errors -> `failed` at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import EMBED_MARKUP_TYPES, Post
from .util import iso, parse_iso

PENDING, POSTED, RETRY, SKIPPED, FAILED, NA = "pending", "posted", "retry", "skipped", "failed", "n/a"
STATUSES = (PENDING, POSTED, RETRY, SKIPPED, FAILED, NA)
TERMINAL = frozenset({POSTED, SKIPPED, FAILED})
_ALLOWED = {
    NA: {PENDING, SKIPPED},
    PENDING: {POSTED, RETRY, FAILED, SKIPPED},
    RETRY: {POSTED, RETRY, FAILED, SKIPPED},
    POSTED: set(), SKIPPED: set(), FAILED: set(),
}
QUOTABLE_PLATFORMS = frozenset({"bluesky"})
LINK_TYPES = frozenset({"link"})


class TransitionError(ValueError):
    pass


def _move(post: Post, new: str, now: datetime) -> None:
    if new not in _ALLOWED.get(post.repost_status, set()):
        raise TransitionError(f"{post.key}: {post.repost_status} -> {new} not allowed")
    post.repost_status = new
    post.updated_at = iso(now)


def is_quotable(platform: str, embed_type: str, quote_links: bool = True) -> bool:
    """Embed markup always; plain links in the article body too when `quote_links`."""
    if platform not in QUOTABLE_PLATFORMS:
        return False
    return embed_type in EMBED_MARKUP_TYPES or (quote_links and embed_type in LINK_TYPES)


def on_real_embed(post: Post, now: datetime, posting_enabled: bool,
                  article_url: str, title: str, outlet: str) -> bool:
    """Called when a quotable occurrence (see is_quotable) is recorded. Returns True if the
    post changed. `first_embed_seen` starts the eligibility clock."""
    if post.repost_status != NA:
        return False
    post.first_embed_seen = iso(now)
    post.via_article_url, post.via_title, post.via_outlet = article_url, title, outlet
    if posting_enabled:
        _move(post, PENDING, now)
    else:
        _move(post, SKIPPED, now)
        post.last_error = "posting disabled"
    return True


def expire_if_stale(post: Post, now: datetime, max_age_hours: float) -> bool:
    if post.repost_status not in (PENDING, RETRY):
        return False
    seen = parse_iso(post.first_embed_seen) or parse_iso(post.first_seen)
    if seen is None or now - seen > timedelta(hours=max_age_hours):
        _move(post, SKIPPED, now)
        post.last_error = f"too old (> {max_age_hours:g}h when posting was attempted)"
        return True
    return False


def is_due(post: Post, now: datetime) -> bool:
    if post.repost_status == PENDING:
        return True
    if post.repost_status == RETRY:
        nt = parse_iso(post.next_try)
        return nt is None or nt <= now
    return False


def mark_posted(post: Post, quoted_uri: str, now: datetime) -> None:
    _move(post, POSTED, now)
    post.quoted_uri = quoted_uri
    post.next_try = ""
    post.last_error = ""


def mark_skipped(post: Post, reason: str, now: datetime) -> None:
    _move(post, SKIPPED, now)
    post.last_error = reason[:300]


def mark_failed(post: Post, reason: str, now: datetime) -> None:
    post.attempts += 1
    _move(post, FAILED, now)
    post.next_try = ""
    post.last_error = reason[:300]


def mark_transient(post: Post, reason: str, now: datetime, max_attempts: int = 5,
                   base_minutes: float = 30, cap_hours: float = 12) -> None:
    post.attempts += 1
    post.last_error = reason[:300]
    if post.attempts >= max_attempts:
        _move(post, FAILED, now)
        post.next_try = ""
        post.last_error = f"gave up after {post.attempts} attempts: {reason}"[:300]
        return
    delay = min(timedelta(minutes=base_minutes * 2 ** (post.attempts - 1)), timedelta(hours=cap_hours))
    _move(post, RETRY, now)
    post.next_try = iso(now + delay)
