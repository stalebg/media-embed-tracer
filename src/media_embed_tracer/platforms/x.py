"""X / Twitter. Canonical id: numeric status id."""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "x"
BLOCKQUOTE_CLASSES = ("twitter-tweet", "twitter-video")  # official oEmbed: blockquote.twitter-tweet

_HOSTS = {"twitter.com", "x.com"}
_EMBED_HOSTS = {"platform.twitter.com", "platform.x.com"}
_I_STATUS = re.compile(r"^/i/(?:web/)?status/(\d{1,20})(?:/|$)")
_USER_STATUS = re.compile(r"^/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{1,20})(?:/|$)")
_RESERVED = {"i", "intent", "share", "home", "search", "hashtag", "explore", "settings", "messages"}


def make(status_id: str, user: str = "") -> PostRef:
    if user:
        return PostRef(NAME, status_id, f"https://x.com/{user}/status/{status_id}", f"@{user}")
    return PostRef(NAME, status_id, f"https://x.com/i/status/{status_id}", "")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u:
        return None
    if u.host in _EMBED_HOSTS:
        # UNVERIFIED: iframe src produced by widgets.js (not in official docs).
        if u.path.startswith("/embed/Tweet.html") and u.q("id").isdigit():
            return make(u.q("id"))
        return None
    if u.base_host not in _HOSTS:
        return None
    m = _I_STATUS.match(u.path)
    if m:
        return make(m.group(1))
    m = _USER_STATUS.match(u.path)
    if m:
        user = m.group(1)
        return make(m.group(2), "" if user.lower() in _RESERVED else user)
    return None
