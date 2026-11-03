"""Quote text: 300-grapheme / 3000-byte limits, facet byte offsets, langs."""

import pytest

from media_embed_tracer.quote import (
    MAX_GRAPHEMES,
    build_quote_text,
    build_record,
    display_url,
    graphemes,
    parse_langs,
    validate_template,
)

URL = "https://www.example.com/news/2026/03/01/a-very-long-article-slug-that-goes-on-and-on"


def facet_text(q):
    f = q.facets[0]["index"]
    return q.text.encode("utf-8")[f["byteStart"]:f["byteEnd"]].decode("utf-8")


def test_short_text_untouched():
    q = build_quote_text("Quoted by {outlet} → {title}", "Example News", "Short title", URL)
    assert q.text.startswith("Quoted by Example News → Short title ")
    assert facet_text(q) == URL
    assert q.facets[0]["features"][0]["uri"] == URL


@pytest.mark.parametrize("title", [
    "A" * 1000,
    "Ærlig talt – øl på Ålesund 🍺 " * 40,
    "👩‍👩‍👧‍👦" * 400,               # multi-codepoint grapheme clusters
    "é" * 500,                 # combining accents
])
def test_long_titles_fit_limits(title):
    q = build_quote_text("Quoted by {outlet} → {title}", "Outlet", title, URL)
    assert graphemes(q.text) <= MAX_GRAPHEMES
    assert len(q.text.encode()) <= 3000
    assert q.text.endswith(URL)
    assert facet_text(q) == URL


def test_facet_offsets_are_bytes_not_chars():
    q = build_quote_text("{outlet} → {title}", "Ño", "Émoji 🎉 title", "https://ex.com/a")
    start = q.facets[0]["index"]["byteStart"]
    assert start == len("Ño → Émoji 🎉 title ".encode())
    assert start != len("Ño → Émoji 🎉 title ")
    assert facet_text(q) == "https://ex.com/a"


def test_facet_not_fooled_by_url_in_title():
    """Old code used text.find(url): a title containing the URL put the facet in the wrong place."""
    q = build_quote_text("{title}", "", "ex.com/a is the story", "https://ex.com/a")
    assert q.facets[0]["index"]["byteStart"] == len(b"ex.com/a is the story ")


def test_display_url_shortened():
    assert display_url("https://www.example.com/") == "example.com"
    assert len(display_url(URL)) == 40 and display_url(URL).endswith("…")


def test_template_validation():
    validate_template("{outlet} {title}")
    with pytest.raises(ValueError):
        validate_template("{outlet} {secret}")


def test_langs_and_record():
    assert parse_langs("en, nb ,de,fr") == ["en", "nb", "de"]
    q = build_quote_text("{title}", "", "t", "https://ex.com")
    rec = build_record(q, "at://did:plc:x/app.bsky.feed.post/1", "cid1", ["nb"], "2026-01-01T00:00:00Z")
    assert rec["langs"] == ["nb"]
    assert rec["embed"] == {"$type": "app.bsky.embed.record",
                            "record": {"uri": "at://did:plc:x/app.bsky.feed.post/1", "cid": "cid1"}}
    assert "langs" not in build_record(q, "u", "c", [], "t")


def test_v1_format_full_url():
    q = build_quote_text("Quoted by {outlet} → {title}", "Engadget", "A title", "https://www.engadget.com/a/b.html")
    assert q.text == "Quoted by Engadget → A title https://www.engadget.com/a/b.html"
    assert facet_text(q) == "https://www.engadget.com/a/b.html"


def test_absurdly_long_url_falls_back_to_short_display():
    long_url = "https://example.com/" + "x" * 400
    q = build_quote_text("Quoted by {outlet} → {title}", "Outlet", "T", long_url)
    assert graphemes(q.text) <= 300
    assert q.facets[0]["features"][0]["uri"] == long_url
