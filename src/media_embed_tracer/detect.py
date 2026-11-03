"""Embed detection: one pass over an article's HTML, all platforms at once.

What counts as what (see models.EMBED_TYPES):
  * blockquote / widget: element carrying a platform's official embed class
    (blockquote.twitter-tweet, div.fb-post, ...) or a known placeholder attribute
    (script[data-telegram-post], lite-youtube[videoid], amp-twitter[data-tweetid], data-* URLs)
  * iframe: <iframe>/<amp-iframe> whose src (or lazy-load data-src) is a platform URL
  * script-data: URL inside inline <script> text, including JSON-escaped (https:\\/\\/...).
    Scanned page-wide; frameworks put plain links there too, so it is never quoted.
  * link: plain <a href>; anchors inside a recognised embed are not counted again

Markup is scanned inside the article body only (see _content_roots): the main <article>
(else <main>, else common content containers, else <body>), without page furniture inside
it (nested <article> teasers, aside/nav/footer, teaser/related/recommendation boxes).
News pages often carry many <article> teaser cards; a post embedded in one of those must
not be attributed to every page that shows the teaser. Inline scripts are scanned
page-wide (script-data is never quoted).
Per article each post key is reported once, with the strongest embed type.
Detection is pure: no network. Handle -> DID and short-link expansion happen in resolve.py.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from .models import EMBED_PRIORITY, Hit, PostRef
from .platforms import CLASS_TO_PLATFORM, match_url
from .platforms import telegram as tg
from .platforms import x as xp
from .platforms import youtube as yt
from .util import clean_url

_CONTENT_SELECTORS = (
    ".article-body", ".article-content", ".article__body", ".article__content", ".story-body",
    ".entry-content", ".post-content", ".content-body", ".c-entry-content",
    "#article-body", "#story-body", "#main-content",
)
_IFRAME_ATTRS = ("src", "data-src", "data-lazy-src", "data-url")
# Attributes checked first on an embed container, before its inner links.
_CONTAINER_ATTRS = ("data-bluesky-uri", "data-bluesky-aturi", "data-instgrm-permalink",
                    "data-text-post-permalink", "data-embed-url", "data-href", "cite", "data-url",
                    "data-src")
# Page furniture inside the article root, removed before scanning. Class/id tokens (split on
# space, "-" and "_") that mark teaser, related-content and recommendation boxes. Kept small:
# "widget" and "embed" are deliberately absent, because CMSs wrap real embeds in them.
_FURNITURE_TAGS = ("article", "aside", "nav", "footer")
_FURNITURE_TOKENS = frozenset({"teaser", "teasers", "related", "recommended", "recommendation",
                               "recommendations", "preview", "promo", "xavier", "outbrain", "taboola",
                               "mostread", "readmore"})
# Recommendation-engine attributes (e.g. data-cxense_widget on teaser cards).
_FURNITURE_ATTRS = ("data-cxense_widget", "data-cxence_widget", "data-rv-part")
_TOKEN_SPLIT = re.compile(r"[\s_-]+")
# Other top-level <article>s are scanned only when this large relative to the main one
# (genuine multi-article pages); teaser cards are far smaller.
_COMPARABLE_SHARE = 0.5
_URL_IN_TEXT = re.compile(r"""(?:https?:)?//[^\s"'<>\\`]+|at://did:[^\s"'<>\\`]+""")


def _text_len(el: Tag) -> int:
    return len(el.get_text(" ", strip=True))


def _is_furniture(el: Tag) -> bool:
    if el.name in _FURNITURE_TAGS:
        return True
    classes = el.get("class") or []
    if any(c in CLASS_TO_PLATFORM for c in classes):
        return False   # a real embed container is never furniture
    if any(el.has_attr(a) for a in _FURNITURE_ATTRS):
        return True
    words = " ".join([*classes, str(el.get("id") or "")]).lower()
    return bool(words) and any(t in _FURNITURE_TOKENS for t in _TOKEN_SPLIT.split(words))


def _main_article(articles: list[Tag], soup: BeautifulSoup) -> Tag:
    """The page's own article: marked as the article body, else the one holding the <h1>,
    else the one with the most text. Teaser-like articles are only chosen as a last resort."""
    def marked(a: Tag) -> bool:
        cls = " ".join(a.get("class") or []).lower()
        return a.get("itemprop") == "articleBody" or a.find(attrs={"itemprop": "articleBody"}) is not None \
            or "article-entity" in cls
    plain = [a for a in articles if not _is_furniture_root(a)] or articles
    for a in plain:
        if marked(a):
            return a
    h1 = soup.find("h1")
    if h1 is not None:
        for a in plain:
            if h1 in a.descendants:
                return a
    return max(plain, key=_text_len)


def _is_furniture_root(el: Tag) -> bool:
    """For <article> candidates: teaser classes/attributes (the tag itself is not counted)."""
    saved, el.name = el.name, "div"
    try:
        return _is_furniture(el)
    finally:
        el.name = saved


def _content_roots(soup: BeautifulSoup) -> list[Tag]:
    articles: list[Tag] = soup.find_all("article")
    top = [a for a in articles if not any(p.name == "article" for p in a.parents)]
    roots: list[Tag] = []
    # Teaser-like <article>s alone do not outrank <main> or a marked content container;
    # they are only used when neither exists.
    if top and all(_is_furniture_root(a) for a in top) and (
            soup.find("main") or any(soup.select_one(sel) for sel in _CONTENT_SELECTORS)):
        top = []
    if top:
        main = _main_article(top, soup)
        size = _text_len(main)
        roots = [main] + [a for a in top if a is not main and not _is_furniture_root(a)
                          and size and _text_len(a) >= _COMPARABLE_SHARE * size]
    if not roots:
        main = soup.find("main")
        roots = [main] if main else []
    if not roots:
        for sel in _CONTENT_SELECTORS:
            found = soup.select_one(sel)
            if found:
                roots = [found]
                break
    if not roots:
        roots = [soup.body or soup]
    for root in roots:
        # Remove page furniture inside the root (outermost first; decomposed children are skipped).
        for el in root.find_all(True):
            if el.parent is not None and el.decomposed is False and _is_furniture(el):
                el.decompose()
    return roots


def unescape_script_text(text: str) -> str:
    return (text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
            .replace("\\u0026", "&").replace("\\u003D", "=").replace("\\u003d", "="))


class _Collector:
    def __init__(self) -> None:
        self.hits: dict[str, Hit] = {}

    def add(self, ref: PostRef | None, embed_type: str) -> bool:
        if ref is None:
            return False
        prev = self.hits.get(ref.key)
        if prev is None or EMBED_PRIORITY[embed_type] > EMBED_PRIORITY[prev.embed_type]:
            # Keep the richer author/post_url if the stronger hit lacks one.
            if prev and not ref.author and prev.ref.author and not ref.unresolved:
                ref = PostRef(ref.platform, ref.canonical_id, prev.ref.post_url, prev.ref.author)
            self.hits[ref.key] = Hit(ref, embed_type)
        return True


def _container_candidates(el: Tag) -> list[str]:
    urls = [el.get(a) for a in _CONTAINER_ATTRS if el.get(a)]
    vid = el.get("data-video-id")  # legacy tiktok-embed
    if vid and str(vid).isdigit():
        urls.append(f"https://www.tiktok.com/embed/v2/{vid}")
    tweet_id = el.get("data-tweet-id")
    if tweet_id and str(tweet_id).isdigit():
        urls.append(f"https://x.com/i/status/{tweet_id}")
    # Permalink is the last link in twitter/bluesky blockquotes: check inner links last-first.
    urls.extend(a.get("href") for a in reversed(el.find_all("a", href=True)))
    return [clean_url(str(u)) for u in urls if u]


def _special(el: Tag) -> PostRef | None:
    """Placeholders whose attribute holds an id rather than a URL."""
    name = el.name
    if el.get("data-telegram-post"):
        return tg.from_widget_value(str(el["data-telegram-post"]))
    if name == "lite-youtube" and el.get("videoid"):
        return yt.match(f"https://www.youtube.com/embed/{el['videoid']}")
    # AMP components (attribute names from AMP component docs; not re-verified here)
    if name == "amp-twitter" and str(el.get("data-tweetid", "")).isdigit():
        return xp.make(str(el["data-tweetid"]))
    if name == "amp-youtube" and el.get("data-videoid"):
        return yt.match(f"https://www.youtube.com/embed/{el['data-videoid']}")
    if name == "amp-instagram" and el.get("data-shortcode"):
        return match_url(f"https://www.instagram.com/p/{el['data-shortcode']}/")
    if el.get("data-tweet-id") and str(el["data-tweet-id"]).isdigit():
        return xp.make(str(el["data-tweet-id"]))
    return None


def detect(html: str) -> list[Hit]:
    soup = BeautifulSoup(html or "", "html.parser")
    out = _Collector()

    # Inline scripts first (page-wide), before furniture is removed from the article root.
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        text = unescape_script_text(script.string or script.get_text() or "")
        if "//" not in text:  # checked after unescaping: https:\/\/ has no plain "//"
            continue
        for m in _URL_IN_TEXT.finditer(text):
            out.add(match_url(clean_url(m.group(0))), "script-data")

    for root in _content_roots(soup):
        claimed: set[int] = set()  # descendants of recognised embeds
        for el in root.find_all(True):
            if id(el) in claimed:
                continue
            name = el.name
            classes = el.get("class") or []
            platform = next((CLASS_TO_PLATFORM[c] for c in classes if c in CLASS_TO_PLATFORM), None)
            found = False

            if platform is not None:
                etype = "blockquote" if name == "blockquote" else "widget"
                for url in _container_candidates(el):
                    ref = platform.match(url)
                    if ref and out.add(ref, etype):
                        found = True
                        break
            if not found and name in ("iframe", "amp-iframe"):
                for attr in _IFRAME_ATTRS:
                    if el.get(attr) and out.add(match_url(clean_url(str(el[attr]))), "iframe"):
                        found = True
                        break
            if not found:
                found = out.add(_special(el), "widget")
            if not found and name not in ("a", "iframe", "amp-iframe", "img", "source", "link"):
                for attr, value in el.attrs.items():
                    if attr.startswith("data-") and isinstance(value, str) and (
                            "//" in value or value.startswith("at://")):
                        if out.add(match_url(clean_url(value)), "widget"):
                            break
                # A bare data-* URL does not claim its descendants: the element may be a
                # wrapper around a different, real embed.
            if found:
                claimed.update(id(d) for d in el.descendants)
                continue
            if name == "a" and el.get("href"):
                out.add(match_url(clean_url(str(el["href"]))), "link")

    return sorted(out.hits.values(), key=lambda h: h.key)
