"""HTML content fetcher with retry logic and caching."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger(__name__)

# Default headers to mimic a browser
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}


def create_session() -> requests.Session:
    """Create a requests session with retry logic."""
    session = requests.Session()

    # Configure retry strategy
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update(DEFAULT_HEADERS)

    return session


# Global session for reuse
_session: Optional[requests.Session] = None


def get_session() -> requests.Session:
    """Get or create the global session."""
    global _session
    if _session is None:
        _session = create_session()
    return _session


def fetch_html(url: str, timeout: int = 30) -> Optional[str]:
    """
    Fetch HTML content from a URL.

    Args:
        url: The URL to fetch
        timeout: Request timeout in seconds

    Returns:
        HTML content as string, or None if fetch fails
    """
    try:
        session = get_session()
        response = session.get(url, timeout=timeout)
        response.raise_for_status()

        # Check content type
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' not in content_type and 'text/xml' not in content_type:
            logger.debug(f"Skipping non-HTML content: {content_type} for {url}")
            return None

        return response.text

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching {url}")
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP error fetching {url}: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching {url}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching {url}: {e}")

    return None


@lru_cache(maxsize=500)
def fetch_html_cached(url: str, timeout: int = 30) -> Optional[str]:
    """
    Fetch HTML content with caching.

    Useful when the same URL might be processed multiple times.

    Args:
        url: The URL to fetch
        timeout: Request timeout in seconds

    Returns:
        HTML content as string, or None if fetch fails
    """
    return fetch_html(url, timeout)


def clear_cache():
    """Clear the HTML cache."""
    fetch_html_cached.cache_clear()
    logger.debug("HTML cache cleared")
