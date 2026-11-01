"""Shared URL parsing for platform matchers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from ..util import clean_url

_HOST_PREFIXES = ("www.", "m.", "mobile.", "web.", "mbasic.", "touch.")


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
    url = clean_url(url)
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
