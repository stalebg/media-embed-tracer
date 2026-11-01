"""Pick which articles to fetch this run.

* Articles already checked are skipped (seen-article state).
* New articles are taken round-robin across feeds (newest first within a feed), with a
  per-feed cap and a global cap, starting from a feed offset that rotates every run so
  no feed is starved when the global cap is hit.
* Each article is re-checked once ~RECHECK_AFTER_HOURS after its first check, because
  outlets often add embeds after publication. Rechecks may use up to half the budget
  first; new articles fill the rest; leftover budget goes to further rechecks.
* Articles whose fetch failed are retried on later runs, up to max_fetch_errors.
* Eligible articles left over by the caps are counted per feed (skipped_by_cap) so
  coverage gaps are visible in the run log and feed health.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .feeds import Entry
from .models import SeenArticle
from .util import parse_iso

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class Task:
    url: str
    kind: str                      # "new" | "retry" | "recheck"
    entry: Entry | None = None
    seen: SeenArticle | None = None


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)
    skipped_by_cap: dict[str, int] = field(default_factory=dict)  # feed -> new/retry articles not taken
    rechecks_deferred: int = 0


def plan(entries_by_feed: dict[str, Sequence[Entry]], seen: dict[str, SeenArticle], now: datetime, *,
         max_articles: int = 150, per_feed_cap: int = 25, recheck_after_hours: float = 24,
         max_age_hours: float = 168, max_fetch_errors: int = 3, rotation: int | None = None) -> Plan:
    result = Plan()
    due: list[SeenArticle] = []
    if recheck_after_hours > 0:
        cutoff = now - timedelta(hours=recheck_after_hours)
        due = [s for s in seen.values()
               if not s.done and s.checks == 1 and (parse_iso(s.last_checked) or now) <= cutoff]
        due.sort(key=lambda s: parse_iso(s.last_checked) or _EPOCH)
    first_rechecks = due[: max_articles // 2]
    result.tasks.extend(Task(s.url, "recheck", seen=s) for s in first_rechecks)
    taken: set[str] = {t.url for t in result.tasks}

    age_cutoff = now - timedelta(hours=max_age_hours)
    queues: list[tuple[str, int, list[Task]]] = []
    for feed_name, entries in entries_by_feed.items():
        queue: list[Task] = []
        ordered = sorted(entries, key=lambda e: (e.published is not None, e.published or _EPOCH), reverse=True)
        for e in ordered:
            if e.published and e.published < age_cutoff:
                continue
            s = seen.get(e.url)
            if s is None:
                queue.append(Task(e.url, "new", entry=e))
            elif s.checks == 0 and not s.done and s.fetch_errors < max_fetch_errors:
                queue.append(Task(e.url, "retry", entry=e, seen=s))
        cap = entries[0].feed.per_feed_cap if entries and entries[0].feed.per_feed_cap else per_feed_cap
        queues.append((feed_name, cap, queue))

    if queues:
        offset = (rotation if rotation is not None else int(now.timestamp() // 1800)) % len(queues)
        queues = queues[offset:] + queues[:offset]
    counts = [0] * len(queues)
    positions = [0] * len(queues)
    progress = True
    while len(result.tasks) < max_articles and progress:
        progress = False
        for i, (_name, cap, queue) in enumerate(queues):
            if len(result.tasks) >= max_articles:
                break
            while positions[i] < len(queue) and counts[i] < cap:
                task = queue[positions[i]]
                positions[i] += 1
                if task.url in taken:  # same article in two feeds
                    continue
                taken.add(task.url)
                result.tasks.append(task)
                counts[i] += 1
                progress = True
                break

    for name, _cap, queue in queues:
        left = sum(1 for t in queue if t.url not in taken)
        if left:
            result.skipped_by_cap[name] = left

    extra = due[len(first_rechecks):]
    room = max_articles - len(result.tasks)
    result.tasks.extend(Task(s.url, "recheck", seen=s) for s in extra[:room])
    result.rechecks_deferred = max(0, len(extra) - room)
    return result
