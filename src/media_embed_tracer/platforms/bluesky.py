"""Bluesky. Canonical id: ``at://<did>/app.bsky.feed.post/<rkey>``.

URLs that use a handle instead of a DID come back with ``unresolved="bsky-handle"``;
the resolver (resolve.py) swaps the handle for the DID so handle and DID forms of the
same post share one key. Handle-form keys are never written to the dataset: if the
lookup fails, the hit waits in the resolve queue (state) for a later run.
"""

from __future__ import annotations

import re

from ..models import PostRef
from ._common import parse

NAME = "bluesky"
# Official oEmbed: <blockquote class="bluesky-embed" data-bluesky-uri="at://..." data-bluesky-cid=...>
BLOCKQUOTE_CLASSES = ("bluesky-embed",)

DID_RE = r"did:(?:plc:[a-z2-7]{24}|web:[A-Za-z0-9.\-]+(?:%3[Aa][0-9]+)?)"
HANDLE_RE = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}"
RKEY_RE = r"[A-Za-z0-9._~:-]{1,512}"
ACTOR_RE = rf"(?:{DID_RE}|{HANDLE_RE})"

_IS_DID = re.compile(rf"^{DID_RE}$")
_AT_URI = re.compile(rf"^at://({ACTOR_RE})/app\.bsky\.feed\.post/({RKEY_RE})$")
_PROFILE_POST = re.compile(rf"^/profile/({ACTOR_RE})/post/({RKEY_RE})/?$")
# embed.js builds iframes as https://embed.bsky.app/embed/<did>/app.bsky.feed.post/<rkey>
_EMBED_PATH = re.compile(rf"^/embed/({ACTOR_RE})/app\.bsky\.feed\.post/({RKEY_RE})/?$")


def is_did(actor: str) -> bool:
    return bool(_IS_DID.match(actor))


def make(actor: str, rkey: str) -> PostRef:
    if is_did(actor):
        # author is the DID; handles are resolved only for display/export (they change).
        return PostRef(NAME, f"at://{actor}/app.bsky.feed.post/{rkey}",
                       f"https://bsky.app/profile/{actor}/post/{rkey}", actor)
    handle = actor.lower()
    return PostRef(NAME, f"at://{handle}/app.bsky.feed.post/{rkey}",
                   f"https://bsky.app/profile/{handle}/post/{rkey}", f"@{handle}",
                   unresolved="bsky-handle")


def match(url: str) -> PostRef | None:
    url = url.strip()
    if url.startswith("at://"):
        m = _AT_URI.match(url.split("?")[0].split("#")[0])
        return make(m.group(1), m.group(2)) if m else None
    u = parse(url)
    if not u:
        return None
    if u.host in ("bsky.app", "www.bsky.app"):
        m = _PROFILE_POST.match(u.path)
    elif u.host == "embed.bsky.app":
        m = _EMBED_PATH.match(u.path)
    else:
        return None
    return make(m.group(1), m.group(2)) if m else None


def actor_of(canonical_id: str) -> str:
    return canonical_id[len("at://"):].split("/", 1)[0]
