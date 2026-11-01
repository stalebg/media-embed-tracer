"""YouTube. Canonical id: 11-character video id."""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "youtube"
BLOCKQUOTE_CLASSES: tuple[str, ...] = ()

_VID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH = re.compile(r"^/(?:embed|shorts|live|v|e)/([A-Za-z0-9_-]{11})(?:/|$)")


def make(video_id: str) -> PostRef:
    return PostRef(NAME, video_id, f"https://www.youtube.com/watch?v={video_id}", "")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u:
        return None
    if u.base_host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
        return make(vid) if _VID.match(vid) else None
    # youtube-nocookie.com = privacy-enhanced embed domain (verified, YouTube help 171780)
    if u.base_host not in ("youtube.com", "youtube-nocookie.com"):
        return None
    if u.path.rstrip("/") == "/watch":
        vid = u.q("v")
        return make(vid) if _VID.match(vid) else None
    m = _PATH.match(u.path)  # /embed/videoseries (playlist) is 11 chars too but excluded below
    if m and m.group(1) != "videoseries":
        return make(m.group(1))
    return None
