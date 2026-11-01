"""Minimal Bluesky XRPC client (requests + timeouts; no SDK).

* login: com.atproto.server.createSession on the PDS / entryway (BLUESKY_SERVICE)
* read:  app.bsky.feed.getPosts and getAuthorFeed, sent to the PDS with the
         atproto-proxy header so they are answered by the Bluesky AppView
         (gives viewer.embeddingDisabled for quote gating)
* write: com.atproto.repo.createRecord into the bot's own repo
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

APPVIEW_PROXY = "did:web:api.bsky.app#bsky_appview"


class BlueskyError(RuntimeError):
    pass


class LoginError(BlueskyError):
    pass


class TransientError(BlueskyError):
    """Worth retrying later: network problems, 5xx, rate limits."""


class PermanentError(BlueskyError):
    """Retrying will not help: invalid request, post not found, ..."""


def quoted_subject(embed: dict) -> str:
    """The at:// URI a post record quotes (app.bsky.embed.record or recordWithMedia), or ''."""
    kind = embed.get("$type")
    if kind == "app.bsky.embed.record":
        return str((embed.get("record") or {}).get("uri") or "")
    if kind == "app.bsky.embed.recordWithMedia":
        return str(((embed.get("record") or {}).get("record") or {}).get("uri") or "")
    return ""


class BlueskyClient:
    def __init__(self, session: requests.Session, service: str = "https://bsky.social",
                 timeout: float = 20.0, proxy: str = APPVIEW_PROXY) -> None:
        self.session = session
        self.service = service.rstrip("/")
        self.timeout = timeout
        self.proxy = proxy
        self.did = ""
        self._jwt = ""

    def _raise_for(self, resp: requests.Response, nsid: str) -> None:
        if resp.ok:
            return
        try:
            body = resp.json()
            detail = f"{body.get('error', '')}: {body.get('message', '')}"
        except ValueError:
            detail = resp.text[:200]
        msg = f"{nsid} HTTP {resp.status_code} {detail}"
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(msg)
        raise PermanentError(msg)

    def _request(self, method: str, nsid: str, *, params=None, json=None, proxy: bool = False) -> dict:
        headers = {}
        if self._jwt:
            headers["Authorization"] = f"Bearer {self._jwt}"
        if proxy and self.proxy:
            headers["atproto-proxy"] = self.proxy
        try:
            resp = self.session.request(method, f"{self.service}/xrpc/{nsid}", params=params, json=json,
                                        headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise TransientError(f"{nsid}: {type(exc).__name__}: {exc}") from exc
        self._raise_for(resp, nsid)
        try:
            return resp.json() if resp.content else {}
        except ValueError as exc:
            raise TransientError(f"{nsid}: invalid JSON response") from exc

    def login(self, identifier: str, app_password: str) -> str:
        """Log in. Any failure raises LoginError and leaves the client logged out."""
        if not identifier or not app_password:
            raise LoginError("BLUESKY_HANDLE and BLUESKY_APP_PASSWORD must both be set")
        self.did, self._jwt = "", ""
        try:
            data = self._request("POST", "com.atproto.server.createSession",
                                 json={"identifier": identifier, "password": app_password})
        except BlueskyError as exc:
            raise LoginError(f"Bluesky login failed: {exc}") from exc
        did, jwt = data.get("did", ""), data.get("accessJwt", "")
        if not did or not jwt:
            raise LoginError("Bluesky login response missing did/accessJwt")
        self.did, self._jwt = did, jwt
        # Talk to the account's own PDS from now on (as the official SDKs do).
        for svc in (data.get("didDoc") or {}).get("service") or []:
            if svc.get("id") in ("#atproto_pds", f"{did}#atproto_pds") and str(
                    svc.get("serviceEndpoint", "")).startswith("https://"):
                self.service = svc["serviceEndpoint"].rstrip("/")
        return did

    def resolve_handle(self, handle: str) -> str:
        data = self._request("GET", "com.atproto.identity.resolveHandle", params={"handle": handle})
        return data.get("did", "")

    def get_posts(self, uris: list[str]) -> dict[str, dict]:
        """uri -> postView for posts that exist (max 25 per call)."""
        out: dict[str, dict] = {}
        for i in range(0, len(uris), 25):
            data = self._request("GET", "app.bsky.feed.getPosts", params={"uris": uris[i:i + 25]}, proxy=True)
            for post in data.get("posts", []):
                out[post.get("uri", "")] = post
        return out

    def recent_quotes(self, limit: int = 100) -> dict[str, str]:
        """subject uri -> our quote post uri, from the bot's latest `limit` posts (idempotency
        guard). Follows the feed cursor in pages of 100. Plain quotes and quotes with media
        both count."""
        quotes: dict[str, str] = {}
        cursor, read = "", 0
        while read < limit:
            params = {"actor": self.did, "limit": min(limit - read, 100), "filter": "posts_no_replies"}
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "app.bsky.feed.getAuthorFeed", params=params, proxy=True)
            feed = data.get("feed") or []
            for item in feed:
                post = item.get("post") or {}
                if (post.get("author") or {}).get("did") != self.did:
                    continue  # reposts of other people's posts
                subject = quoted_subject((post.get("record") or {}).get("embed") or {})
                if subject:
                    quotes.setdefault(subject, post.get("uri", ""))
            read += len(feed)
            cursor = data.get("cursor") or ""
            if not feed or not cursor:
                break
        return quotes

    def create_post(self, record: dict) -> str:
        data = self._request("POST", "com.atproto.repo.createRecord",
                             json={"repo": self.did, "collection": "app.bsky.feed.post", "record": record})
        uri = data.get("uri", "")
        if not uri:
            raise TransientError("createRecord returned no uri")
        return uri
