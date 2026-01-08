"""Instagram platform detection and URL handling."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .base import BasePlatform


class InstagramPlatform(BasePlatform):
    """Detect and handle Instagram embeds."""

    # Regex patterns for Instagram URLs
    # Matches: /p/CODE/, /reel/CODE/, /reels/CODE/, /tv/CODE/
    INSTAGRAM_URL_PATTERN = re.compile(
        r'https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([a-zA-Z0-9_-]+)',
        re.IGNORECASE
    )

    # Pattern for Instagram profile URLs (to extract author)
    INSTAGRAM_PROFILE_PATTERN = re.compile(
        r'https?://(?:www\.)?instagram\.com/([a-zA-Z0-9_.]+)(?:/|$)',
        re.IGNORECASE
    )

    @property
    def name(self) -> str:
        return "instagram"

    def detect_embeds(self, html: str, article_url: str) -> list[str]:
        """
        Detect Instagram embeds in HTML.

        Looks for:
        - Direct instagram.com/p/ links (posts)
        - instagram.com/reel/ links (reels)
        - instagram.com/tv/ links (IGTV)
        - Instagram embed blockquotes
        """
        urls = set()

        # Parse HTML
        soup = BeautifulSoup(html, 'html.parser')

        # Find direct links to Instagram posts/reels
        for link in soup.find_all('a', href=True):
            href = link['href']
            match = self.INSTAGRAM_URL_PATTERN.search(href)
            if match:
                urls.add(href)

        # Find Instagram embed blockquotes (class="instagram-media")
        for blockquote in soup.find_all('blockquote', class_='instagram-media'):
            # Get the data-instgrm-permalink attribute
            permalink = blockquote.get('data-instgrm-permalink')
            if permalink and self.INSTAGRAM_URL_PATTERN.search(permalink):
                urls.add(permalink)

            # Also check for links inside the blockquote
            for link in blockquote.find_all('a', href=True):
                match = self.INSTAGRAM_URL_PATTERN.search(link['href'])
                if match:
                    urls.add(link['href'])

        # Search raw HTML for Instagram URLs
        for match in self.INSTAGRAM_URL_PATTERN.finditer(html):
            urls.add(match.group(0))

        return list(urls)

    def extract_author(self, post_url: str) -> str:
        """
        Extract author handle from Instagram URL.

        Note: Instagram post URLs don't contain the username directly.
        We return the post type and ID as a placeholder.
        Full author extraction would require API access.
        """
        match = self.INSTAGRAM_URL_PATTERN.search(post_url)
        if match:
            # We can't easily get the username from the URL alone
            # Return a placeholder indicating we found a post
            return "(see post)"
        return "unknown"

    def normalize_url(self, url: str) -> str:
        """Normalize Instagram URL to canonical form."""
        url = url.strip()

        # Remove query parameters and fragments
        match = self.INSTAGRAM_URL_PATTERN.search(url)
        if match:
            # Extract the path segment and code
            full_match = match.group(0)
            # Ensure trailing slash
            if not full_match.endswith('/'):
                full_match += '/'
            # Ensure https and www
            parsed = urlparse(full_match)
            return f"https://www.instagram.com{parsed.path}"

        return url
