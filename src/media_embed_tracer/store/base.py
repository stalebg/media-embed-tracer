"""Store interfaces.

The pipeline talks to one primary store through the `Store` protocol:
  JsonlStore (`store/jsonl.py`), private and public modes:
    data  (append-only, monthly partitions + posts.jsonl)  -> `data` branch, fast-forward only
    state (seen articles, feed health, resolve queue)      -> `state` branch, single commit
  SheetsStore (`store/sheets_store.py`), sheets mode: state and dataset in the Google Sheet.
Mirrors (`SheetsMirror` in `store/sheets.py`, JSONL modes only) receive one write at the end
of each run.

All methods raise StoreError on failure; the pipeline maps that to exit code 2.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from ..models import ArticleCheck, FeedHealth, Occurrence, Post, QueuedHit, RunLog, SeenArticle, StoreState
from ..util import parse_iso


class StoreError(RuntimeError):
    """A store failure. ``safe=True`` marks a message this package built from fixed text, tab
    names and exception class names only (never URLs, handles, DIDs, titles, cell values or
    library exception text): redact mode logs it in full. Otherwise only the class name."""

    def __init__(self, message: str = "", *, safe: bool = False) -> None:
        super().__init__(message)
        self.log_safe = safe


def seen_expired(seen: SeenArticle, cutoff: datetime) -> bool:
    """True when a seen article was last checked (or first seen) before the retention cutoff."""
    return (parse_iso(seen.last_checked) or parse_iso(seen.first_seen) or cutoff) < cutoff


class Store(Protocol):
    name: str

    def load(self) -> StoreState: ...

    def save(self, *, occurrences: Iterable[Occurrence], articles: Iterable[ArticleCheck],
             posts: Iterable[Post], seen: Iterable[SeenArticle], health: Iterable[FeedHealth],
             queue: list[QueuedHit]) -> None:
        """Append occurrences/articles, upsert posts/seen/health, replace the queue."""
        ...

    def update_post(self, post: Post) -> None:
        """Persist one post's repost state after a Bluesky action. JsonlStore writes at once;
        SheetsStore writes at once after a quote and batches the rest until `append_run`."""
        ...

    def append_run(self, run: RunLog) -> None:
        """Append the run log row (the last write of a run; flushes anything still buffered)."""
        ...


class Mirror(Protocol):
    name: str

    def write(self, *, occurrences: list[Occurrence], articles: list[ArticleCheck], posts: list[Post],
              health: list[FeedHealth], run: RunLog) -> None: ...
