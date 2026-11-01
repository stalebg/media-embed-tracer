"""JSONL store for git scraping.

data dir (commit to the `data` branch; files only ever grow, except posts.jsonl):
  occurrences/YYYY-MM.jsonl   one row per (article, post), by first_seen month
  articles/YYYY-MM.jsonl      one row per article check (the denominator), by checked_at month
  runs/YYYY-MM.jsonl          one row per run
  posts.jsonl                 one row per canonical post, sorted by key (repost state)
state dir (commit to the `state` branch; rewritten every run):
  seen_articles.jsonl         sorted by URL, rows older than the retention pruned
  feed_health.json
  resolve_queue.jsonl         hits waiting for a handle / short-link lookup
Writes go to a temp file and are renamed into place.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from ..models import ArticleCheck, FeedHealth, Occurrence, Post, QueuedHit, RunLog, SeenArticle, StoreState
from ..util import parse_iso, utcnow
from .base import StoreError, seen_expired

log = logging.getLogger(__name__)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def read_jsonl(path: Path) -> list[dict]:
    """Read one JSON object per line. Malformed lines (e.g. a line truncated by a killed
    run and then committed) are skipped with a warning instead of blocking every later run."""
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("%s:%d: skipping malformed JSONL line (%s)", path, n, exc)
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                log.warning("%s:%d: skipping non-object JSONL line", path, n)
    return rows


def append_lines(path: Path, lines: list[str]) -> None:
    """Append complete lines in one write + fsync. If the file ends in a partial line
    (an earlier interrupted append), start on a fresh line so new rows stay intact."""
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(line + "\n" for line in lines).encode("utf-8")
    with path.open("ab+") as fh:
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                payload = b"\n" + payload
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


def read_partitions(directory: Path) -> list[dict]:
    rows: list[dict] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.jsonl")):
            rows.extend(read_jsonl(path))
    return rows


def _month(ts: str) -> str:
    dt = parse_iso(ts) or utcnow()
    return dt.strftime("%Y-%m")


class JsonlStore:
    name = "jsonl"

    def __init__(self, data_dir: str | Path, state_dir: str | Path | None = None,
                 seen_retention_days: float = 30, now: datetime | None = None) -> None:
        self.data = Path(data_dir)
        self.state = Path(state_dir) if state_dir else self.data / "state"
        self.seen_retention = timedelta(days=seen_retention_days)
        self._now = now
        self._state: StoreState | None = None

    # --- reading -----------------------------------------------------------
    def load(self) -> StoreState:
        try:
            st = StoreState(
                posts={p.key: p for p in map(Post.from_dict, read_jsonl(self.data / "posts.jsonl"))},
                seen={s.url: s for s in map(SeenArticle.from_dict, read_jsonl(self.state / "seen_articles.jsonl"))},
                queue=[QueuedHit.from_dict(r) for r in read_jsonl(self.state / "resolve_queue.jsonl")],
            )
            hp = self.state / "feed_health.json"
            if hp.exists():
                for row in json.loads(hp.read_text(encoding="utf-8")):
                    h = FeedHealth.from_dict(row)
                    st.health[h.feed] = h
        except StoreError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise StoreError(f"cannot read JSONL store: {exc}") from exc
        self._state = st
        return st

    def _ensure(self) -> StoreState:
        return self._state if self._state is not None else self.load()

    def read_occurrences(self) -> list[Occurrence]:
        return [Occurrence.from_dict(r) for r in read_partitions(self.data / "occurrences")]

    def read_articles(self) -> list[ArticleCheck]:
        return [ArticleCheck.from_dict(r) for r in read_partitions(self.data / "articles")]

    # --- writing -----------------------------------------------------------
    def _append_partitioned(self, sub: str, rows: list[dict], ts_field: str) -> None:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            groups[_month(r.get(ts_field, ""))].append(r)
        for month, items in sorted(groups.items()):
            append_lines(self.data / sub / f"{month}.jsonl", [dumps(r) for r in items])

    def _write_posts(self, st: StoreState) -> None:
        atomic_write(self.data / "posts.jsonl", "".join(dumps(st.posts[k].to_dict()) + "\n" for k in sorted(st.posts)))

    def save(self, *, occurrences, articles, posts, seen, health, queue) -> None:
        try:
            st = self._ensure()
            self._append_partitioned("occurrences", [o.to_dict() for o in occurrences], "first_seen")
            self._append_partitioned("articles", [a.to_dict() for a in articles], "checked_at")
            for p in posts:
                st.posts[p.key] = p
            self._write_posts(st)
            for s in seen:
                st.seen[s.url] = s
            cutoff = (self._now or utcnow()) - self.seen_retention
            st.seen = {u: s for u, s in st.seen.items() if not seen_expired(s, cutoff)}
            atomic_write(self.state / "seen_articles.jsonl",
                         "".join(dumps(st.seen[u].to_dict()) + "\n" for u in sorted(st.seen)))
            for h in health:
                st.health[h.feed] = h
            atomic_write(self.state / "feed_health.json",
                         json.dumps([st.health[k].to_dict() for k in sorted(st.health)], ensure_ascii=False,
                                    indent=1) + "\n")
            st.queue = list(queue)
            atomic_write(self.state / "resolve_queue.jsonl", "".join(dumps(q.to_dict()) + "\n" for q in st.queue))
        except OSError as exc:
            raise StoreError(f"cannot write JSONL store: {exc}") from exc

    def update_post(self, post: Post) -> None:
        try:
            st = self._ensure()
            st.posts[post.key] = post
            self._write_posts(st)
        except OSError as exc:
            raise StoreError(f"cannot write posts.jsonl: {exc}") from exc

    def append_run(self, run: RunLog) -> None:
        try:
            self._append_partitioned("runs", [run.to_dict()], "run_at")
        except OSError as exc:
            raise StoreError(f"cannot write run log: {exc}") from exc
