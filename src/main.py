"""Main entry point for the Media Embed Tracer."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Optional, List, Dict

from .rss_scraper import fetch_multiple_feeds, RSSEntry
from .html_fetcher import fetch_html
from .sheets_manager import SheetsManager
from .bluesky_poster import BlueskyPoster
from .platforms import (
    BlueskyPlatform,
    TwitterPlatform,
    TikTokPlatform,
    InstagramPlatform,
    FacebookPlatform,
    EmbedPost,
)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """
    Load configuration from environment variables.

    Expected environment variables:
    - FEEDS_JSON: JSON string containing feed configuration
    - SPREADSHEET_URL: Google Sheets URL
    - BLUESKY_POSTING_ENABLED: "true" to enable Bluesky posting
    - BLUESKY_ACCOUNT: Account name for Bluesky credentials
    - MAX_ARTICLES: Maximum articles to process per run (default 50)
    - MAX_AGE_HOURS: Maximum age of articles in hours (default 168 = 7 days)
    - FEED_NAMES_JSON: Optional JSON mapping domains to friendly names

    Returns:
        Configuration dictionary
    """
    config = {}

    # Required: Feeds configuration
    feeds_json = os.environ.get('FEEDS_JSON', '[]')
    try:
        config['feeds'] = json.loads(feeds_json)
    except json.JSONDecodeError as e:
        logger.error(f"Invalid FEEDS_JSON: {e}")
        config['feeds'] = []

    # Required: Spreadsheet URL
    config['spreadsheet_url'] = os.environ.get('SPREADSHEET_URL', '')

    # Optional: Bluesky posting
    config['bluesky_posting_enabled'] = os.environ.get(
        'BLUESKY_POSTING_ENABLED', 'false'
    ).lower() == 'true'
    config['bluesky_account'] = os.environ.get('BLUESKY_ACCOUNT', 'default')

    # Optional: Limits
    config['max_articles'] = int(os.environ.get('MAX_ARTICLES', '50'))
    config['max_age_hours'] = float(os.environ.get('MAX_AGE_HOURS', '168'))

    # Optional: Feed names for Bluesky posts
    feed_names_json = os.environ.get('FEED_NAMES_JSON', '{}')
    try:
        config['feed_names'] = json.loads(feed_names_json)
    except json.JSONDecodeError:
        config['feed_names'] = {}

    return config


def process_article(
    entry: RSSEntry,
    platforms: list,
) -> list[EmbedPost]:
    """
    Process a single article and detect embeds from all platforms.

    Args:
        entry: The RSS entry to process
        platforms: List of platform detector instances

    Returns:
        List of detected EmbedPost objects
    """
    embeds = []

    # Fetch the article HTML
    html = fetch_html(entry.url)
    if not html:
        logger.warning(f"Failed to fetch HTML for: {entry.url}")
        return embeds

    # Process with each platform
    for platform in platforms:
        try:
            platform_embeds = platform.process_article(
                html=html,
                article_url=entry.url,
                article_title=entry.title,
                article_published=entry.published,
                article_summary=entry.summary,
            )
            embeds.extend(platform_embeds)
        except Exception as e:
            logger.error(f"Error processing {platform.name} for {entry.url}: {e}")

    return embeds


def post_pending_to_bluesky(
    sheets: SheetsManager,
    poster: BlueskyPoster,
    worksheet_name: str = "All Embeds",
    max_posts: int = 10,
    delay_seconds: float = 2.0,
) -> int:
    """
    Post pending Bluesky embeds from the sheet.

    Args:
        sheets: SheetsManager instance
        poster: BlueskyPoster instance
        worksheet_name: Name of the worksheet
        max_posts: Maximum posts per run
        delay_seconds: Delay between posts

    Returns:
        Number of posts made
    """
    pending = sheets.get_pending_bluesky_posts(worksheet_name)

    if not pending:
        logger.info("No pending Bluesky posts")
        return 0

    logger.info(f"Found {len(pending)} pending Bluesky posts")

    posted = 0
    for post_info in pending[:max_posts]:
        try:
            success = poster.post_quote(
                post_url=post_info['post_url'],
                article_url=post_info['article_url'],
                article_title=post_info['article_title'],
                article_domain=post_info['domain'],
            )

            if success:
                sheets.update_repost_status(
                    worksheet_name,
                    post_info['row_number'],
                    "posted"
                )
                posted += 1
            else:
                sheets.update_repost_status(
                    worksheet_name,
                    post_info['row_number'],
                    "failed"
                )

            # Rate limiting
            time.sleep(delay_seconds)

        except Exception as e:
            logger.error(f"Error posting {post_info['post_url']}: {e}")
            sheets.update_repost_status(
                worksheet_name,
                post_info['row_number'],
                "failed"
            )

    logger.info(f"Posted {posted} Bluesky quotes")
    return posted


def run():
    """Main entry point for the scraper."""
    logger.info("Starting Media Embed Tracer")

    # Load configuration
    config = load_config()

    if not config['feeds']:
        logger.error("No feeds configured. Set FEEDS_JSON environment variable.")
        sys.exit(1)

    if not config['spreadsheet_url']:
        logger.error("No spreadsheet URL. Set SPREADSHEET_URL environment variable.")
        sys.exit(1)

    # Initialize platforms
    platforms = [
        BlueskyPlatform(),
        TwitterPlatform(),
        TikTokPlatform(),
        InstagramPlatform(),
        FacebookPlatform(),
    ]

    # Initialize sheets manager
    sheets = SheetsManager(config['spreadsheet_url'])

    # Initialize Bluesky poster if enabled
    poster: Optional[BlueskyPoster] = None
    if config['bluesky_posting_enabled']:
        poster = BlueskyPoster(account_name=config['bluesky_account'])
        poster.set_feed_names(config['feed_names'])

    # Fetch RSS entries
    logger.info(f"Fetching {len(config['feeds'])} RSS feeds...")
    entries = fetch_multiple_feeds(
        config['feeds'],
        max_age_hours=config['max_age_hours'],
    )

    if not entries:
        logger.info("No articles found in feeds")
        return

    # Limit number of articles to process
    entries = entries[:config['max_articles']]
    logger.info(f"Processing {len(entries)} articles...")

    # Process each article
    all_embeds: list[EmbedPost] = []
    for entry in entries:
        embeds = process_article(entry, platforms)
        all_embeds.extend(embeds)

        # Small delay to be nice to servers
        time.sleep(0.5)

    if not all_embeds:
        logger.info("No embeds found in articles")
    else:
        logger.info(f"Found {len(all_embeds)} total embeds")

        # Group by platform for logging
        by_platform = {}
        for embed in all_embeds:
            by_platform.setdefault(embed.platform, []).append(embed)

        for platform, embeds in by_platform.items():
            logger.info(f"  {platform}: {len(embeds)} embeds")

        # Write to sheets
        written, skipped = sheets.write_embeds_batch(all_embeds)
        logger.info(f"Written: {written}, Skipped (duplicates): {skipped}")

    # Post pending Bluesky embeds if enabled
    if poster:
        try:
            post_pending_to_bluesky(sheets, poster)
        except Exception as e:
            logger.error(f"Error posting to Bluesky: {e}")

    logger.info("Media Embed Tracer completed")


if __name__ == "__main__":
    run()
