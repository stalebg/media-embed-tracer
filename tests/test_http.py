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


def _trickle_server(body: bytes, delay: float, headers: str):
    """A loopback HTTP server that sends `body` one byte every `delay` seconds."""
    import socket
    import threading
    import time

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n{headers}Connection: close\r\n\r\n".encode())
            if not delay:
                conn.sendall(body)
                return
            for i in range(len(body)):
                if stop.is_set():
                    return
                try:
                    conn.sendall(body[i:i + 1])
                except OSError:
                    return
                time.sleep(delay)

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}/", stop, srv


@pytest.mark.parametrize("chunked", [False, True])
def test_overall_deadline_bounds_a_trickling_body(chunked):
    """Each byte arrives well within the read timeout, so only the overall deadline
    (2 x timeout) can stop the download; it must not wait for a full 64 KB chunk."""
    import time

    body = b"<html>" + b"x" * 200 + b"</html>"   # ~40 s at 0.2 s per byte
    if chunked:
        body = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        headers = "Transfer-Encoding: chunked\r\n"
    else:
        headers = f"Content-Length: {len(body)}\r\n"
    url, stop, srv = _trickle_server(body, 0.2, headers)
    try:
        t0 = time.monotonic()
        res = Http(timeout=0.75).get(url)
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        srv.close()
    assert not res.ok and res.error == "deadline exceeded"
    assert elapsed < 4


def test_a_normal_loopback_response_is_read_in_full():
    import gzip

    page = b"<html>" + "blåbær".encode() * 5000 + b"</html>"
    for headers, body in ((f"Content-Length: {len(page)}\r\n", page),
                          (f"Content-Encoding: gzip\r\nContent-Length: {len(gzip.compress(page))}\r\n",
                           gzip.compress(page))):
        url, stop, srv = _trickle_server(body, 0, headers)
        try:
            res = Http(timeout=5).get(url)
        finally:
            stop.set()
            srv.close()
        assert res.ok and res.content == page
