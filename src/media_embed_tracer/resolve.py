"""Network lookups that turn provisional post refs into canonical ones.

* Bluesky handle -> DID via com.atproto.identity.resolveHandle (AppView), so
  handle and DID URLs of one post share a key.
* TikTok short links -> numeric video id, by reading redirect Location headers.
* Bluesky DID -> display handle (export only, `export --resolve-handles`), accepted only
  if verified both ways: the DID document's alsoKnownAs claims at://<handle> AND that
  handle resolves back to the DID. The dataset itself stores DIDs.

All results are cached per run, and the number of lookups per run is capped. A hit
that is still unresolved after canonicalize() keeps ``ref.unresolved`` set; the
pipeline queues it instead of recording it.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from .http import Http
from .models import Hit, PostRef
from .platforms import bluesky, tiktok
from .redact import private

log = logging.getLogger(__name__)

PLC_DIRECTORY = "https://plc.directory"


class Resolver:
    def __init__(self, http: Http, appview: str = "https://public.api.bsky.app",
                 max_lookups: int = 60) -> None:
        self.http = http
        self.appview = appview.rstrip("/")
        self.max_lookups = max_lookups
        self.lookups = 0
        self._cache: dict[tuple[str, str], str] = {}

    def _budget(self) -> bool:
        if self.lookups >= self.max_lookups:
            return False
        self.lookups += 1
        return True

    def _json(self, url: str) -> dict | None:
        res = self.http.get(url, accept="application/json", timeout=10)
        if not res.ok:
            return None
        try:
            data = json.loads(res.content)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _cached(self, kind: str, key: str, fn) -> str:
        ck = (kind, key)
        if ck not in self._cache:
            self._cache[ck] = fn(key) if self._budget() else ""
        return self._cache[ck]

    # --- Bluesky ---------------------------------------------------------
    def handle_to_did(self, handle: str) -> str:
        def look(h: str) -> str:
            data = self._json(f"{self.appview}/xrpc/com.atproto.identity.resolveHandle?handle={quote(h)}")
            did = (data or {}).get("did", "")
            return did if bluesky.is_did(did) else ""
        return self._cached("handle", handle.lower(), look)

    def did_document(self, did: str) -> dict | None:
        if did.startswith("did:plc:"):
            return self._json(f"{PLC_DIRECTORY}/{did}")
        if did.startswith("did:web:"):
            host = did[len("did:web:"):].replace("%3A", ":").replace("%3a", ":")
            return self._json(f"https://{host}/.well-known/did.json")
        return None

    def verified_handle(self, did: str) -> str:
        def look(d: str) -> str:
            doc = self.did_document(d) or {}
            for aka in doc.get("alsoKnownAs") or []:
                if isinstance(aka, str) and aka.startswith("at://"):
                    handle = aka[len("at://"):].lower()
                    if self.handle_to_did(handle) == d:
                        return handle
            return ""
        return self._cached("verified", did, look)

    def cached_did(self, handle: str) -> str:
        """The DID of a handle already resolved this run, '' if none (no lookup)."""
        return self._cache.get(("handle", handle.lower()), "")

    # --- TikTok ----------------------------------------------------------
    def expand_tiktok(self, short_url: str) -> PostRef | None:
        target = self._cached("tiktok", short_url, self.http.first_redirect_target)
        ref = tiktok.match(target) if target else None
        return ref if ref and not ref.unresolved else None

    # --- public ----------------------------------------------------------
    def canonicalize(self, hit: Hit) -> Hit:
        ref = hit.ref
        if ref.unresolved == "bsky-handle":
            handle = bluesky.actor_of(ref.canonical_id)
            did = self.handle_to_did(handle)
            if did:
                return Hit(bluesky.make(did, ref.canonical_id.rsplit("/", 1)[1]), hit.embed_type)
            log.warning("could not resolve Bluesky handle %s; queued for a later run", private(handle))
        elif ref.unresolved == "tiktok-short":
            new = self.expand_tiktok(ref.post_url)
            if new:
                return Hit(new, hit.embed_type)
            log.info("could not expand TikTok short link %s; queued for a later run", private(ref.post_url))
        return hit


class OfflineResolver(Resolver):
    """No network: leaves refs as detected (used by tests and `detect --offline`)."""

    def __init__(self) -> None:
        self.lookups = 0
        self.max_lookups = 0
        self._cache = {}

    def canonicalize(self, hit: Hit) -> Hit:
        return hit
