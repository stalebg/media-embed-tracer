"""Small helpers shared across modules: UTC time, URL cleanup, plain text."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str:
    """Format a datetime as ISO 8601 UTC with a trailing Z ('' for None)."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


_TRAILING_JUNK = ").,;:!?'\"]>"


def clean_url(raw: str | None) -> str:
    """Normalise a URL-ish string found in markup.

    Repeatedly HTML-unescapes (so ``&amp;amp;`` collapses to ``&``), strips
    whitespace and trailing punctuation, and makes protocol-relative URLs https.
    """
    if not raw:
        return ""
    url = raw.strip()
    for _ in range(5):
        unescaped = html.unescape(url)
        if unescaped == url:
            break
        url = unescaped
    url = url.strip().rstrip(_TRAILING_JUNK)
    if url.startswith("//"):
        url = "https:" + url
    return url


_TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ocid|cmpid|at_medium|at_campaign)$", re.I)


def canonical_article_url(url: str) -> str:
    """Article URL used as the seen-article key: no fragment, no tracking params."""
    url = clean_url(url)
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       urlencode(query), ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# Article summaries from RSS (v1 kept 500 characters).
SUMMARY_LIMIT = 500


def plain_text(value: str | None, limit: int | None = None) -> str:
    """Strip HTML tags/entities and control characters, collapse whitespace."""
    if not value:
        return ""
    if "<" in value or "&" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ")
    value = _CONTROL.sub("", value)
    value = _WS.sub(" ", value).strip()
    if limit and len(value) > limit:
        cut = value[: limit - 1]
        space = cut.rfind(" ")
        if space >= limit * 0.8:   # end on a word boundary when one is close
            cut = cut[:space]
        value = cut.rstrip(" ,;:-") + "…"
    return value
