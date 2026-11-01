"""Import the v1 Google Sheet layout ("All Embeds" tab, 11 columns) into a v2 store.

v1 columns: Date, Time, Platform, Domain, Author Handle, Article URL, Post URL,
Article Title, Article Summary, Published Date, Repost Status  (Date/Time were UTC).

* Post URLs are re-matched to canonical ids, so x.com/twitter.com and other
  variants merge. Unmatchable rows are counted and skipped.
* embed_type is "unknown" (v1 did not record it, and counted plain links too);
  detector_version is "v1". No article (denominator) rows exist for v1 data.
* Bluesky handle-form URLs are only imported if --resolve can turn them into DIDs.
* Repost status: posted -> posted, failed -> failed, pending (and retry/skipped, which v2
  writes too) -> skipped (so enabling posting never floods the backlog), anything else -> n/a.
* Times: v1 wrote "0:27:38" and "2026-10-29 8:00" (UTC, no leading zero); rows written by
  earlier v2 versions have "00:27:38" and an ISO published date. Both are accepted.
* Every article URL is marked as seen and done, so it is not fetched again.
The v1 tab is only read, never modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import Hit, Occurrence, Post, SeenArticle
from .platforms import match_url
from .resolve import Resolver
from .util import SUMMARY_LIMIT, canonical_article_url, domain_of, iso, parse_iso, plain_text

V1_HEADERS = ["Date", "Time", "Platform", "Domain", "Author Handle", "Article URL", "Post URL",
              "Article Title", "Article Summary", "Published Date", "Repost Status"]
_STATUS = {"posted": "posted", "failed": "failed", "pending": "skipped", "retry": "skipped", "skipped": "skipped"}


@dataclass
class MigrationResult:
    occurrences: list[Occurrence]
    posts: list[Post]
    seen: list[SeenArticle]
    skipped_rows: int


def _v1_time(date: str, time_: str) -> str:
    try:
        return iso(datetime.strptime(f"{date.strip()} {time_.strip() or '00:00:00'}", "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=UTC))
    except ValueError:
        return ""


def _v1_published(value: str) -> str:
    try:
        return iso(datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=UTC))
    except ValueError:
        return iso(parse_iso(value.strip())) if "T" in value else ""   # earlier v2 rows: ISO


def convert_v1(rows: list[list[str]], resolver: Resolver | None = None, existing_posts=None) -> MigrationResult:
    if not rows or [c.strip() for c in rows[0][:11]] != V1_HEADERS:
        raise ValueError("first row does not match the v1 'All Embeds' headers")
    posts: dict[str, Post] = dict(existing_posts or {})
    new_posts: dict[str, Post] = {}
    seen: dict[str, SeenArticle] = {}
    occ_keys: set[tuple[str, str]] = set()
    occurrences: list[Occurrence] = []
    skipped = 0
    for raw in rows[1:]:
        row = (list(raw) + [""] * 11)[:11]
        date, time_, _platform, domain, author, article_url, post_url, title, summary, published, status = row
        ref = match_url(post_url)
        if ref is None or not article_url:
            skipped += 1
            continue
        if resolver is not None and ref.unresolved:
            ref = resolver.canonicalize(Hit(ref, "unknown")).ref
        if ref.unresolved:  # never persist handle-form / short-link keys
            skipped += 1
            continue
        first_seen = _v1_time(date, time_)
        article = canonical_article_url(article_url)
        if (article, ref.key) in occ_keys:
            continue
        occ_keys.add((article, ref.key))
        post = posts.get(ref.key)
        if post is None:
            post = Post(platform=ref.platform, canonical_id=ref.canonical_id, post_url=ref.post_url,
                        author=ref.author or (plain_text(author) if author not in ("(see post)", "unknown") else ""),
                        first_seen=first_seen, updated_at=first_seen)
            post.repost_status = _STATUS.get(status.strip().lower(), "n/a") if ref.platform == "bluesky" else "n/a"
            if post.repost_status == "skipped":
                post.last_error = "migrated from v1 pending"
            posts[ref.key] = post
            new_posts[ref.key] = post
        occurrences.append(Occurrence(
            platform=ref.platform, canonical_id=ref.canonical_id, post_url=post.post_url, author=post.author,
            embed_type="unknown", article_url=article, final_url=article, outlet=domain or domain_of(article),
            domain=domain_of(article), feed="", article_title=plain_text(title, 500),
            article_published=_v1_published(published), first_seen=first_seen, detector_version="v1",
            article_summary=plain_text(summary, SUMMARY_LIMIT)))
        s = seen.get(article)
        if s is None:
            s = SeenArticle(url=article, feed="", domain=domain_of(article), title=plain_text(title, 500),
                            published=_v1_published(published), summary=plain_text(summary, SUMMARY_LIMIT),
                            first_seen=first_seen, last_checked=first_seen, checks=2, done="1")
            seen[article] = s
        s.add_keys([ref.key])
    return MigrationResult(occurrences, list(new_posts.values()), list(seen.values()), skipped)
