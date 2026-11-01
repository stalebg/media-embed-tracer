"""Instagram. Canonical id: media shortcode (shared by /p/, /reel/, /tv/)."""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "instagram"
# Official embed: blockquote.instagram-media[data-instgrm-permalink]
BLOCKQUOTE_CLASSES = ("instagram-media",)

_POST = re.compile(r"^/(?:([A-Za-z0-9_.]{1,30})/)?(p|reel|reels|tv)/([A-Za-z0-9_-]{5,64})(?:/|$)")
_RESERVED_USERS = {"p", "reel", "reels", "tv", "explore", "stories", "accounts", "direct"}
_KIND_PATH = {"p": "p", "reel": "reel", "reels": "reel", "tv": "tv"}


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u or u.base_host not in ("instagram.com", "instagr.am"):
        return None
    m = _POST.match(u.path)
    if not m:
        return None
    user, kind, code = m.group(1), m.group(2), m.group(3)
    if user and user.lower() in _RESERVED_USERS:
        return None
    if kind == "reels" and code.lower() == "audio":
        return None  # /reels/audio/<id>/ is an audio page, not a post
    path_kind = _KIND_PATH[kind]
    return PostRef(NAME, code, f"https://www.instagram.com/{path_kind}/{code}/",
                   f"@{user}" if user else "")
