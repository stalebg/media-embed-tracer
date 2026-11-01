"""Facebook. Canonical ids are typed so different objects never collapse:

    post:<id>      /<page>/posts/<id>, permalink.php / story.php ?story_fbid=, groups
    video:<id>     /watch?v=, /<page>/videos/[slug/]<id>, video.php?v=, /reel/<id>
    photo:<id>     photo.php?fbid=, /photo/?fbid=, /<page>/photos/<album>/<id>
    share:<k>:<c>  /share/p|v|r/<code> (not resolved: share links need a login to expand)
    fbwatch:<c>    fb.watch/<code> (not resolved)

Numeric story ids and pfbid ids for the same post stay distinct (Facebook does not
expose the mapping without the Graph API).
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "facebook"
# Official embedded posts / videos: div.fb-post[data-href], div.fb-video[data-href],
# with an inner blockquote.fb-xfbml-parse-ignore[cite].
BLOCKQUOTE_CLASSES = ("fb-post", "fb-video", "fb-xfbml-parse-ignore")

_ID = r"(pfbid[A-Za-z0-9]+|\d{5,25})"
_PAGE = r"([A-Za-z0-9.\-]{2,80})"
_PAGE_POST = re.compile(rf"^/{_PAGE}/posts/(?:[^/]+/)?{_ID}/?$")
_GROUP_POST = re.compile(rf"^/groups/{_PAGE}/(?:posts|permalink)/{_ID}/?$")
_PAGE_VIDEO = re.compile(rf"^/{_PAGE}/videos/(?:[^/]+/)?(\d{{5,25}})/?$")
_PAGE_PHOTO = re.compile(rf"^/{_PAGE}/photos/(?:[^/]+/)?(\d{{5,25}})/?$")
_REEL = re.compile(r"^/reels?/(\d{5,25})/?$")
_SHARE = re.compile(r"^/share/(?:(p|v|r)/)?([A-Za-z0-9]{5,40})/?$")
_RESERVED_PAGES = {"groups", "watch", "reel", "reels", "share", "plugins", "photo", "photos",
                   "permalink.php", "story.php", "photo.php", "video.php", "sharer", "dialog",
                   "login", "home.php", "events", "stories", "hashtag"}


def _ref(kind: str, obj_id: str, url: str, author: str = "") -> PostRef:
    return PostRef(NAME, f"{kind}:{obj_id}", url, author)


def match(url: str, _depth: int = 0) -> PostRef | None:
    u = parse(url)
    if not u:
        return None
    if u.host == "fb.watch":
        code = u.path.strip("/")
        if re.fullmatch(r"[A-Za-z0-9_-]{4,30}", code):
            return _ref("fbwatch", code, f"https://fb.watch/{code}/")
        return None
    if u.base_host not in ("facebook.com", "fb.com"):
        return None
    path = u.path.rstrip("/") or "/"

    # Plugin iframes wrap the real URL in ?href= (UNVERIFIED: SDK-generated, not in docs).
    if path in ("/plugins/post.php", "/plugins/video.php", "/plugins/comment_embed.php"):
        href = u.q("href")
        if href and _depth < 2:
            return match(href, _depth + 1)
        return None

    if path in ("/permalink.php", "/story.php"):
        story = u.q("story_fbid")
        if re.fullmatch(_ID, story or ""):
            owner = u.q("id")
            return _ref("post", story, f"https://www.facebook.com/permalink.php?story_fbid={story}"
                        + (f"&id={owner}" if owner.isdigit() else ""), owner if owner.isdigit() else "")
        return None
    if path in ("/photo.php", "/photo"):
        fbid = u.q("fbid")
        return _ref("photo", fbid, f"https://www.facebook.com/photo/?fbid={fbid}") if fbid.isdigit() else None
    if path in ("/watch", "/video.php"):
        vid = u.q("v")
        return _ref("video", vid, f"https://www.facebook.com/watch/?v={vid}") if vid.isdigit() else None

    m = _REEL.match(path)
    if m:
        # Reel ids are treated as video ids (assumption; not documented).
        return _ref("video", m.group(1), f"https://www.facebook.com/reel/{m.group(1)}")
    m = _SHARE.match(path)
    if m:
        kind = m.group(1) or "x"
        return _ref("share", f"{kind}:{m.group(2)}", f"https://www.facebook.com{u.path}")
    m = _GROUP_POST.match(path)
    if m:
        return _ref("post", m.group(2), f"https://www.facebook.com/groups/{m.group(1)}/posts/{m.group(2)}",
                    f"group:{m.group(1)}")
    for regex, kind, seg in ((_PAGE_POST, "post", "posts"), (_PAGE_VIDEO, "video", "videos"),
                             (_PAGE_PHOTO, "photo", "photos")):
        m = regex.match(path)
        if m and m.group(1).lower() not in _RESERVED_PAGES:
            page, obj = m.group(1), m.group(2)
            return _ref(kind, obj, f"https://www.facebook.com/{page}/{seg}/{obj}", page)
    return None
