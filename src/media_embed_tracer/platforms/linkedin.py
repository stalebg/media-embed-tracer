"""LinkedIn. Canonical id: ``urn:li:<share|ugcPost|activity>:<id>``.

UNVERIFIED (no official doc found): embed iframes look like
https://www.linkedin.com/embed/feed/update/urn:li:share:<id>. A share URN and the
activity URN of the same post differ, so they are not merged.
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "linkedin"
BLOCKQUOTE_CLASSES: tuple[str, ...] = ()

_URN = re.compile(r"^/(?:embed/)?feed/update/urn:li:(share|ugcPost|activity):(\d{10,25})/?$")
_SLUG = re.compile(r"^/posts/([A-Za-z0-9\-]+?)_[^/]*-(share|ugcPost|activity)-(\d{10,25})-[A-Za-z0-9]{2,8}/?$")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u or not (u.host == "linkedin.com" or u.host.endswith(".linkedin.com")):
        return None
    m = _URN.match(u.path)
    if m:
        urn = f"urn:li:{m.group(1)}:{m.group(2)}"
        return PostRef(NAME, urn, f"https://www.linkedin.com/feed/update/{urn}/", "")
    m = _SLUG.match(u.path)
    if m:
        urn = f"urn:li:{m.group(2)}:{m.group(3)}"
        return PostRef(NAME, urn, f"https://www.linkedin.com/feed/update/{urn}/", m.group(1))
    return None
