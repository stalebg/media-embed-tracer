"""Facebook platform detection and URL handling."""

from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs, unquote

from bs4 import BeautifulSoup

from .base import BasePlatform


class FacebookPlatform(BasePlatform):
    """Detect and handle Facebook embeds."""

    # Regex patterns for Facebook post URLs
    # Facebook has many URL formats for posts:
    # - facebook.com/username/posts/ID
    # - facebook.com/permalink.php?story_fbid=ID&id=USER
    # - facebook.com/photo.php?fbid=ID
    # - facebook.com/watch/?v=ID
    # - facebook.com/reel/ID
    # - facebook.com/ID/videos/ID
    # - fb.watch/ID

    FACEBOOK_POST_PATTERNS = [
        # Standard post URL
        re.compile(
            r'https?://(?:www\.)?facebook\.com/([^/]+)/posts/([a-zA-Z0-9]+)',
            re.IGNORECASE
        ),
        # Permalink format
        re.compile(
            r'https?://(?:www\.)?facebook\.com/permalink\.php\?',
            re.IGNORECASE
        ),
        # Photo posts
        re.compile(
            r'https?://(?:www\.)?facebook\.com/photo(?:\.php)?\?',
            re.IGNORECASE
        ),
        # Video/Watch
        re.compile(
            r'https?://(?:www\.)?facebook\.com/watch/?\?v=(\d+)',
            re.IGNORECASE
        ),
        # Reels
        re.compile(
            r'https?://(?:www\.)?facebook\.com/reel/(\d+)',
            re.IGNORECASE
        ),
        # Video in profile
        re.compile(
            r'https?://(?:www\.)?facebook\.com/([^/]+)/videos/(\d+)',
            re.IGNORECASE
        ),
        # fb.watch short links
        re.compile(
            r'https?://fb\.watch/([a-zA-Z0-9_-]+)',
            re.IGNORECASE
        ),
        # Story links
        re.compile(
            r'https?://(?:www\.)?facebook\.com/stories/',
            re.IGNORECASE
        ),
    ]

    # General Facebook content pattern (catches most)
    FACEBOOK_GENERAL_PATTERN = re.compile(
        r'https?://(?:www\.)?(?:facebook\.com|fb\.watch)/[^\s"\'<>]+',
        re.IGNORECASE
    )

    @property
    def name(self) -> str:
        return "facebook"

    def detect_embeds(self, html: str, article_url: str) -> list[str]:
        """
        Detect Facebook embeds in HTML.

        Looks for:
        - Direct facebook.com links to posts/videos/reels
        - fb.watch short links
        - Facebook embed divs
        """
        urls = set()

        # Parse HTML
        soup = BeautifulSoup(html, 'html.parser')

        # Find direct links to Facebook
        for link in soup.find_all('a', href=True):
            href = link['href']
            if self._is_facebook_content_url(href):
                urls.add(href)

        # Find Facebook embed divs (class="fb-post", "fb-video", etc.)
        for div in soup.find_all(['div', 'blockquote'], class_=re.compile(r'fb-(post|video|reel)')):
            data_href = div.get('data-href')
            if data_href and self._is_facebook_content_url(data_href):
                urls.add(data_href)

        # Find iframes with Facebook embeds
        for iframe in soup.find_all('iframe', src=True):
            src = iframe['src']
            if 'facebook.com/plugins/' in src:
                # Try to extract the actual post URL from the embed
                parsed = urlparse(src)
                query = parse_qs(parsed.query)
                href = query.get('href', [None])[0]
                if href:
                    decoded_href = unquote(href)
                    if self._is_facebook_content_url(decoded_href):
                        urls.add(decoded_href)

        # Search raw HTML for Facebook URLs
        for match in self.FACEBOOK_GENERAL_PATTERN.finditer(html):
            url = match.group(0)
            if self._is_facebook_content_url(url):
                urls.add(url)

        return list(urls)

    def _is_facebook_content_url(self, url: str) -> bool:
        """Check if a URL is a Facebook content URL (not just the homepage)."""
        # Skip homepage and generic pages
        skip_patterns = [
            r'^https?://(?:www\.)?facebook\.com/?$',
            r'^https?://(?:www\.)?facebook\.com/home',
            r'^https?://(?:www\.)?facebook\.com/login',
            r'^https?://(?:www\.)?facebook\.com/sharer',
            r'^https?://(?:www\.)?facebook\.com/share',
        ]

        for pattern in skip_patterns:
            if re.match(pattern, url, re.IGNORECASE):
                return False

        # Check against our content patterns
        for pattern in self.FACEBOOK_POST_PATTERNS:
            if pattern.search(url):
                return True

        return False

    def extract_author(self, post_url: str) -> str:
        """
        Extract author handle from Facebook URL.

        Note: Many Facebook URL formats don't expose the author directly.
        """
        # Try standard post URL format
        match = re.search(
            r'facebook\.com/([^/]+)/(?:posts|videos)/',
            post_url,
            re.IGNORECASE
        )
        if match:
            username = match.group(1)
            # Filter out generic paths
            if username not in ['permalink.php', 'photo.php', 'watch', 'reel', 'stories']:
                return username

        # For other formats, we can't easily extract the author
        return "(see post)"

    def normalize_url(self, url: str) -> str:
        """Normalize Facebook URL."""
        url = url.strip()

        # Remove tracking parameters but keep essential ones
        parsed = urlparse(url)

        # For most Facebook URLs, keep the path and essential query params
        if 'facebook.com' in parsed.netloc:
            # Keep story_fbid and id for permalink URLs
            if 'permalink.php' in parsed.path or 'photo.php' in parsed.path:
                query = parse_qs(parsed.query)
                essential = {}
                for key in ['story_fbid', 'id', 'fbid', 'v']:
                    if key in query:
                        essential[key] = query[key][0]

                if essential:
                    query_str = '&'.join(f"{k}={v}" for k, v in essential.items())
                    return f"https://www.facebook.com{parsed.path}?{query_str}"

            # For clean URLs, just normalize the domain
            return f"https://www.facebook.com{parsed.path}"

        # fb.watch links - keep as is
        if 'fb.watch' in parsed.netloc:
            return f"https://fb.watch{parsed.path}"

        return url
