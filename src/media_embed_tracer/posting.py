"""Quote-post eligible Bluesky embeds and record the outcome after every action.

Limits (per account = per instance, since each has its own posts store):
  * at most BLUESKY_DAILY_CAP quotes in any rolling 24 h (0 = unlimited, the default as in v1)
  * at most BLUESKY_AUTHOR_DAILY_CAP quotes of the same author in any rolling 24 h (0 = unlimited)
  * never quote DIDs on the opt-out list (status `skipped`)
  * at most MAX_POSTS_PER_RUN quote attempts per run (default 10, as v1)
Posts held back by a cap stay `pending` and expire after REPOST_MAX_AGE_HOURS.

Idempotency: before quoting, the bot's own latest posts are read and any subject it already
quoted is marked `posted` instead. This also repairs a status that was lost because the
store write failed after a successful quote.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from . import repost
from .bsky import BlueskyClient, BlueskyError, PermanentError, TransientError
from .models import Post
from .platforms import bluesky
from .quote import build_quote_text, build_record
from .redact import private, private_error
from .store.base import Store
from .util import iso, parse_iso

log = logging.getLogger(__name__)


@dataclass
class PostingStats:
    posted: int = 0
    already_quoted: int = 0
    retry: int = 0
    failed: int = 0
    skipped: int = 0
    held_by_cap: int = 0
    error: str = ""
    changed: list[Post] = field(default_factory=list, repr=False)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("changed")
        return d


@dataclass
class Limits:
    max_age_hours: float = 24
    max_posts: int = 10
    max_attempts: int = 5
    daily_cap: int = 0          # 0 = unlimited
    author_daily_cap: int = 0   # 0 = unlimited
    opt_out: frozenset[str] = frozenset()


def quote_check_window(limits: Limits) -> int:
    """How many of the bot's latest posts to scan for earlier quotes: at least every quote
    that can fall inside the eligibility window (daily cap x days in the window, plus a day)."""
    if not limits.daily_cap:
        return 1000   # no cap: read as far back as the guard reasonably can
    days = math.ceil(max(limits.max_age_hours, 1) / 24) + 1
    return max(100, min(1000, limits.daily_cap * days))


def _recent_posted(posts: dict[str, Post], now: datetime) -> list[Post]:
    since = now - timedelta(hours=24)
    return [p for p in posts.values() if p.platform == bluesky.NAME and p.repost_status == repost.POSTED
            and (parse_iso(p.updated_at) or since) > since]


def post_pending(store: Store, posts: dict[str, Post], client: BlueskyClient, now: datetime, limits: Limits, *,
                 template: str, langs: list[str], delay: float = 2.0, sleep=time.sleep) -> PostingStats:
    """Raises StoreError if a state update cannot be saved (posting stops immediately).
    Sets stats.error when a required Bluesky read fails (the pipeline exits non-zero)."""
    stats = PostingStats()

    def save(p: Post) -> None:
        store.update_post(p)
        stats.changed.append(p)

    for p in posts.values():
        if p.platform == bluesky.NAME and repost.expire_if_stale(p, now, limits.max_age_hours):
            save(p)
            stats.skipped += 1

    due = sorted((p for p in posts.values() if p.platform == bluesky.NAME and repost.is_due(p, now)),
                 key=lambda p: (p.first_embed_seen or p.first_seen, p.key))
    if not due:
        return stats

    recent = _recent_posted(posts, now)
    total_24h = len(recent)
    per_author = Counter(bluesky.actor_of(p.canonical_id) for p in recent)
    try:
        quoted = client.recent_quotes(limit=quote_check_window(limits))
    except BlueskyError as exc:
        # Without the idempotency check we might double-post: do nothing, fail the run.
        stats.error = f"could not read own recent posts: {exc}"
        log.error("%s", private_error(stats.error))
        return stats

    attempts = 0
    for p in due:
        if attempts >= limits.max_posts or (limits.daily_cap and total_24h >= limits.daily_cap):
            stats.held_by_cap += 1
            continue
        subject = p.canonical_id
        actor = bluesky.actor_of(subject)
        if actor in limits.opt_out:
            repost.mark_skipped(p, "author opted out", now)
            stats.skipped += 1
            save(p)
            continue
        if actor == client.did:
            repost.mark_skipped(p, "own post", now)
            stats.skipped += 1
            save(p)
            continue
        if subject in quoted:
            repost.mark_posted(p, quoted[subject], now)
            stats.already_quoted += 1
            save(p)
            continue
        if limits.author_daily_cap and per_author[actor] >= limits.author_daily_cap:
            stats.held_by_cap += 1
            continue
        if attempts:
            sleep(delay)
        attempts += 1
        # Only the work before createRecord sits under the broad except: once create_post
        # returned, the quote exists and the post must end up `posted`, whatever follows.
        uri, sending = "", False
        try:
            view = client.get_posts([subject]).get(subject)
            if view is None:
                repost.mark_failed(p, "post not found (deleted, blocked or not indexed)", now)
                stats.failed += 1
            elif (view.get("viewer") or {}).get("embeddingDisabled"):
                repost.mark_skipped(p, "author has disabled quote posts", now)
                stats.skipped += 1
            else:
                text = build_quote_text(template, p.via_outlet, p.via_title, p.via_article_url)
                record = build_record(text, view["uri"], view["cid"], langs, iso(now))
                sending = True
                uri = client.create_post(record)
        except TransientError as exc:
            repost.mark_transient(p, str(exc), now, max_attempts=limits.max_attempts)
            if p.repost_status == repost.FAILED:
                stats.failed += 1
            else:
                stats.retry += 1
            log.warning("transient error quoting %s: %s", private(subject), exc)
        except (PermanentError, KeyError, ValueError) as exc:
            repost.mark_failed(p, f"{type(exc).__name__}: {exc}", now)
            stats.failed += 1
            log.warning("permanent error quoting %s: %s", private(subject), exc)
        except Exception as exc:
            if sending:
                # Unknown whether the quote was created: leave the status as it is (the
                # own-posts check of the next run marks it posted if it exists), fail the
                # run and stop posting.
                stats.error = f"unexpected error while creating a quote: {type(exc).__name__}"
                log.error("%s", stats.error)
                break
            # One bad post must not stop the run (class name only).
            repost.mark_failed(p, f"unexpected error: {type(exc).__name__}", now)
            stats.failed += 1
            log.warning("unexpected error quoting %s: %s", private(subject), type(exc).__name__)
        if uri:
            stats.posted += 1
            quoted[subject] = uri
            total_24h += 1
            per_author[actor] += 1
            try:
                repost.mark_posted(p, uri, now)
                log.info("quoted %s -> %s", private(subject), private(uri))
            except Exception as exc:
                log.warning("error after quoting %s (kept as posted): %s", private(subject), type(exc).__name__)
                p.repost_status, p.quoted_uri, p.next_try, p.updated_at = repost.POSTED, uri, "", iso(now)
        save(p)
    return stats
