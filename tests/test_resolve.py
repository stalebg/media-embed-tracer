"""Handle -> DID normalisation, verified alsoKnownAs, TikTok short-link cache."""

from media_embed_tracer.models import Hit
from media_embed_tracer.platforms import match_url
from media_embed_tracer.resolve import PLC_DIRECTORY, Resolver

from .conftest import FakeHttp

APPVIEW = "https://appview.test"
DID = "did:plc:abcdefghijklmnopqrstuvwx"


def resolver(pages=None, redirects=None, **kw):
    http = FakeHttp(pages, redirects)
    return Resolver(http, APPVIEW, **kw), http


def test_handle_and_did_urls_share_a_key():
    r, _ = resolver({f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.example.com": {"did": DID}})
    by_handle = r.canonicalize(Hit(match_url("https://bsky.app/profile/alice.example.com/post/3kabc"), "blockquote"))
    by_did = r.canonicalize(Hit(match_url(f"https://bsky.app/profile/{DID}/post/3kabc"), "blockquote"))
    assert by_handle.key == by_did.key == f"bluesky:at://{DID}/app.bsky.feed.post/3kabc"
    assert by_handle.ref.unresolved == ""
    assert by_handle.ref.author == DID  # DIDs are stored; handles resolved only at export


def test_unresolvable_handle_stays_marked_unresolved():
    """A handle that cannot be resolved stays marked unresolved."""
    r, _ = resolver({})
    hit = r.canonicalize(Hit(match_url("https://bsky.app/profile/gone.example.com/post/3kabc"), "link"))
    assert hit.ref.unresolved == "bsky-handle"


def test_also_known_as_must_verify_both_ways():
    pages = {
        f"{PLC_DIRECTORY}/{DID}": {"alsoKnownAs": ["at://impostor.example.com"]},
        f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=impostor.example.com": {"did": "did:plc:zzzzzzzzzzzzzzzzzzzzzzzz"},
    }
    r, _ = resolver(pages)
    assert r.verified_handle(DID) == ""  # the claimed handle points elsewhere


def test_also_known_as_verified():
    pages = {
        f"{PLC_DIRECTORY}/{DID}": {"alsoKnownAs": ["at://alice.example.com"]},
        f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=alice.example.com": {"did": DID},
    }
    r, _ = resolver(pages)
    assert r.verified_handle(DID) == "alice.example.com"


def test_did_web_document_location():
    pages = {
        "https://example.com/.well-known/did.json": {"alsoKnownAs": ["at://example.com"]},
        f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle?handle=example.com": {"did": "did:web:example.com"},
    }
    r, _ = resolver(pages)
    assert r.verified_handle("did:web:example.com") == "example.com"


def test_tiktok_short_link_expanded_once_per_run():
    short = "https://vm.tiktok.com/ZMabc123/"
    r, http = resolver(redirects={short: "https://www.tiktok.com/@u/video/7300000000000000001?x=1"})
    hit = Hit(match_url(short), "link")
    for _ in range(3):
        assert r.canonicalize(hit).key == "tiktok:7300000000000000001"
    assert http.redirect_calls == [short]


def test_lookup_budget_is_capped():
    r, http = resolver(max_lookups=2)
    for i in range(5):
        r.canonicalize(Hit(match_url(f"https://vm.tiktok.com/ZMabc12{i}/"), "link"))
    assert len(http.redirect_calls) == 2
