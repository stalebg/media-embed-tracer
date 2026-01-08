"""Bluesky platform detection and URL handling."""

from __future__ import annotations

import re
import logging
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

from .base import BasePlatform


class BlueskyPlatform(BasePlatform):
    """Detect and handle Bluesky (bsky.app) embeds."""

    # Regex patterns for Bluesky URLs
    BSKY_URL_PATTERN = re.compile(
        r'https?://bsky\.app/profile/([^/]+)/post/([a-zA-Z0-9]+)',
        re.IGNORECASE
    )

    # Pattern for at:// URIs
    AT_URI_PATTERN = re.compile(
        r'at://([^/]+)/app\.bsky\.feed\.post/([a-zA-Z0-9]+)',
        re.IGNORECASE
    )

    # DID pattern for resolving handles
    DID_PATTERN = re.compile(r'^did:plc:[a-zA-Z0-9]+$')

    @property
    def name(self) -> str:
        return "bluesky"

    def detect_embeds(self, html: str, article_url: str) -> list[str]:
        """
        Detect Bluesky embeds in HTML.

        Looks for:
        - Direct bsky.app links
        - at:// URIs
        - Bluesky embed blockquotes
        """
        urls = set()

        # Parse HTML
        soup = BeautifulSoup(html, 'html.parser')

        # Find direct links to bsky.app
        for link in soup.find_all('a', href=True):
            href = link['href']
            if 'bsky.app/profile/' in href and '/post/' in href:
                match = self.BSKY_URL_PATTERN.search(href)
                if match:
                    urls.add(href)

        # Find at:// URIs in the HTML text
        for match in self.AT_URI_PATTERN.finditer(html):
            at_uri = match.group(0)
            converted = self._at_uri_to_url(at_uri)
            if converted:
                urls.add(converted)

        # Find Bluesky embed blockquotes
        for blockquote in soup.find_all('blockquote', class_='bluesky-embed'):
            # The blockquote usually contains a link to the post
            link = blockquote.find('a', href=True)
            if link and 'bsky.app' in link['href']:
                match = self.BSKY_URL_PATTERN.search(link['href'])
                if match:
                    urls.add(link['href'])

        # Also check data attributes on embeds
        for elem in soup.find_all(attrs={'data-bluesky-uri': True}):
            uri = elem.get('data-bluesky-uri')
            if uri:
                if uri.startswith('at://'):
                    converted = self._at_uri_to_url(uri)
                    if converted:
                        urls.add(converted)
                elif 'bsky.app' in uri:
                    urls.add(uri)

        return list(urls)

    def extract_author(self, post_url: str) -> str:
        """Extract author handle from Bluesky URL."""
        match = self.BSKY_URL_PATTERN.search(post_url)
        if match:
            handle_or_did = match.group(1)
            # If it's a DID, try to resolve it to a handle
            if self.DID_PATTERN.match(handle_or_did):
                resolved = self._resolve_did(handle_or_did)
                return resolved if resolved else handle_or_did
            return handle_or_did
        return "unknown"

    def normalize_url(self, url: str) -> str:
        """Normalize Bluesky URL to canonical form."""
        url = url.strip()

        # Remove query parameters and fragments
        parsed = urlparse(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        # Ensure it matches the expected format
        match = self.BSKY_URL_PATTERN.search(clean_url)
        if match:
            handle = match.group(1)
            post_id = match.group(2)
            return f"https://bsky.app/profile/{handle}/post/{post_id}"

        return clean_url

    def _at_uri_to_url(self, at_uri: str) -> str | None:
        """Convert an at:// URI to a bsky.app URL."""
        match = self.AT_URI_PATTERN.match(at_uri)
        if match:
            did_or_handle = match.group(1)
            post_id = match.group(2)
            return f"https://bsky.app/profile/{did_or_handle}/post/{post_id}"
        return None

    @lru_cache(maxsize=100)
    def _resolve_did(self, did: str) -> str | None:
        """
        Resolve a DID to a handle using the PLC directory.

        Args:
            did: The DID to resolve (e.g., 'did:plc:xyz123')

        Returns:
            The resolved handle, or None if resolution fails
        """
        logger = logging.getLogger(__name__)

        try:
            # Try PLC directory first
            response = requests.get(
                f"https://plc.directory/{did}",
                timeout=10,
                headers={'Accept': 'application/json'}
            )

            if response.status_code == 200:
                data = response.json()
                # Look for handle in alsoKnownAs
                also_known_as = data.get('alsoKnownAs', [])
                for aka in also_known_as:
                    if aka.startswith('at://'):
                        handle = aka.replace('at://', '')
                        logger.debug(f"Resolved {did} to {handle}")
                        return handle

        except Exception as e:
            logger.warning(f"Failed to resolve DID {did}: {e}")

        return None
