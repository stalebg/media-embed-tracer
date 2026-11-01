"""Data model: occurrences (article x post), canonical posts, seen articles, feed health.

All timestamps are ISO 8601 strings in UTC (``2026-01-31T12:00:00Z``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

# Bump when detection or canonicalisation changes, so dataset rows can be compared fairly.
DETECTOR_VERSION = "2.0.0"

# How a post was found in an article.
#   blockquote   official embed blockquote (twitter-tweet, bluesky-embed, ...)
#   iframe       iframe / amp-iframe pointing at a platform embed URL
#   widget       other official embed placeholders hydrated by a platform script:
#                div.fb-post, script[data-telegram-post], lite-youtube, amp-twitter, data-* URLs
#   script-data  URL found inside inline <script> page data (JSON, possibly escaped), page-wide.
#                Frameworks (Next.js, Nuxt) serialise plain links there too, so this is NOT
#                evidence of an embed: recorded, never quote-posted.
#   link         plain <a href> link in the article body; quoted too unless QUOTE_LINKS=false (as v1)
#   unknown      only used for rows migrated from the v1 sheet
EMBED_TYPES = ("blockquote", "iframe", "widget", "script-data", "link", "unknown")
# Markup found inside the article body that renders as an embed. These always trigger quote posts
# (links only when QUOTE_LINKS is on, see repost.is_quotable).
EMBED_MARKUP_TYPES = frozenset({"blockquote", "iframe", "widget"})
# Suffix on the embed_type of page furniture (see pipeline.TEMPLATE_MIN_ARTICLES).
TEMPLATE_SUFFIX = "+template"
EMBED_PRIORITY = {"blockquote": 5, "iframe": 5, "widget": 4, "script-data": 3, "link": 1, "unknown": 0}


@dataclass(frozen=True)
class PostRef:
    """A post identified from a URL. ``unresolved`` is set when the canonical id
    still needs a network lookup (Bluesky handle -> DID, TikTok short link)."""

    platform: str
    canonical_id: str
    post_url: str
    author: str = ""
    unresolved: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.canonical_id}"


@dataclass(frozen=True)
class Hit:
    ref: PostRef
    embed_type: str

    @property
    def key(self) -> str:
        return self.ref.key


class Record:
    """Mixin: flat string/int serialisation used by every store backend."""

    @classmethod
    def field_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]  # type: ignore[arg-type]

    def to_dict(self) -> dict:
        return asdict(self)  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict):
        kwargs = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name not in data:
                continue
            value = data[f.name]
            if f.type in ("dict", dict, "dict[str, int]"):
                if isinstance(value, str):
                    try:
                        value = json.loads(value) if value else {}
                    except ValueError:
                        value = {}
                value = dict(value or {})
            elif f.type in ("int", int):
                try:
                    value = int(value or 0)
                except (TypeError, ValueError):
                    value = 0
            elif value is None:
                value = ""
            else:
                value = str(value)
            kwargs[f.name] = value
        return cls(**kwargs)

    def to_row(self) -> list:
        """Flat row for spreadsheets/CSV (dicts become compact JSON)."""
        out = []
        for n in self.field_names():
            v = getattr(self, n)
            out.append(json.dumps(v, sort_keys=True, separators=(",", ":")) if isinstance(v, dict) else v)
        return out

    @classmethod
    def from_row(cls, row: list):
        names = cls.field_names()
        return cls.from_dict({n: (row[i] if i < len(row) else "") for i, n in enumerate(names)})


@dataclass
class Occurrence(Record):
    """One post found in one article (the research unit)."""

    platform: str
    canonical_id: str
    post_url: str
    author: str
    embed_type: str
    article_url: str
    final_url: str
    outlet: str
    domain: str
    feed: str
    article_title: str
    article_published: str
    first_seen: str
    detector_version: str = DETECTOR_VERSION
    # Added after the first v2 layout: kept last, so older sheets only gain a column.
    article_summary: str = ""   # RSS summary/description, plain text, max 500 chars

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.canonical_id}"


@dataclass
class ArticleCheck(Record):
    """One fetch of one article: the denominator. Logged for every check, including
    failed fetches and articles with zero embeds."""

    url: str
    final_url: str
    outlet: str
    domain: str
    feed: str
    title: str
    published: str
    checked_at: str
    check_n: int
    fetch_ok: int                 # 1 / 0
    http_status: int = 0
    error: str = ""
    embeds: dict = field(default_factory=dict)        # platform -> number of posts (all types)
    embed_types: dict = field(default_factory=dict)   # embed_type -> number of posts
    detector_version: str = DETECTOR_VERSION


@dataclass
class RunLog(Record):
    """One row per run: totals plus per-feed counts of articles left over by caps."""

    run_at: str
    detector_version: str
    feeds_ok: int = 0
    feeds_failed: int = 0
    planned: int = 0
    articles_ok: int = 0
    articles_failed: int = 0
    occurrences: int = 0
    new_posts: int = 0
    queued_unresolved: int = 0
    dropped_unresolved: int = 0
    skipped_by_cap: dict = field(default_factory=dict)   # feed -> eligible articles not fetched
    posting: dict = field(default_factory=dict)
    exit_code: int = 0


@dataclass
class Post(Record):
    """One canonical post, with Bluesky repost state.

    repost_status: pending | posted | retry | skipped | failed | n/a (see repost.py).
    via_* holds the first article that embedded it for real (used for the quote text).
    """

    platform: str
    canonical_id: str
    post_url: str
    author: str
    first_seen: str
    first_embed_seen: str = ""   # first time seen as embed markup (quote eligibility clock)
    repost_status: str = "n/a"
    attempts: int = 0
    next_try: str = ""
    quoted_uri: str = ""
    last_error: str = ""
    updated_at: str = ""
    via_article_url: str = ""
    via_title: str = ""
    via_outlet: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.canonical_id}"


@dataclass
class SeenArticle(Record):
    """An article the scheduler has fetched (or tried to)."""

    url: str
    feed: str
    domain: str
    title: str
    published: str
    summary: str
    first_seen: str
    last_checked: str = ""
    checks: int = 0
    fetch_errors: int = 0
    found_keys: str = ""  # space-separated post keys already recorded for this article
    done: str = ""        # "1" once no further checks are planned

    def keys(self) -> set[str]:
        return set(self.found_keys.split()) if self.found_keys else set()

    def add_keys(self, keys) -> None:
        self.found_keys = " ".join(sorted(self.keys() | set(keys)))


@dataclass
class FeedHealth(Record):
    feed: str
    url: str
    last_attempt: str = ""
    last_ok: str = ""
    consecutive_failures: int = 0
    last_error: str = ""
    last_entries: int = 0
    last_skipped_by_cap: int = 0


@dataclass
class QueuedHit(Record):
    """A hit whose canonical id needs a lookup that failed (Bluesky handle, TikTok short
    link). Kept in state and retried on later runs; never written to the dataset unresolved."""

    url: str              # provisional post URL
    embed_type: str
    article_url: str
    final_url: str
    outlet: str
    domain: str
    feed: str
    article_title: str
    article_published: str
    first_seen: str
    attempts: int = 0
    article_summary: str = ""


@dataclass
class StoreState:
    posts: dict[str, Post] = field(default_factory=dict)
    seen: dict[str, SeenArticle] = field(default_factory=dict)
    health: dict[str, FeedHealth] = field(default_factory=dict)
    queue: list[QueuedHit] = field(default_factory=list)
