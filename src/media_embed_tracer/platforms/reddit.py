"""Reddit. Canonical id: ``t3_<post id>`` or ``t1_<comment id>``.

Official widgets.js targets .reddit-embed-bq, .reddit-everywhere-bq, .reddit-card,
.embedly-card, .reddit-embed and builds iframes at embed.reddit.com/<same path>.
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "reddit"
BLOCKQUOTE_CLASSES = ("reddit-embed-bq", "reddit-everywhere-bq", "reddit-card", "reddit-embed")

_COMMENTS = re.compile(
    r"^/(?:(?:r|u|user)/([A-Za-z0-9_-]{2,30})/)?comments/([a-z0-9]{3,12})"
    r"(?:/[^/]*(?:/([a-z0-9]{3,12}))?)?/?$")
_SHARE = re.compile(r"^/r/([A-Za-z0-9_]{2,30})/s/([A-Za-z0-9]{5,20})/?$")


def match(url: str) -> PostRef | None:
    u = parse(url)
    if not u:
        return None
    if u.base_host == "redd.it":
        pid = u.path.strip("/")
        if re.fullmatch(r"[a-z0-9]{3,12}", pid):
            return PostRef(NAME, f"t3_{pid}", f"https://www.reddit.com/comments/{pid}/", "")
        return None
    host = u.host
    if not (host == "reddit.com" or host.endswith(".reddit.com")):
        return None
    m = _COMMENTS.match(u.path)
    if m:
        sub, pid, cid = m.group(1), m.group(2), m.group(3)
        prefix = f"/r/{sub}" if sub and u.path.startswith("/r/") else ""
        if cid:
            return PostRef(NAME, f"t1_{cid}", f"https://www.reddit.com{prefix}/comments/{pid}/_/{cid}/", "")
        return PostRef(NAME, f"t3_{pid}", f"https://www.reddit.com{prefix}/comments/{pid}/", "")
    m = _SHARE.match(u.path)
    if m:  # share links redirect to the real post; not resolved
        return PostRef(NAME, f"share:{m.group(2)}", f"https://www.reddit.com/r/{m.group(1)}/s/{m.group(2)}", "")
    return None
