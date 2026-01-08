"""Twitter/X platform detection and URL handling."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .base import BasePlatform


class TwitterPlatform(BasePlatform):
    """Detect and handle Twitter/X embeds."""

    # Regex pattern for Twitter/X URLs
    TWITTER_URL_PATTERN = re.compile(
        r'https?://(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)',
        re.IGNORECASE
    )

    @property
    def name(self) -> str:
        return "twitter"

    def detect_embeds(self, html: str, article_url: str) -> list[str]:
        """
        Detect Twitter/X embeds in HTML.

        Looks for:
        - Direct twitter.com/x.com links
        - Twitter embed blockquotes
        - Tweet cards and widgets
        """
        urls = set()

        # Parse HTML
        soup = BeautifulSoup(html, 'html.parser')

        # Find direct links to twitter.com or x.com
        for link in soup.find_all('a', href=True):
            href = link['href']
            match = self.TWITTER_URL_PATTERN.search(href)
            if match:
                urls.add(href)

        # Find Twitter embed blockquotes (class="twitter-tweet")
        for blockquote in soup.find_all('blockquote', class_='twitter-tweet'):
            # The blockquote usually contains a link to the tweet
            for link in blockquote.find_all('a', href=True):
                match = self.TWITTER_URL_PATTERN.search(link['href'])
                if match:
                    urls.add(link['href'])

        # Also search raw HTML for Twitter URLs that might not be in anchor tags
        for match in self.TWITTER_URL_PATTERN.finditer(html):
            urls.add(match.group(0))

        return list(urls)

    def extract_author(self, post_url: str) -> str:
        """Extract author handle from Twitter URL."""
        match = self.TWITTER_URL_PATTERN.search(post_url)
        if match:
            return f"@{match.group(1)}"
        return "unknown"

    def normalize_url(self, url: str) -> str:
        """Normalize Twitter URL to x.com format."""
        url = url.strip()

        # Remove query parameters and fragments
        parsed = urlparse(url)
        clean_path = parsed.path

        # Extract username and status ID
        match = self.TWITTER_URL_PATTERN.search(url)
        if match:
            username = match.group(1)
            status_id = match.group(2)
            # Normalize to x.com format
            return f"https://x.com/{username}/status/{status_id}"

        return url
