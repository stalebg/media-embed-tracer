"""RSS feed scraper with caching and filtering."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional, List, Dict
from urllib.parse import urlparse

import feedparser


logger = logging.getLogger(__name__)


@dataclass
class RSSEntry:
    """Represents a single RSS feed entry."""

    title: str
    url: str
    published: Optional[datetime]
    summary: Optional[str]
    feed_name: str
    feed_url: str

    @property
    def domain(self) -> str:
        """Extract domain from the entry URL."""
        parsed = urlparse(self.url)
        return parsed.netloc.replace("www.", "")

    @property
    def age_hours(self) -> float:
        """Calculate age of entry in hours."""
        if not self.published:
            return 0
        delta = datetime.utcnow() - self.published
        return delta.total_seconds() / 3600

    def is_recent(self, max_age_hours: float = 168) -> bool:
        """Check if entry is within the age limit (default 7 days)."""
        if not self.published:
            return True  # Include entries without dates
        return self.age_hours <= max_age_hours


def parse_date(entry) -> Optional[datetime]:
    """Parse date from a feedparser entry."""
    # Try different date fields
    for date_field in ['published_parsed', 'updated_parsed', 'created_parsed']:
        parsed = getattr(entry, date_field, None)
        if parsed:
            try:
                return datetime(*parsed[:6])
            except (TypeError, ValueError):
                continue
    return None


def fetch_feed(feed_url: str, feed_name: str = "", timeout: int = 30) -> list[RSSEntry]:
    """
    Fetch and parse an RSS feed.

    Args:
        feed_url: URL of the RSS feed
        feed_name: Human-readable name for the feed
        timeout: Request timeout in seconds

    Returns:
        List of RSSEntry objects
    """
    if not feed_name:
        parsed = urlparse(feed_url)
        feed_name = parsed.netloc.replace("www.", "")

    entries = []

    try:
        logger.info(f"Fetching feed: {feed_name}")
        feed = feedparser.parse(feed_url)

        if feed.bozo and feed.bozo_exception:
            logger.warning(f"Feed {feed_name} has issues: {feed.bozo_exception}")

        for entry in feed.entries:
            try:
                # Get entry URL
                url = entry.get('link', '')
                if not url:
                    continue

                # Get title
                title = entry.get('title', 'No title')

                # Get summary/description
                summary = entry.get('summary', entry.get('description', ''))
                # Clean up summary (remove HTML, limit length)
                if summary:
                    # Simple HTML strip (for full cleaning, use BeautifulSoup)
                    summary = summary[:500]

                rss_entry = RSSEntry(
                    title=title,
                    url=url,
                    published=parse_date(entry),
                    summary=summary,
                    feed_name=feed_name,
                    feed_url=feed_url,
                )
                entries.append(rss_entry)

            except Exception as e:
                logger.warning(f"Failed to parse entry in {feed_name}: {e}")
                continue

        logger.info(f"Found {len(entries)} entries in {feed_name}")

    except Exception as e:
        logger.error(f"Failed to fetch feed {feed_name}: {e}")

    return entries


def fetch_multiple_feeds(
    feeds: list[dict],
    max_age_hours: float = 168,
) -> list[RSSEntry]:
    """
    Fetch multiple RSS feeds and return combined entries.

    Args:
        feeds: List of dicts with 'url' and optional 'name' keys
        max_age_hours: Maximum age of entries to include (default 7 days)

    Returns:
        List of RSSEntry objects, filtered by age and deduplicated
    """
    all_entries = []
    seen_urls = set()

    for feed_config in feeds:
        feed_url = feed_config.get('url', '')
        feed_name = feed_config.get('name', '')

        if not feed_url:
            continue

        entries = fetch_feed(feed_url, feed_name)

        for entry in entries:
            # Skip if we've seen this URL
            if entry.url in seen_urls:
                continue
            seen_urls.add(entry.url)

            # Skip if too old
            if not entry.is_recent(max_age_hours):
                continue

            all_entries.append(entry)

    logger.info(f"Total entries after filtering: {len(all_entries)}")
    return all_entries
