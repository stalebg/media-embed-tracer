"""Threads. Canonical id: post shortcode.

Official embed (verified, Meta developer docs): blockquote.text-post-media with
data-text-post-permalink="https://www.threads.com/@user/post/<code>". Docs use
threads.com; threads.net URLs are still matched.
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "threads"
BLOCKQUOTE_CLASSES = ("text-post-media",)

_POST = re.compile(r"^/@([A-Za-z0-9_.]{1,30})/post/([A-Za-z0-9_-]{5,64})(?:/|$)")
_SHORT = re.compile(r"^/t/([A-Za-z0-9_-]{5,64})(?:/|$)")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u or u.base_host not in ("threads.com", "threads.net"):
        return None
    m = _POST.match(u.path)
    if m:
        user, code = m.group(1), m.group(2)
        return PostRef(NAME, code, f"https://www.threads.com/@{user}/post/{code}", f"@{user}")
    m = _SHORT.match(u.path)
    if m:
        return PostRef(NAME, m.group(1), f"https://www.threads.com/t/{m.group(1)}", "")
    return None
