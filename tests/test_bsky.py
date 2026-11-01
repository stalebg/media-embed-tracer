"""Bluesky XRPC client with a mocked requests session."""

import pytest
import requests

from media_embed_tracer.bsky import APPVIEW_PROXY, BlueskyClient, LoginError, PermanentError, TransientError

BOT = "did:plc:zzzzzzzzzzzzzzzzzzzzzzzz"


class Resp:
    def __init__(self, status=200, data=None):
        self.status_code, self._data = status, data or {}
        self.ok = status < 400
        self.content = b"x"
        self.text = str(data)

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json,
                           "headers": headers or {}, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def logged_in(*responses):
    session = Session([Resp(200, {"did": BOT, "accessJwt": "jwt", "didDoc": {"service": [
        {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://pds.test"}]}}),
        *responses])
    c = BlueskyClient(session, "https://entryway.test", timeout=7)
    c.login("bot.example.com", "app-pass")
    return c, session


def test_login_failure_leaves_client_logged_out():
    """A failed login leaves the client logged out."""
    c = BlueskyClient(Session([Resp(401, {"error": "AuthenticationRequired", "message": "Invalid"})]))
    with pytest.raises(LoginError):
        c.login("bot", "wrong")
    assert c.did == "" and c._jwt == ""


def test_login_network_error_is_login_error():
    c = BlueskyClient(Session([requests.ConnectionError("down")]))
    with pytest.raises(LoginError):
        c.login("bot", "pw")


def test_login_requires_credentials():
    with pytest.raises(LoginError):
        BlueskyClient(Session([])).login("", "")


def test_login_switches_to_account_pds_and_uses_timeout():
    c, s = logged_in(Resp(200, {"posts": []}))
    c.get_posts(["at://did:plc:a/app.bsky.feed.post/1"])
    assert s.calls[0]["url"] == "https://entryway.test/xrpc/com.atproto.server.createSession"
    assert s.calls[1]["url"] == "https://pds.test/xrpc/app.bsky.feed.getPosts"
    assert all(call["timeout"] == 7 for call in s.calls)


def test_get_posts_goes_to_appview_via_proxy_header():
    """getPosts goes to the AppView through the atproto-proxy header."""
    uri = "at://did:plc:aaaaaaaaaaaaaaaaaaaaaaaa/app.bsky.feed.post/3k"
    c, s = logged_in(Resp(200, {"posts": [{"uri": uri, "cid": "bafy", "viewer": {}}]}))
    assert c.get_posts([uri])[uri]["cid"] == "bafy"
    call = s.calls[1]
    assert call["headers"]["atproto-proxy"] == APPVIEW_PROXY
    assert call["headers"]["Authorization"] == "Bearer jwt"
    assert call["params"] == {"uris": [uri]}


def test_recent_quotes_maps_subject_to_our_post():
    feed = {"feed": [
        {"post": {"uri": f"at://{BOT}/app.bsky.feed.post/q1", "author": {"did": BOT},
                  "record": {"embed": {"$type": "app.bsky.embed.record", "record": {"uri": "at://did:plc:a/x/1"}}}}},
        {"post": {"uri": f"at://{BOT}/app.bsky.feed.post/q2", "author": {"did": BOT}, "record": {"text": "plain"}}},
        {"post": {"uri": "at://did:plc:other/app.bsky.feed.post/r", "author": {"did": "did:plc:other"},
                  "record": {"embed": {"$type": "app.bsky.embed.record", "record": {"uri": "at://did:plc:b/x/2"}}}}},
    ]}
    c, s = logged_in(Resp(200, feed))
    assert c.recent_quotes() == {"at://did:plc:a/x/1": f"at://{BOT}/app.bsky.feed.post/q1"}
    assert s.calls[1]["params"]["actor"] == BOT


@pytest.mark.parametrize(("status", "exc"), [(429, TransientError), (502, TransientError), (400, PermanentError)])
def test_error_mapping(status, exc):
    c, _ = logged_in(Resp(status, {"error": "X", "message": "m"}))
    with pytest.raises(exc):
        c.create_post({"text": "t"})


def test_create_post_writes_to_own_repo():
    c, s = logged_in(Resp(200, {"uri": f"at://{BOT}/app.bsky.feed.post/new", "cid": "c"}))
    assert c.create_post({"$type": "app.bsky.feed.post"}).endswith("/new")
    assert s.calls[1]["json"]["repo"] == BOT and s.calls[1]["json"]["collection"] == "app.bsky.feed.post"


def _own(rkey, embed):
    return {"post": {"uri": f"at://{BOT}/app.bsky.feed.post/{rkey}", "author": {"did": BOT},
                     "record": {"text": "t", "embed": embed}}}


def test_recent_quotes_follows_cursor_and_counts_quotes_with_media():
    """Idempotency guard: older pages are read too, and a quote with media is still a quote."""
    page1 = {"feed": [_own(f"p{i}", {"$type": "app.bsky.embed.images"}) for i in range(100)], "cursor": "c1"}
    page2 = {"feed": [_own("q", {"$type": "app.bsky.embed.recordWithMedia",
                                 "record": {"record": {"uri": "at://did:plc:a/app.bsky.feed.post/1"}},
                                 "media": {"$type": "app.bsky.embed.images"}})], "cursor": "c2"}
    page3 = {"feed": []}
    c, s = logged_in(Resp(200, page1), Resp(200, page2), Resp(200, page3))
    assert c.recent_quotes(limit=250) == {"at://did:plc:a/app.bsky.feed.post/1": f"at://{BOT}/app.bsky.feed.post/q"}
    assert [call["params"].get("cursor") for call in s.calls[1:]] == [None, "c1", "c2"]
    assert [call["params"]["limit"] for call in s.calls[1:]] == [100, 100, 100]


def test_recent_quotes_stops_at_limit():
    page1 = {"feed": [_own(f"p{i}", {}) for i in range(100)], "cursor": "c1"}
    c, s = logged_in(Resp(200, page1))
    assert c.recent_quotes(limit=100) == {}
    assert len(s.calls) == 2


def test_quote_check_window_covers_every_eligible_quote():
    from media_embed_tracer.posting import Limits, quote_check_window

    assert quote_check_window(Limits(max_age_hours=24, daily_cap=30)) == 100
    assert quote_check_window(Limits(max_age_hours=72, daily_cap=60)) == 240
    assert quote_check_window(Limits(max_age_hours=24 * 30, daily_cap=200)) == 1000
