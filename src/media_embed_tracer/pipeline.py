"""One scrape run.

feeds -> resolve queue retry -> schedule -> fetch + detect -> save (data + state)
-> optional quote-posting (state saved after every action) -> run log -> mirrors.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import repost
from .bsky import BlueskyClient, LoginError
from .config import Config
from .detect import detect
from .feeds import FeedConfig, FeedSet, fetch_feed
from .http import Http
from .models import (
    DETECTOR_VERSION,
    TEMPLATE_SUFFIX,
    ArticleCheck,
    FeedHealth,
    Hit,
    Occurrence,
    Post,
    QueuedHit,
    RunLog,
    SeenArticle,
)
from .platforms import match_url
from .posting import Limits, PostingStats, post_pending
from .redact import private, private_error
from .resolve import Resolver
from .scheduler import plan
from .store.base import Mirror, Store, StoreError
from .util import domain_of, iso, utcnow

log = logging.getLogger(__name__)

EXIT_OK, EXIT_CONFIG, EXIT_STORE, EXIT_LOGIN, EXIT_FETCH, EXIT_BLUESKY = 0, 1, 2, 3, 4, 5
MAX_FETCH_ERRORS = 3
# A post found in this many different articles of one outlet in one run is page furniture
# (a teaser, live box or footer widget the detector could not tell apart): recorded with
# embed_type "<type>+template" and never quoted. Only this run's articles count: earlier
# runs are not consulted, so a post genuinely re-embedded over days keeps one label in the
# dataset. The main-article detection (detect._content_roots) is the real protection.
TEMPLATE_MIN_ARTICLES = 3
MIN_ARTICLES_FOR_RATIO = 4


@dataclass
class RunStats:
    feeds_ok: int = 0
    feeds_failed: int = 0
    articles_ok: int = 0
    articles_failed: int = 0
    crashed: int = 0          # article checks that raised unexpectedly (retried next run)
    occurrences: int = 0
    new_posts: int = 0
    queued: int = 0
    dropped: int = 0
    by_platform: Counter = field(default_factory=Counter)
    by_type: Counter = field(default_factory=Counter)
    skipped_by_cap: dict = field(default_factory=dict)
    posting: PostingStats | None = None
    problems: list[str] = field(default_factory=list)


def _too_many_failures(ok: int, failed: int, threshold: float, minimum: int) -> bool:
    total = ok + failed
    return total >= minimum and failed / total > threshold


@dataclass
class _Context:
    """Article context shared by every hit on one page."""

    article_url: str
    final_url: str
    outlet: str
    domain: str
    feed: str
    title: str
    published: str
    summary: str = ""   # RSS summary (plain text); written to the dataset, never logged


class _Recorder:
    """Turns resolved hits into occurrences/posts; queues unresolved ones."""

    def __init__(self, cfg: Config, state, now: datetime, stats: RunStats) -> None:
        self.cfg, self.state, self.now, self.stats = cfg, state, now, stats
        self.occurrences: list[Occurrence] = []
        self.changed_posts: dict[str, Post] = {}
        self.queue: list[QueuedHit] = []
        self._queued_keys = set()
        # First quotable occurrence per post this run; quote state is set in finish().
        self._quotable: dict[str, tuple[Post, _Context]] = {}

    def record(self, hit: Hit, ctx: _Context, seen: SeenArticle | None, already: set[str]) -> bool:
        """Returns True if a new occurrence was written."""
        if hit.key in already:
            return False
        already.add(hit.key)
        ref, now = hit.ref, self.now
        post = self.changed_posts.get(hit.key) or self.state.posts.get(hit.key)
        if post is None:
            post = Post(platform=ref.platform, canonical_id=ref.canonical_id, post_url=ref.post_url,
                        author=ref.author, first_seen=iso(now), updated_at=iso(now))
            self.state.posts[hit.key] = post
            self.changed_posts[hit.key] = post
            self.stats.new_posts += 1
        self.occurrences.append(Occurrence(
            platform=ref.platform, canonical_id=ref.canonical_id, post_url=post.post_url, author=post.author,
            embed_type=hit.embed_type, article_url=ctx.article_url, final_url=ctx.final_url, outlet=ctx.outlet,
            domain=ctx.domain, feed=ctx.feed, article_title=ctx.title, article_published=ctx.published,
            first_seen=iso(now), article_summary=ctx.summary))
        self.stats.by_platform[ref.platform] += 1
        self.stats.by_type[hit.embed_type] += 1
        if repost.is_quotable(ref.platform, hit.embed_type, self.cfg.quote_links):
            self._quotable.setdefault(hit.key, (post, ctx))
        if seen is not None:
            seen.add_keys([hit.key])
        return True

    def finish(self) -> int:
        """Flag page furniture, then start the quote state of the remaining quotable posts.
        Returns the number of posts flagged as templates."""
        articles: dict[tuple[str, str], set[str]] = {}
        for o in self.occurrences:
            articles.setdefault((o.outlet, o.key), set()).add(o.article_url)
        template = {key for (_outlet, key), urls in articles.items() if len(urls) >= TEMPLATE_MIN_ARTICLES}
        for o in self.occurrences:
            if o.key in template and not o.embed_type.endswith(TEMPLATE_SUFFIX):
                o.embed_type += TEMPLATE_SUFFIX
        for key in template:
            post = self.changed_posts.get(key) or self.state.posts.get(key)
            if post is not None and post.repost_status in (repost.PENDING, repost.RETRY):
                repost.mark_skipped(post, "page furniture (in many articles of one outlet)", self.now)
                self.changed_posts[key] = post
        for key, (post, ctx) in self._quotable.items():
            if key in template:
                continue
            if repost.on_real_embed(post, self.now, self.cfg.posting_enabled, ctx.final_url, ctx.title, ctx.outlet):
                self.changed_posts[key] = post
        return len(template)

    def enqueue(self, hit: Hit, ctx: _Context, attempts: int = 0, first_seen: str = "") -> None:
        k = (ctx.article_url, hit.ref.post_url)
        if k in self._queued_keys:
            return
        self._queued_keys.add(k)
        self.queue.append(QueuedHit(
            url=hit.ref.post_url, embed_type=hit.embed_type, article_url=ctx.article_url,
            final_url=ctx.final_url, outlet=ctx.outlet, domain=ctx.domain, feed=ctx.feed,
            article_title=ctx.title, article_published=ctx.published, first_seen=first_seen or iso(self.now),
            attempts=attempts, article_summary=ctx.summary))


def run(cfg: Config, feedset: FeedSet, store: Store, http: Http, resolver: Resolver,
        client: BlueskyClient | None = None, mirrors: list[Mirror] | None = None,
        now: datetime | None = None, sleep=time.sleep, clock=time.monotonic) -> tuple[int, RunStats]:
    now = now or utcnow()
    stats = RunStats()
    exit_codes: list[int] = []
    deadline = clock() + cfg.max_run_minutes * 60

    try:
        state = store.load()
    except StoreError as exc:
        log.error("store load failed: %s", exc)
        return EXIT_STORE, stats

    # 1. Feeds -------------------------------------------------------------
    entries_by_feed = {}
    feeds_by_name: dict[str, FeedConfig] = {f.name: f for f in feedset.feeds}
    health: dict[str, FeedHealth] = {}
    for feed in feedset.feeds:
        h = state.health.get(feed.name) or FeedHealth(feed.name, feed.url)
        h.url, h.last_attempt = feed.url, iso(now)
        entries, error = fetch_feed(http, feed)
        if error:
            stats.feeds_failed += 1
            h.consecutive_failures += 1
            h.last_error = error
            level = logging.ERROR if h.consecutive_failures >= 3 else logging.WARNING
            log.log(level, "feed %s failed (%d in a row): %s", feed.name, h.consecutive_failures, private_error(error))
        else:
            stats.feeds_ok += 1
            h.consecutive_failures, h.last_error, h.last_ok = 0, "", iso(now)
            h.last_entries = len(entries)
            entries_by_feed[feed.name] = entries
        health[feed.name] = h

    # Summaries for rechecks: stores may keep seen articles without them (sheets mode).
    summaries = {e.url: e.summary for entries in entries_by_feed.values() for e in entries}
    rec = _Recorder(cfg, state, now, stats)
    changed_seen: dict[str, SeenArticle] = {}
    articles: list[ArticleCheck] = []

    # 2. Retry queued hits (handle / short-link lookups that failed earlier) --
    for q in state.queue:
        ref = match_url(q.url)
        ctx = _Context(q.article_url, q.final_url, q.outlet, q.domain, q.feed, q.article_title, q.article_published,
                       q.article_summary)
        if ref is None:
            continue
        hit = resolver.canonicalize(Hit(ref, q.embed_type))
        if not hit.ref.unresolved:
            seen = state.seen.get(q.article_url)
            if rec.record(hit, ctx, seen, seen.keys() if seen else set()) and seen is not None:
                changed_seen[seen.url] = seen
        elif q.attempts + 1 >= cfg.resolve_max_attempts:
            stats.dropped += 1
            log.warning("giving up on unresolvable %s (from %s)", private(q.url), private(q.article_url))
        else:
            rec.enqueue(hit, ctx, q.attempts + 1, q.first_seen)

    # 3. Schedule ----------------------------------------------------------
    p = plan(entries_by_feed, state.seen, now, max_articles=cfg.max_articles, per_feed_cap=cfg.per_feed_cap,
             recheck_after_hours=cfg.recheck_after_hours, max_age_hours=cfg.max_age_hours,
             max_fetch_errors=MAX_FETCH_ERRORS)
    skipped = Counter(p.skipped_by_cap)
    log.info("planned %d article fetches (%s); left over by caps: %d", len(p.tasks),
             dict(Counter(t.kind for t in p.tasks)), sum(skipped.values()))

    # 4. Fetch + detect ----------------------------------------------------
    for n, task in enumerate(p.tasks):
        if clock() > deadline:
            for t in p.tasks[n:]:
                skipped[(t.entry.feed.name if t.entry else (t.seen.feed if t.seen else ""))] += 1
            stats.problems.append(f"time budget reached; {len(p.tasks) - n} planned fetches deferred")
            log.warning(stats.problems[-1])
            break
        if n and cfg.article_delay:
            sleep(cfg.article_delay)
        if task.entry is not None:
            e = task.entry
            seen = task.seen or SeenArticle(
                url=e.url, feed=e.feed.name, domain=domain_of(e.url), title=e.title,
                published=iso(e.published), summary=e.summary, first_seen=iso(now))
        else:
            seen = task.seen
        assert seen is not None
        # One bad article must not stop the run. An unexpected exception while fetching or
        # parsing is a crash (our bug), not a fetch error: the class name is logged, nothing
        # about the article changes (no check row, no fetch_errors, seen state untouched), so
        # it is tried again next run. The run exits non-zero, so a repeating crash stays
        # visible. (No per-article crash cap: the seen state has no field for one.)
        hits: list[Hit] = []
        try:
            res = http.fetch_html(seen.url)
            if res.ok:
                hits = [resolver.canonicalize(h) for h in detect(res.text)]
        except Exception as exc:
            stats.crashed += 1
            log.error("check crashed %s: %s; retried next run", private(seen.url), type(exc).__name__)
            continue
        final_url = res.url or seen.url
        seen.last_checked = iso(now)
        changed_seen[seen.url] = seen
        ctx = _Context(seen.url, final_url, feedset.outlet_for(feeds_by_name.get(seen.feed), final_url),
                       domain_of(final_url), seen.feed, seen.title, seen.published,
                       seen.summary or summaries.get(seen.url, ""))
        check = ArticleCheck(url=seen.url, final_url=final_url, outlet=ctx.outlet, domain=ctx.domain,
                             feed=seen.feed, title=seen.title, published=seen.published, checked_at=iso(now),
                             check_n=seen.checks + 1, fetch_ok=1 if res.ok else 0, http_status=res.status,
                             error=res.error)
        articles.append(check)
        if not res.ok:
            stats.articles_failed += 1
            seen.fetch_errors += 1
            if task.kind == "recheck" or seen.fetch_errors >= MAX_FETCH_ERRORS:
                seen.done = "1"
            log.warning("fetch failed %s (status %s): %s", private(seen.url), res.status, private_error(res.error))
            continue
        stats.articles_ok += 1
        seen.checks += 1
        if seen.checks >= 2 or cfg.recheck_after_hours <= 0:
            seen.done = "1"

        check.embeds = dict(Counter(h.ref.platform for h in hits))
        check.embed_types = dict(Counter(h.embed_type for h in hits))
        already = seen.keys()
        new = 0
        for hit in hits:
            if hit.ref.unresolved:
                rec.enqueue(hit, ctx)
            elif rec.record(hit, ctx, seen, already):
                new += 1
        if new:
            log.info("%d new post(s) in %s", new, private(seen.url))

    templates = rec.finish()
    if templates:
        log.warning("%d post(s) found in %d+ articles of one outlet this run: recorded as page furniture "
                    "(+template), not quoted", templates, TEMPLATE_MIN_ARTICLES)
    stats.occurrences = len(rec.occurrences)
    stats.queued = len(rec.queue)
    stats.skipped_by_cap = {k: v for k, v in sorted(skipped.items()) if v}
    for name, h in health.items():
        h.last_skipped_by_cap = stats.skipped_by_cap.get(name, 0)

    # 5. Save data + state -------------------------------------------------
    try:
        store.save(occurrences=rec.occurrences, articles=articles, posts=rec.changed_posts.values(),
                   seen=changed_seen.values(), health=health.values(), queue=rec.queue)
    except StoreError as exc:
        log.error("store save failed: %s", exc)
        stats.problems.append(private_error(exc, "store: "))
        return EXIT_STORE, stats  # never post when state could not be saved

    # 6. Quote-post --------------------------------------------------------
    if cfg.posting_enabled and client is not None:
        try:
            client.login(cfg.bluesky_handle, cfg.bluesky_app_password)
        except LoginError as exc:
            stats.problems.append(private_error(exc, "Bluesky login: "))
            exit_codes.append(EXIT_LOGIN)
        else:
            limits = Limits(cfg.repost_max_age_hours, cfg.max_posts_per_run, cfg.repost_max_attempts,
                            cfg.daily_cap, cfg.author_daily_cap, cfg.opt_out_dids)
            try:
                stats.posting = post_pending(store, state.posts, client, now, limits, template=cfg.quote_template,
                                             langs=cfg.quote_langs, delay=cfg.post_delay, sleep=sleep)
            except StoreError as exc:
                stats.problems.append(private_error(exc, "store during posting: "))
                exit_codes.append(EXIT_STORE)
            if stats.posting and stats.posting.error:
                stats.problems.append(private_error(stats.posting.error))
                exit_codes.append(EXIT_BLUESKY)
            if stats.posting:
                for post in stats.posting.changed:
                    rec.changed_posts[post.key] = post

    # 7. Verdict, run log, mirrors -----------------------------------------
    if _too_many_failures(stats.feeds_ok, stats.feeds_failed, cfg.fail_threshold, 1):
        stats.problems.append(f"{stats.feeds_failed}/{stats.feeds_ok + stats.feeds_failed} feeds failed")
        exit_codes.append(EXIT_FETCH)
    if stats.crashed:
        stats.problems.append(f"{stats.crashed} article check(s) crashed (class names logged); retried next run")
        exit_codes.append(EXIT_FETCH)
    if _too_many_failures(stats.articles_ok, stats.articles_failed, cfg.fail_threshold, MIN_ARTICLES_FOR_RATIO):
        total = stats.articles_ok + stats.articles_failed
        stats.problems.append(f"{stats.articles_failed}/{total} article fetches failed")
        exit_codes.append(EXIT_FETCH)

    def verdict() -> int:
        for code in (EXIT_STORE, EXIT_LOGIN, EXIT_BLUESKY, EXIT_FETCH):
            if code in exit_codes:
                return code
        return EXIT_OK

    run_log = RunLog(run_at=iso(now), detector_version=DETECTOR_VERSION, feeds_ok=stats.feeds_ok,
                     feeds_failed=stats.feeds_failed, planned=len(p.tasks), articles_ok=stats.articles_ok,
                     articles_failed=stats.articles_failed, occurrences=stats.occurrences,
                     new_posts=stats.new_posts, queued_unresolved=stats.queued,
                     dropped_unresolved=stats.dropped, skipped_by_cap=stats.skipped_by_cap,
                     posting=stats.posting.summary() if stats.posting else {}, exit_code=verdict())
    try:
        store.append_run(run_log)
    except StoreError as exc:
        stats.problems.append(private_error(exc, "store: "))
        exit_codes.append(EXIT_STORE)

    for mirror in mirrors or []:
        try:
            mirror.write(occurrences=rec.occurrences, articles=articles, posts=list(rec.changed_posts.values()),
                         health=list(health.values()), run=run_log)
        except StoreError as exc:
            stats.problems.append(private_error(exc, f"mirror {mirror.name}: "))
            exit_codes.append(EXIT_STORE)

    log.info("run done: feeds ok=%d failed=%d, articles ok=%d failed=%d, occurrences=%d, new posts=%d, "
             "queued=%d, dropped=%d, %s %s", stats.feeds_ok, stats.feeds_failed, stats.articles_ok,
             stats.articles_failed, stats.occurrences, stats.new_posts, stats.queued, stats.dropped,
             dict(stats.by_platform), dict(stats.by_type))
    if stats.posting:
        log.info("posting: %s", stats.posting.summary())
    for problem in stats.problems:
        log.error("problem: %s", problem)
    return verdict(), stats
