"""Shared fakes. No test touches the network: Http and the Bluesky session are faked."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from media_embed_tracer.http import FetchResult

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHttp:
    """Maps URL -> body (str/bytes) or Exception/int status. Records every request."""

    def __init__(self, pages: dict | None = None, redirects: dict | None = None, final: dict | None = None) -> None:
        self.pages = dict(pages or {})
        self.redirects = dict(redirects or {})
        self.final = dict(final or {})  # url -> final URL after (simulated) HTTP redirects
        self.calls: list[str] = []
        self.redirect_calls: list[str] = []
        self.session = requests.Session()

    def get(self, url, accept="", timeout=None) -> FetchResult:
        self.calls.append(url)
        body = self.pages.get(url)
        if body is None:
            return FetchResult(False, url, 404, error="HTTP 404")
        if isinstance(body, int):
            return FetchResult(False, url, body, error=f"HTTP {body}")
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        return FetchResult(True, self.final.get(url, url), 200, body, "text/html; charset=utf-8", encoding="utf-8")

    def fetch_html(self, url) -> FetchResult:
        return self.get(url)

    def first_redirect_target(self, url, max_hops=4) -> str:
        self.redirect_calls.append(url)
        return self.redirects.get(url, "")


@pytest.fixture
def now():
    return NOW
