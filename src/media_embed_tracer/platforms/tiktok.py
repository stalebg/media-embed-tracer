"""TikTok. Canonical id: numeric video/photo id.

Short links (vm./vt.tiktok.com, tiktok.com/t/...) come back with
``unresolved="tiktok-short"`` and are expanded (with a cache) by resolve.py.
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "tiktok"
# UNVERIFIED (legacy): blockquote.tiktok-embed with cite + data-video-id. TikTok's
# current oEmbed returns an iframe to /player/v1/<id> instead (verified).
BLOCKQUOTE_CLASSES = ("tiktok-embed",)

_USER_POST = re.compile(r"^/@([A-Za-z0-9_.]{1,64})/(?:video|photo)/(\d{8,25})(?:/|$)")
_EMBED = re.compile(r"^/(?:embed/v2|embed|player/v1)/(\d{8,25})(?:/|$)")
_MOBILE_V = re.compile(r"^/v/(\d{8,25})(?:\.html)?(?:/|$)")
_SHORT_T = re.compile(r"^/t/([A-Za-z0-9]{5,20})/?$")
_SHORT = re.compile(r"^/([A-Za-z0-9]{5,20})/?$")


def make(video_id: str, user: str = "") -> PostRef:
    if user:
        return PostRef(NAME, video_id, f"https://www.tiktok.com/@{user}/video/{video_id}", f"@{user}")
    return PostRef(NAME, video_id, f"https://www.tiktok.com/embed/v2/{video_id}", "")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u:
        return None
    if u.host in ("vm.tiktok.com", "vt.tiktok.com"):
        m = _SHORT.match(u.path)
        if m:
            code = m.group(1)
            return PostRef(NAME, f"short:{code}", f"https://{u.host}/{code}/", "",
                           unresolved="tiktok-short")
        return None
    if u.base_host != "tiktok.com":
        return None
    m = _USER_POST.match(u.path)
    if m:
        return make(m.group(2), m.group(1))
    m = _EMBED.match(u.path) or _MOBILE_V.match(u.path)
    if m:
        return make(m.group(1))
    m = _SHORT_T.match(u.path)
    if m:
        code = m.group(1)
        return PostRef(NAME, f"short:{code}", f"https://www.tiktok.com/t/{code}/", "",
                       unresolved="tiktok-short")
    return None
