"""HTTP with an honest User-Agent, per-request timeouts and a size cap."""

from __future__ import annotations

import codecs
import logging
import re
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__
from .redact import private

log = logging.getLogger(__name__)

# <meta charset="x"> or <meta http-equiv="Content-Type" content="text/html; charset=x">
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.IGNORECASE)


# Browsers decode these labels as windows-1252 (WHATWG Encoding Standard).
_AS_CP1252 = frozenset({"latin-1", "iso8859-1", "ascii"})


def _browser_name(name: str) -> str:
    """Python codec name for a charset label, with latin-1/iso-8859-1/ascii as cp1252
    (browser behaviour); '' for unknown labels."""
    try:
        name = codecs.lookup(name).name
    except LookupError:
        return ""
    return "cp1252" if name in _AS_CP1252 else name


def _encoding(content_type: str, header_encoding: str | None, content: bytes) -> str:
    """The header charset when there is one. Otherwise (requests would assume ISO-8859-1 for
    text/*) the page's meta charset, then a guess from the bytes; '' means UTF-8."""
    if "charset=" in content_type.lower():
        return _browser_name(header_encoding) if header_encoding else ""
    m = _META_CHARSET.search(content[:4096])
    if m:
        name = _browser_name(m.group(1).decode("ascii", "replace"))
        if name:
            return name
    try:
        content.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    guess = requests.compat.chardet.detect(content) if requests.compat.chardet else None
    guessed = (guess or {}).get("encoding") or ""
    return _browser_name(guessed) if guessed else ""


# Set USER_AGENT to add a contact URL.
DEFAULT_USER_AGENT = f"media-embed-tracer/{__version__}"


@dataclass
class FetchResult:
    ok: bool
    url: str
    status: int = 0
    content: bytes = b""
    content_type: str = ""
    error: str = ""

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    encoding: str = ""


class Http:
    def __init__(self, timeout: float = 20.0, user_agent: str = "", max_bytes: int = 8_000_000,
                 session: requests.Session | None = None) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.session = session or requests.Session()
        retry = Retry(total=2, connect=2, read=1, backoff_factor=1.0,
                      status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET", "HEAD"),
                      respect_retry_after_header=False, raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Accept-Language": "en;q=0.8, *;q=0.5",
        })

    def get(self, url: str, accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            timeout: float | None = None) -> FetchResult:
        """GET with (connect, read) timeouts plus an overall deadline and size cap."""
        timeout = timeout or self.timeout
        deadline = time.monotonic() + timeout * 2
        try:
            with self.session.get(url, headers={"Accept": accept}, timeout=(min(10.0, timeout), timeout),
                                  stream=True, allow_redirects=True) as resp:
                chunks, size = [], 0
                for chunk in resp.iter_content(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_bytes:
                        return FetchResult(False, url, resp.status_code, error="response too large")
                    if time.monotonic() > deadline:
                        return FetchResult(False, url, resp.status_code, error="deadline exceeded")
                content = b"".join(chunks)
                content_type = resp.headers.get("Content-Type", "")
                return FetchResult(resp.ok, resp.url, resp.status_code, content, content_type,
                                   "" if resp.ok else f"HTTP {resp.status_code}",
                                   encoding=_encoding(content_type, resp.encoding, content))
        except requests.RequestException as exc:
            return FetchResult(False, url, error=f"{type(exc).__name__}: {exc}"[:300])

    def fetch_html(self, url: str) -> FetchResult:
        res = self.get(url)
        if res.ok and res.content_type and not any(
                t in res.content_type for t in ("html", "xml", "text/plain")):
            res.ok, res.error = False, f"not HTML: {res.content_type}"
        return res

    def first_redirect_target(self, url: str, max_hops: int = 4) -> str:
        """Follow redirects by reading Location headers only (no body download)."""
        current = url
        for _ in range(max_hops):
            try:
                resp = self.session.get(current, allow_redirects=False, timeout=(5, 10), stream=True)
                resp.close()
            except requests.RequestException as exc:
                log.debug("redirect lookup failed for %s: %s", private(current), exc)
                return ""
            location = resp.headers.get("Location")
            if resp.status_code not in (301, 302, 303, 307, 308) or not location:
                return current if current != url else ""
            current = requests.compat.urljoin(current, location)
        return current
