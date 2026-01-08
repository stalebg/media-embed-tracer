"""TikTok platform detection and URL handling."""

from __future__ import annotations

import re
import logging
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .base import BasePlatform


class TikTokPlatform(BasePlatform):
    """Detect and handle TikTok embeds."""

    # Regex pattern for TikTok URLs
    TIKTOK_URL_PATTERN = re.compile(
        r'https?://(?:www\.)?tiktok\.com/@([^/]+)/video/(\d+)',
        re.IGNORECASE
    )

    # Pattern for short TikTok URLs
    TIKTOK_SHORT_PATTERN = re.compile(
        r'https?://(?:vm\.tiktok\.com|vt\.tiktok\.com)/([a-zA-Z0-9]+)',
        re.IGNORECASE
    )

    @property
    def name(self) -> str:
        return "tiktok"

    def detect_embeds(self, html: str, article_url: str) -> list[str]:
        """
        Detect TikTok embeds in HTML.

        Looks for:
        - Direct tiktok.com links
        - Short vm.tiktok.com links
        - TikTok embed blockquotes
        """
        urls = set()

        # Parse HTML
        soup = BeautifulSoup(html, 'html.parser')

        # Find direct links to TikTok
        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'tiktok.com' in href:
                # Full URL
                if self.TIKTOK_URL_PATTERN.search(href):
                    urls.add(href)
                # Short URL
                elif self.TIKTOK_SHORT_PATTERN.search(href):
                    expanded = self._expand_short_url(href)
                    if expanded:
                        urls.add(expanded)

        # Find TikTok embed blockquotes
        for blockquote in soup.find_all('blockquote', class_='tiktok-embed'):
            cite = blockquote.get('cite')
            if cite and self.TIKTOK_URL_PATTERN.search(cite):
                urls.add(cite)

        # Search raw HTML for TikTok URLs
        for match in self.TIKTOK_URL_PATTERN.finditer(html):
            urls.add(match.group(0))

        # Also look for short URLs in raw HTML
        for match in self.TIKTOK_SHORT_PATTERN.finditer(html):
            expanded = self._expand_short_url(match.group(0))
            if expanded:
                urls.add(expanded)

        return list(urls)

    def extract_author(self, post_url: str) -> str:
        """Extract author handle from TikTok URL."""
        match = self.TIKTOK_URL_PATTERN.search(post_url)
        if match:
            return f"@{match.group(1)}"
        return "unknown"

    def normalize_url(self, url: str) -> str:
        """Normalize TikTok URL to canonical form."""
        url = url.strip()

        # If it's a short URL, try to expand it
        if self.TIKTOK_SHORT_PATTERN.search(url):
            expanded = self._expand_short_url(url)
            if expanded:
                url = expanded

        # Remove query parameters and fragments
        match = self.TIKTOK_URL_PATTERN.search(url)
        if match:
            username = match.group(1)
            video_id = match.group(2)
            return f"https://www.tiktok.com/@{username}/video/{video_id}"

        return url

    def _expand_short_url(self, short_url: str) -> str | None:
        """
        Expand a short TikTok URL to the full format.

        Args:
            short_url: A vm.tiktok.com or vt.tiktok.com URL

        Returns:
            The expanded URL, or None if expansion fails
        """
        logger = logging.getLogger(__name__)

        try:
            # Follow redirects to get the full URL
            response = requests.head(
                short_url,
                allow_redirects=True,
                timeout=10,
                headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; MediaEmbedTracer/1.0)'
                }
            )

            final_url = response.url
            if self.TIKTOK_URL_PATTERN.search(final_url):
                logger.debug(f"Expanded {short_url} to {final_url}")
                return final_url

        except Exception as e:
            logger.warning(f"Failed to expand TikTok short URL {short_url}: {e}")

        return None
