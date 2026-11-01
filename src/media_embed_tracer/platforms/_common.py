"""Shared URL parsing for platform matchers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from ..util import clean_url

_HOST_PREFIXES = ("www.", "m.", "mobile.", "web.", "mbasic.", "touch.")

# Archive snapshot links wrap the original URL in their path, e.g.
#   https://web.archive.org/web/20260209200936mp_/https://bsky.app/profile/x/post/abc
#   https://archive.ph/2026.02.09-200936/https://x.com/u/status/1   (also /o/<id>/, /newest/)
# The original URL may have its "//" collapsed to "/" (some proxies) or no scheme at all.
_ARCHIVE_PREFIX = re.compile(
    r"^https?://(?:web\.)?archive\.org/web/(?:\d{1,14}|\*)(?:[a-z]{2}_)?/"
    r"|^https?://(?:www\.)?archive\.(?:ph|today|is|li|vn|fo|md)/"
    r"(?:o/[A-Za-z0-9]+/|newest/|oldest/|\d{4}[\d.]*-?\d*/)", re.I)
_SCHEME = re.compile(r"^(https?):/+", re.I)


def unwrap_archive(url: str) -> str:
    """The original URL inside a Wayback Machine or archive.today snapshot link (nested
    snapshots included); any other URL unchanged."""
    for _ in range(3):
        m = _ARCHIVE_PREFIX.match(url)
        if not m:
            break
        inner = url[m.end():]
        if not inner:
            break
        sm = _SCHEME.match(inner)
        url = f"{sm.group(1).lower()}://{inner[sm.end():]}" if sm else f"https://{inner}"
    return url


@dataclass(frozen=True)
class Url:
    raw: str
    scheme: str
    host: str       # lower-case, as given
    base_host: str  # host without www./m./mobile./web./mbasic./touch.
    path: str       # percent-decoded
    query: dict[str, list[str]]

    def q(self, name: str) -> str:
        values = self.query.get(name)
        return values[0] if values else ""


def parse(url: str) -> Url | None:
    url = unwrap_archive(clean_url(url))
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    base = host
    for prefix in _HOST_PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return Url(url, parts.scheme, host, base, unquote(parts.path), parse_qs(parts.query))
