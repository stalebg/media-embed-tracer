"""CSV/JSON export of the JSONL data dir (run weekly by the export workflow, or locally).

* occurrences.csv, articles.csv, posts.csv, summary.json
* Duplicates are removed: occurrences on (article_url, platform, canonical_id), keeping
  the earliest first_seen; article checks on (url, check_n), keeping the earliest. They
  can appear if the state push fails after the data push succeeded (the next run then
  re-checks articles and appends the same rows again).
* --resolve-handles adds a verified Bluesky handle per DID to posts.csv (network)
* Free-text cells (titles, outlet, feed, error) starting with = + - @ or tab/CR get a
  leading apostrophe, so opening the file in a spreadsheet app never evaluates them as
  formulas. Structured columns (ids, URLs, @handles) are left untouched.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from pathlib import Path

from .models import ArticleCheck, Occurrence, Post
from .platforms import bluesky
from .store.jsonl import JsonlStore, atomic_write

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


_FREE_TEXT = {"article_title", "title", "outlet", "feed", "error", "last_error", "via_title", "via_outlet", "summary",
              "article_summary"}


def _csv(rows: list[list], headers: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(headers)
    free = [h in _FREE_TEXT for h in headers]
    for r in rows:
        w.writerow([safe_cell(v) if f else ("" if v is None else v) for v, f in zip(r, free, strict=False)])
    return buf.getvalue()


def dedupe_occurrences(rows: list[Occurrence]) -> list[Occurrence]:
    best: dict[tuple, Occurrence] = {}
    for o in rows:
        k = (o.article_url, o.platform, o.canonical_id)
        if k not in best or o.first_seen < best[k].first_seen:
            best[k] = o
    return sorted(best.values(), key=lambda o: (o.first_seen, o.article_url, o.platform, o.canonical_id))


def dedupe_articles(rows: list[ArticleCheck]) -> list[ArticleCheck]:
    best: dict[tuple, ArticleCheck] = {}
    for a in rows:
        k = (a.url, a.check_n)
        if k not in best or a.checked_at < best[k].checked_at:
            best[k] = a
    return sorted(best.values(), key=lambda a: (a.checked_at, a.url, a.check_n))


def export(store: JsonlStore, out_dir: str | Path, resolver=None) -> dict:
    out = Path(out_dir)
    state = store.load()
    raw_occurrences = store.read_occurrences()
    raw_articles = store.read_articles()
    occurrences = dedupe_occurrences(raw_occurrences)
    articles = dedupe_articles(raw_articles)
    posts = [state.posts[k] for k in sorted(state.posts)]
    post_headers = Post.field_names()
    post_rows = [p.to_row() for p in posts]
    if resolver is not None:
        post_headers = [*post_headers, "author_handle"]
        for p, row in zip(posts, post_rows, strict=True):
            handle = ""
            if p.platform == bluesky.NAME and bluesky.is_did(p.author):
                handle = resolver.verified_handle(p.author)
            row.append(f"@{handle}" if handle else "")
    atomic_write(out / "occurrences.csv", _csv([o.to_row() for o in occurrences], Occurrence.field_names()))
    atomic_write(out / "articles.csv", _csv([a.to_row() for a in articles], ArticleCheck.field_names()))
    atomic_write(out / "posts.csv", _csv(post_rows, post_headers))
    first_checks = [a for a in articles if a.check_n == 1 and a.fetch_ok]
    summary = {
        "occurrences": len(occurrences),
        "duplicates_removed": {"occurrences": len(raw_occurrences) - len(occurrences),
                               "article_checks": len(raw_articles) - len(articles)},
        "posts": len(posts),
        "article_checks": len(articles),
        "articles_fetched_ok_first_check": len(first_checks),
        "articles_with_embed_markup_first_check": sum(
            1 for a in first_checks if any(a.embed_types.get(t) for t in ("blockquote", "iframe", "widget"))),
        "by_platform": dict(Counter(o.platform for o in occurrences).most_common()),
        "by_embed_type": dict(Counter(o.embed_type for o in occurrences).most_common()),
        "by_outlet": dict(Counter(o.outlet for o in occurrences).most_common()),
        "repost_status": dict(Counter(p.repost_status for p in posts).most_common()),
        "detector_versions": dict(Counter(o.detector_version for o in occurrences).most_common()),
        "first_seen_min": min((o.first_seen for o in occurrences), default=""),
        "first_seen_max": max((o.first_seen for o in occurrences), default=""),
    }
    atomic_write(out / "summary.json", json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    return summary
