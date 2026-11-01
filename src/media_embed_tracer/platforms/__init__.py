"""Platform registry. Each module exposes NAME, BLOCKQUOTE_CLASSES and match(url)."""

from __future__ import annotations

from ..models import PostRef
from . import bluesky, facebook, instagram, linkedin, mastodon, reddit, telegram, threads, tiktok, x, youtube

# Order matters only for match_url(): specific hosts first, Mastodon (any host) last.
PLATFORMS = (x, bluesky, tiktok, instagram, facebook, youtube, threads, reddit, telegram,
             linkedin, mastodon)
PLATFORM_NAMES = tuple(p.NAME for p in PLATFORMS)
BY_NAME = {p.NAME: p for p in PLATFORMS}
CLASS_TO_PLATFORM = {cls: p for p in PLATFORMS for cls in p.BLOCKQUOTE_CLASSES}


def match_url(url: str) -> PostRef | None:
    if not url:
        return None
    for platform in PLATFORMS:
        ref = platform.match(url)
        if ref:
            return ref
    return None
