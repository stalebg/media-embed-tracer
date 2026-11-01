"""Feed configuration and fetching.

Feeds come from FEEDS_JSON (env, takes precedence) or a TOML/JSON file (FEEDS_FILE).
Feeds are downloaded with requests (timeout + size cap) and the bytes handed to
feedparser, so a hanging server cannot stall the run.
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import feedparser

from .http import Http
from .util import SUMMARY_LIMIT, canonical_article_url, domain_of, plain_text

log = logging.getLogger(__name__)


class FeedConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    outlet: str = ""
    per_feed_cap: int | None = None


@dataclass
class FeedSet:
    feeds: list[FeedConfig]
    outlet_names: dict[str, str] = field(default_factory=dict)  # domain -> display name

    def outlet_for(self, feed: FeedConfig | None, article_url: str) -> str:
        domain = domain_of(article_url)
        if domain in self.outlet_names:
            return self.outlet_names[domain]
        if feed and feed.outlet:
            return feed.outlet
        return domain


def _parse_feed_list(items, outlet_names) -> FeedSet:
    if not isinstance(items, list):
        raise FeedConfigError("feeds must be a list")
    feeds, names = [], set()
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("url"):
            raise FeedConfigError(f"feed #{i + 1} needs a 'url'")
        url = str(item["url"]).strip()
        name = str(item.get("name") or domain_of(url)).strip()
        if name in names:
            raise FeedConfigError(f"duplicate feed name: {name}")
        names.add(name)
        cap = item.get("per_feed_cap")
        feeds.append(FeedConfig(name, url, str(item.get("outlet", "")).strip(),
                                int(cap) if cap not in (None, "") else None))
    if not feeds:
        raise FeedConfigError("feed list is empty (replace the example file with your list or set FEEDS_JSON)")
    if not isinstance(outlet_names, dict):
        raise FeedConfigError("outlet_names must be a table/object")
    return FeedSet(feeds, {str(k).lower().removeprefix("www."): str(v) for k, v in outlet_names.items()})


def load_feeds(feeds_json: str = "", feeds_file: str = "", feed_names_json: str = "") -> FeedSet:
    """Load the feed list. FEED_NAMES_JSON (v1-compatible: {"domain": "Display name"}) is
    merged into outlet_names and wins over names from the feed list."""
    fs = _load_feed_set(feeds_json, feeds_file)
    if feed_names_json.strip():
        try:
            extra = json.loads(feed_names_json)
        except json.JSONDecodeError as exc:
            raise FeedConfigError(f"FEED_NAMES_JSON is not valid JSON: {exc}") from exc
        if not isinstance(extra, dict):
            raise FeedConfigError("FEED_NAMES_JSON must be an object of domain -> name")
        fs.outlet_names.update({str(k).lower().removeprefix("www."): str(v) for k, v in extra.items()})
    return fs


def _load_feed_set(feeds_json: str, feeds_file: str) -> FeedSet:
    if feeds_json.strip():
        try:
            data = json.loads(feeds_json)
        except json.JSONDecodeError as exc:
            raise FeedConfigError(f"FEEDS_JSON is not valid JSON: {exc}") from exc
        if isinstance(data, dict):
            return _parse_feed_list(data.get("feeds"), data.get("outlet_names", {}))
        return _parse_feed_list(data, {})
    if feeds_file:
        path = Path(feeds_file)
        if not path.is_file():
            raise FeedConfigError(f"feeds file not found: {feeds_file}")
        if path.suffix == ".toml":
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return _parse_feed_list(data, {})
        return _parse_feed_list(data.get("feeds", []), data.get("outlet_names", {}))
    raise FeedConfigError("no feeds configured: set FEEDS_FILE or FEEDS_JSON")


@dataclass
class Entry:
    url: str           # canonical article URL (seen-article key)
    title: str
    published: datetime | None
    summary: str
    feed: FeedConfig


def _entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=UTC)  # feedparser normalises to UTC
            except (TypeError, ValueError):
                continue
    return None


def parse_feed_bytes(content: bytes, feed: FeedConfig) -> list[Entry]:
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")
    entries = []
    for e in parsed.entries:
        link = e.get("link") or ""
        if not link.startswith(("http://", "https://")):
            continue
        entries.append(Entry(
            url=canonical_article_url(link),
            title=plain_text(e.get("title"), 500),
            published=_entry_date(e),
            summary=plain_text(e.get("summary") or e.get("description"), SUMMARY_LIMIT),
            feed=feed,
        ))
    return entries


def fetch_feed(http: Http, feed: FeedConfig) -> tuple[list[Entry], str]:
    """Returns (entries, error). error is '' on success."""
    res = http.get(feed.url, accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5")
    if not res.ok:
        return [], res.error or f"HTTP {res.status}"
    try:
        return parse_feed_bytes(res.content, feed), ""
    except Exception as exc:  # feedparser can raise odd things on garbage
        return [], f"parse error: {exc}"[:300]
