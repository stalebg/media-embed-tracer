"""Mastodon (any instance). Canonical id: ``<host>/<status id>``.

Embed forms (verified against Mastodon docs / source):
  iframe.mastodon-embed src="https://<host>/@user/<id>/embed"
  blockquote.mastodon-embed data-embed-url="https://<host>/@user/<id>/embed"
Status ids are local to an instance, so the same post seen through two instances
gets two ids. Plain links need an 15-20 digit snowflake id to limit false positives.
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "mastodon"
BLOCKQUOTE_CLASSES = ("mastodon-embed",)

_EMBED = re.compile(r"^/@([A-Za-z0-9_]{1,30}(?:@[A-Za-z0-9.\-]+)?)/(\d{1,20})/embed/?$")
_STATUS = re.compile(r"^/@([A-Za-z0-9_]{1,30}(?:@[A-Za-z0-9.\-]+)?)/(\d{15,20})/?$")
_AP = re.compile(r"^/users/([A-Za-z0-9_]{1,30})/statuses/(\d{15,20})/?$")
_NOT_MASTODON = ("x.com", "twitter.com", "tiktok.com", "threads.com", "threads.net",
                 "instagram.com", "medium.com", "youtube.com", "facebook.com", "bsky.app")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u or not u.host or any(u.base_host == h or u.base_host.endswith("." + h) for h in _NOT_MASTODON):
        return None
    m = _EMBED.match(u.path) or _STATUS.match(u.path) or _AP.match(u.path)
    if not m:
        return None
    user, status_id = m.group(1), m.group(2)
    acct = user if "@" in user else f"{user}@{u.host}"
    return PostRef(NAME, f"{u.host}/{status_id}", f"https://{u.host}/@{user}/{status_id}", f"@{acct}")
