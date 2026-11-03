"""Quote-post text and link facet for Bluesky (pure, no I/O).

Limits (verified, app.bsky.feed.post lexicon): text <= 300 graphemes and <= 3000 bytes;
langs <= 3 entries. The article URL is shown shortened (like Bluesky clients do) and
carried in full by a link facet whose offsets are UTF-8 byte offsets.
"""

from __future__ import annotations

import string
from dataclasses import dataclass

import regex

MAX_GRAPHEMES = 300
MAX_BYTES = 3000
MAX_LANGS = 3
DEFAULT_TEMPLATE = "Quoted by {outlet} → {title}"
_ALLOWED_FIELDS = {"outlet", "title"}


def graphemes(text: str) -> int:
    return len(regex.findall(r"\X", text))


def truncate_graphemes(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    clusters = regex.findall(r"\X", text)
    if len(clusters) <= limit:
        return text
    if limit == 1:
        return "…"
    return "".join(clusters[: limit - 1]).rstrip() + "…"


def validate_template(template: str) -> None:
    fields = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
    unknown = fields - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"QUOTE_TEMPLATE may only use {{outlet}} and {{title}}, not {sorted(unknown)}")


def display_url(url: str, max_len: int = 40) -> str:
    shown = url.split("://", 1)[-1]
    if shown.startswith("www."):
        shown = shown[4:]
    shown = shown.rstrip("/")
    return shown if len(shown) <= max_len else shown[: max_len - 1] + "…"


def parse_langs(value: str) -> list[str]:
    langs = [x.strip() for x in (value or "").split(",") if x.strip()]
    return langs[:MAX_LANGS]


@dataclass
class QuoteText:
    text: str
    facets: list[dict]


def build_quote_text(template: str, outlet: str, title: str, article_url: str) -> QuoteText:
    validate_template(template)
    shown = display_url(article_url)
    suffix = " " + shown

    def body(t: str, o: str = outlet) -> str:
        return template.format(outlet=o, title=t).strip()

    def fits(b: str) -> bool:
        return graphemes(b + suffix) <= MAX_GRAPHEMES and len((b + suffix).encode()) <= MAX_BYTES

    text_body = body(title)
    if not fits(text_body):
        room = MAX_GRAPHEMES - graphemes(body("") + suffix)
        text_body = body(truncate_graphemes(title, room))
        while not fits(text_body) and room > 0:  # byte limit (e.g. long emoji sequences)
            room = max(0, room - max(1, room // 10))
            text_body = body(truncate_graphemes(title, room))
        room = MAX_GRAPHEMES - graphemes(suffix)
        while not fits(text_body) and room > 0:  # absurdly long outlet/template
            text_body = truncate_graphemes(text_body, room)
            room -= max(1, room // 10)
    text = text_body + suffix
    start = len((text_body + " ").encode("utf-8"))
    end = start + len(shown.encode("utf-8"))
    facets = [{
        "index": {"byteStart": start, "byteEnd": end},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": article_url}],
    }]
    assert graphemes(text) <= MAX_GRAPHEMES and len(text.encode()) <= MAX_BYTES
    return QuoteText(text, facets)


def build_record(quote: QuoteText, subject_uri: str, subject_cid: str, langs: list[str],
                 created_at: str) -> dict:
    record = {
        "$type": "app.bsky.feed.post",
        "text": quote.text,
        "facets": quote.facets,
        "createdAt": created_at,
        "embed": {"$type": "app.bsky.embed.record", "record": {"uri": subject_uri, "cid": subject_cid}},
    }
    if langs:
        record["langs"] = langs[:MAX_LANGS]
    return record
