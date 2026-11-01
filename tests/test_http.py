"""HTTP client: honest UA, timeouts on every request, size cap ."""

import pytest
import requests

from media_embed_tracer.http import DEFAULT_USER_AGENT, Http


class Resp:
    def __init__(self, chunks, status=200, ctype="text/html", encoding="utf-8"):
        self.chunks, self.status_code, self.ok = chunks, status, status < 400
        self.url, self.headers, self.encoding = "https://final.test/", {"Content-Type": ctype}, encoding

    def iter_content(self, n):
        yield from self.chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Session(requests.Session):
    def __init__(self, resp):
        super().__init__()
        self.resp, self.kwargs = resp, None

    def get(self, url, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def test_honest_user_agent():
    s = Session(Resp([b"<html></html>"]))
    Http(session=s)
    assert s.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert "Mozilla" not in DEFAULT_USER_AGENT and "Chrome" not in DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.startswith("media-embed-tracer/") and "http" not in DEFAULT_USER_AGENT


def test_timeout_always_passed_and_final_url_kept():
    s = Session(Resp([b"<html>ok</html>"]))
    res = Http(timeout=12, session=s).fetch_html("https://a.test/")
    assert res.ok and res.text == "<html>ok</html>" and res.url == "https://final.test/"
    assert s.kwargs["timeout"] == (10.0, 12)


def test_size_cap():
    s = Session(Resp([b"x" * 600, b"x" * 600]))
    res = Http(session=s, max_bytes=1000).get("https://a.test/")
    assert not res.ok and res.error == "response too large"


def test_non_html_rejected_and_errors_reported():
    assert not Http(session=Session(Resp([b"%PDF"], ctype="application/pdf"))).fetch_html("https://a").ok
    res = Http(session=Session(requests.Timeout("slow"))).get("https://a")
    assert not res.ok and "Timeout" in res.error
    assert Http(session=Session(Resp([b""], status=403))).get("https://a").error == "HTTP 403"


def test_no_charset_header_falls_back_to_meta_charset_or_the_bytes():
    """requests assumes ISO-8859-1 for text/html without a charset; most pages are UTF-8."""
    def fetch(body, ctype="text/html"):
        return Http(session=Session(Resp([body], ctype=ctype, encoding="ISO-8859-1"))).fetch_html("https://a")

    assert fetch("<p>blåbærsyltetøy – «sitat»</p>".encode()).text == "<p>blåbærsyltetøy – «sitat»</p>"
    cp = '<meta charset="windows-1252"><p>Søk – «x»</p>'
    assert fetch(cp.encode("cp1252")).text == cp
    equiv = '<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1"><p>Ærlig</p>'
    assert fetch(equiv.encode("latin-1")).text == equiv
    # A charset in the header still wins.
    assert fetch("Ærlig".encode("latin-1"), "text/html; charset=ISO-8859-1").text == "Ærlig"


@pytest.mark.parametrize("label", ["ISO-8859-1", "latin-1", "us-ascii", "ascii"])
def test_latin1_and_ascii_labels_decode_as_windows_1252_like_browsers(label):
    """Pages labelled ISO-8859-1 or ASCII are often cp1252 (– « » ’ €); browsers decode them so."""
    body = "<p>Søk – «x» ’ €</p>".encode("cp1252")
    for ctype, meta in ((f"text/html; charset={label}", ""), ("text/html", f'<meta charset="{label}">')):
        res = Http(session=Session(Resp([meta.encode() + body], ctype=ctype, encoding=label))).fetch_html("https://a")
        assert res.encoding == "cp1252" and res.text == meta + "<p>Søk – «x» ’ €</p>"
