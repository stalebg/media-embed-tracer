"""Telegram channel posts. Canonical id: ``<channel>/<message id>`` (channel lower-cased).

Official widget (verified, telegram-widget.js): <script data-telegram-post="channel/123">,
rendered as an iframe to https://t.me/<channel>/<id>?embed=1.
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "telegram"
BLOCKQUOTE_CLASSES: tuple[str, ...] = ()

_POST = re.compile(r"^/(?:s/)?([A-Za-z][A-Za-z0-9_]{3,31})/(\d{1,10})/?$")
_PRIVATE = re.compile(r"^/c/(\d{5,20})/(\d{1,10})/?$")
_RESERVED = {"joinchat", "addstickers", "share", "proxy", "iv", "socks", "addlist", "boost"}


def from_widget_value(value: str) -> PostRef | None:
    return match(f"https://t.me/{value.strip().strip('/')}")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u or u.base_host not in ("t.me", "telegram.me"):
        return None
    m = _PRIVATE.match(u.path)
    if m:
        cid = f"c/{m.group(1)}/{m.group(2)}"
        return PostRef(NAME, cid, f"https://t.me/{cid}", "")
    m = _POST.match(u.path)
    if not m or m.group(1).lower() in _RESERVED:
        return None
    channel, msg = m.group(1), m.group(2)
    return PostRef(NAME, f"{channel.lower()}/{msg}", f"https://t.me/{channel}/{msg}", f"@{channel}")
