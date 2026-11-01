"""Detection over HTML fixtures: canonical ids and embed types."""

import pytest

from media_embed_tracer.detect import detect
from media_embed_tracer.models import EMBED_MARKUP_TYPES

from .conftest import fixture

DID = "did:plc:abcdefghijklmnopqrstuvwx"

EXPECTED = {
    "x.html": {
        "x:1234567890123456789": "blockquote",
        "x:2222222222222222222": "iframe",
        "x:3333333333333333333": "link",
        "x:4444444444444444444": "link",
    },
    "bluesky.html": {
        f"bluesky:at://{DID}/app.bsky.feed.post/3kabc2defgh2i": "blockquote",
        "bluesky:at://did:web:example.com/app.bsky.feed.post/3kzzz2yyyyy2x": "iframe",
        "bluesky:at://someone.bsky.social/app.bsky.feed.post/3klink2linkk2": "link",
    },
    "tiktok.html": {
        "tiktok:7300000000000000001": "blockquote",
        "tiktok:7300000000000000002": "iframe",
        "tiktok:7300000000000000003": "iframe",
        "tiktok:7300000000000000004": "link",
        "tiktok:short:ZMabc123": "link",
    },
    "instagram.html": {
        "instagram:CAbc123_-xy": "blockquote",
        "instagram:CReel456789": "iframe",
        "instagram:CUser98765x": "link",
    },
    "facebook.html": {
        "facebook:post:pfbid02abcDEF123": "widget",
        "facebook:video:111111111111111": "widget",
        "facebook:post:222222222222222": "iframe",
        "facebook:photo:444444444444444": "link",
        "facebook:video:555555555555555": "link",
        "facebook:share:p:AbCdEf123": "link",
        "facebook:post:666666666666666": "link",
    },
    "youtube.html": {
        "youtube:dQw4w9WgXcQ": "iframe",
        "youtube:aaaaaaaaaaa": "widget",
        "youtube:bbbbbbbbbbb": "link",
        "youtube:ccccccccccc": "link",
        "youtube:ddddddddddd": "link",
    },
    "threads.html": {"threads:DAbCdEf12Gh": "blockquote", "threads:CzYxWv98Ut": "link"},
    "mastodon.html": {
        "mastodon:mastodon.social/113000000000000001": "blockquote",
        "mastodon:fosstodon.org/112000000000000002": "iframe",
        "mastodon:example.social/111000000000000003": "link",
    },
    "reddit.html": {"reddit:t3_abc123": "blockquote", "reddit:t1_ghi789": "iframe", "reddit:t3_xyz987": "link"},
    "telegram.html": {
        "telegram:examplechannel/1234": "widget",
        "telegram:otherchannel/55": "iframe",
        "telegram:linkchannel/77": "link",
    },
    "linkedin.html": {
        "linkedin:urn:li:share:7100000000000000001": "iframe",
        "linkedin:urn:li:activity:7100000000000000002": "link",
    },
    "json_page.html": {
        "x:5555555555555555555": "script-data",
        "bluesky:at://did:plc:bbbbbbbbbbbbbbbbbbbbbbbb/app.bsky.feed.post/3kjson2jsonn2": "script-data",
        "youtube:eeeeeeeeeee": "script-data",
    },
    "amp.html": {
        "x:6666666666666666666": "widget",
        "instagram:CAmp1234567": "widget",
        "youtube:fffffffffff": "widget",
        "bluesky:at://did:plc:cccccccccccccccccccccccc/app.bsky.feed.post/3kamp2ampam2": "iframe",
        "tiktok:7300000000000000009": "widget",
    },
    "sidebar_noise.html": {"x:8888888888888888888": "link"},
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture(name):
    found = {h.key: h.embed_type for h in detect(fixture(name))}
    assert found == EXPECTED[name]


def test_one_hit_per_post_per_article_with_strongest_type():
    """A post both embedded and linked is reported once, as blockquote."""
    hits = [h for h in detect(fixture("x.html")) if h.key == "x:1234567890123456789"]
    assert len(hits) == 1
    assert hits[0].embed_type == "blockquote"
    assert hits[0].ref.author == "@example_user"


def test_links_inside_embed_are_not_separate_hits():
    """The quoted tweet linked inside the blockquote text is not a separate occurrence."""
    keys = {h.key for h in detect(fixture("x.html"))}
    assert "x:1111111111111111111" not in keys


def test_links_are_never_real_embeds():
    """A plain link is typed 'link'."""
    for h in detect(fixture("sidebar_noise.html")):
        assert h.embed_type == "link"
        assert h.embed_type not in EMBED_MARKUP_TYPES


def test_amp_entity_double_escaped_iframe():
    """&amp;amp; in a plugin iframe still yields the permalink.php post."""
    html = ('<article><iframe src="https://www.facebook.com/plugins/post.php?href=https%3A%2F%2Fwww.facebook.com'
            '%2Fpermalink.php%3Fstory_fbid%3D123456789%26amp%3Bamp%3Bid%3D42&amp;amp;show_text=true"></iframe></article>')
    keys = {h.key for h in detect(html)}
    assert keys == {"facebook:post:123456789"}


def test_lazy_iframe_data_src():
    html = '<article><iframe data-src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe></article>'
    assert [(h.key, h.embed_type) for h in detect(html)] == [("youtube:dQw4w9WgXcQ", "iframe")]


def test_no_article_falls_back_to_body():
    html = '<html><body><div><blockquote class="twitter-tweet"><a href="https://x.com/u/status/77777">x</a></blockquote></div></body></html>'
    assert [h.key for h in detect(html)] == ["x:77777"]


def test_empty_and_garbage_input():
    assert detect("") == []
    assert detect("<<<>>> not html") == []


def test_escaped_url_only_script_is_scanned():
    """Regression: the '//' pre-check ran before unescaping, skipping JSON-only scripts."""
    html = '<html><body><script>{"u":"https:\\/\\/x.com\\/u\\/status\\/31337"}</script></body></html>'
    assert [(h.key, h.embed_type) for h in detect(html)] == [("x:31337", "script-data")]


# --- page furniture: teaser <article>s and related boxes (Dagbladet-like pages) -----------

DID_MAIN, DID_TEASE = "did:plc:mainmainmainmainmainmain", "did:plc:teaseteaseteaseteaseteas"


def _bsky(did, rkey):
    return (f'<blockquote class="bluesky-embed" data-bluesky-uri="at://{did}/app.bsky.feed.post/{rkey}">'
            f'<a href="https://bsky.app/profile/{did}/post/{rkey}">p</a></blockquote>')


def test_only_the_main_article_is_scanned_on_teaser_heavy_pages():
    body = "Lorem ipsum dolor sit amet. " * 40
    html = f"""<html><body>
      <article class="preview xavier-preview" data-cxence_widget="free"><h2>Teaser</h2>{_bsky(DID_TEASE, "t1")}</article>
      <main><article class="bg-custom article-entity"><h1>Headline</h1><p>{body}</p>{_bsky(DID_MAIN, "m1")}
        <article class="preview-link"><a href="/other">Other story</a>{_bsky(DID_TEASE, "t2")}</article>
        <div class="related-articles">{_bsky(DID_TEASE, "t3")}</div>
        <aside>{_bsky(DID_TEASE, "t4")}</aside>
        <div class="embed-widget"><blockquote class="twitter-tweet"><a href="https://twitter.com/u/status/42">t</a></blockquote></div>
      </article></main>
      <article class="column preview">{_bsky(DID_TEASE, "t5")}</article>
    </body></html>"""
    keys = {h.key for h in detect(html)}
    assert keys == {f"bluesky:at://{DID_MAIN}/app.bsky.feed.post/m1", "x:42"}


def test_comparably_large_articles_are_all_scanned():
    body = "Lorem ipsum dolor sit amet. " * 40
    html = (f"<html><body><article><h1>One</h1><p>{body}</p>{_bsky(DID_MAIN, 'a')}</article>"
            f"<article><h2>Two</h2><p>{body}</p>{_bsky(DID_TEASE, 'b')}</article></body></html>")
    assert len(detect(html)) == 2


def test_main_article_chosen_by_h1_then_by_size():
    main = f"<article><h1>Title</h1><p>{'word ' * 100}</p>{_bsky(DID_MAIN, 'h')}</article>"
    card = f"<article><p>{'word ' * 30}</p>{_bsky(DID_TEASE, 'card')}</article>"   # small, no teaser class
    assert [h.ref.canonical_id[-1:] for h in detect(f"<html><body>{card}{main}</body></html>")] == ["h"]
    no_h1 = main.replace("h1", "h2")
    assert [h.ref.canonical_id[-1:] for h in detect(f"<html><body>{card}{no_h1}</body></html>")] == ["h"]
    big_card = card.replace("'word ' * 30", "").replace("word " * 30, "word " * 300)
    assert len(detect(f"<html><body>{big_card}{main}</body></html>")) == 2   # comparably large: scanned


def test_script_data_is_still_found_page_wide():
    html = ('<html><body><article class="article-entity"><h1>T</h1></article>'
            '<article class="preview"><script>var x="https:\\/\\/x.com\\/u\\/status\\/99"</script></article>'
            '</body></html>')
    assert [(h.key, h.embed_type) for h in detect(html)] == [("x:99", "script-data")]


@pytest.mark.parametrize("name", ["dagbladet-like-a1.html", "dagbladet-like-a2.html"])
def test_real_teaser_heavy_page_structure(name):
    from bs4 import BeautifulSoup

    from media_embed_tracer.detect import _content_roots

    soup = BeautifulSoup(fixture(name), "html.parser")
    assert len(soup.find_all("article")) >= 18
    roots = _content_roots(soup)
    assert len(roots) == 1 and "article-entity" in roots[0].get("class")
    assert roots[0].find("article") is None and roots[0].find("h1") is not None   # nested teasers removed
    detect(fixture(name))   # no crash
