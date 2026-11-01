"""Command line entry point: python -m media_embed_tracer [run|detect|export|migrate-v1]."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__, redact
from .config import ConfigError, from_env
from .feeds import FeedConfigError, load_feeds
from .http import Http
from .mode import SHEETS, storage_mode
from .pipeline import EXIT_CONFIG, EXIT_STORE, run
from .resolve import OfflineResolver, Resolver
from .store import JsonlStore, StoreError

log = logging.getLogger("media_embed_tracer")


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    # LOG_REDACT=true (or DATA_REPO set, or sheets mode): scrub URLs, at:// URIs, DIDs and
    # handles from all output, because Actions logs of a public repository are public.
    redact.install(os.environ, getattr(logging, level, logging.INFO))


V1_IMPORT_LOOKUPS = 200   # handle lookups for the first-run import of v1 rows
V1_ROW_LOOKUPS = 20       # handle lookups per run for All Embeds rows (new rows' handles, dedup)


def build_sheets_store(cfg, http, **kwargs):
    """Sheets mode: open the spreadsheet as the primary store. Raises StoreError if it
    cannot be opened, so the run stops before anything is scraped or posted. INSTANCE_NAME
    (set by each workflow) is recorded in the sheet, so two instances never share one."""
    from .store.sheets import open_spreadsheet
    from .store.sheets_store import SheetsStore
    kwargs.setdefault("deadline", time.monotonic() + cfg.max_run_minutes * 60)   # no lookups past it
    return SheetsStore(open_spreadsheet(cfg.spreadsheet_url, cfg.google_credentials_json),
                       cfg.seen_retention_days, instance=os.environ.get("INSTANCE_NAME", ""),
                       resolver=Resolver(http, cfg.bluesky_appview, max_lookups=V1_IMPORT_LOOKUPS),
                       v1_resolver=Resolver(http, cfg.bluesky_appview, max_lookups=V1_ROW_LOOKUPS), **kwargs)


def build_mirrors(cfg) -> list:
    if not cfg.spreadsheet_url or cfg.storage_mode == SHEETS:
        return []
    from .store.sheets import SheetsMirror, open_spreadsheet
    return [SheetsMirror(open_spreadsheet(cfg.spreadsheet_url, cfg.google_credentials_json))]


class _BrokenMirror:
    name = "sheets"

    def __init__(self, error: StoreError) -> None:
        self.error = error

    def write(self, **_kwargs) -> None:
        raise StoreError(str(self.error), safe=getattr(self.error, "log_safe", False))


def cmd_run(_args) -> int:
    try:
        cfg = from_env()
        feedset = load_feeds(cfg.feeds_json, cfg.feeds_file, cfg.feed_names_json)
    except (ConfigError, FeedConfigError) as exc:
        log.error("configuration error: %s", str(exc))  # own config: pattern-scrubbed only
        return EXIT_CONFIG
    http = Http(cfg.request_timeout, cfg.user_agent)
    if cfg.storage_mode == SHEETS:
        try:
            store = build_sheets_store(cfg, http)
        except StoreError as exc:
            log.error("sheets store unavailable, nothing scraped or posted: %s", exc)
            return EXIT_STORE
        except ImportError:
            log.error("Google Sheets support needs: pip install '.[sheets]'")
            return EXIT_CONFIG
    else:
        store = JsonlStore(cfg.data_dir, cfg.state_dir, cfg.seen_retention_days)
    try:
        mirrors = build_mirrors(cfg)
    except StoreError as exc:  # the run still happens; the mirror failure turns it red
        log.error("Sheets mirror unavailable: %s", exc)
        mirrors = [_BrokenMirror(exc)]
    except ImportError:
        log.error("Google Sheets support needs: pip install '.[sheets]'")
        return EXIT_CONFIG
    resolver = Resolver(http, cfg.bluesky_appview)
    client = None
    if cfg.posting_enabled:
        from .bsky import BlueskyClient
        client = BlueskyClient(http.session, cfg.bluesky_service, cfg.request_timeout)
    log.info("media-embed-tracer %s: %d feeds, store=%s, mirrors=%s, posting=%s", __version__,
             len(feedset.feeds), store.name, [m.name for m in mirrors], "on" if cfg.posting_enabled else "off")
    code, _stats = run(cfg, feedset, store, http, resolver, client, mirrors)
    return code


def cmd_detect(args) -> int:
    from .detect import detect

    target = args.target
    if Path(target).is_file():
        html = Path(target).read_text(encoding="utf-8", errors="replace")
    else:
        res = Http(20).fetch_html(target)
        if not res.ok:
            print(f"fetch failed: {res.error}", file=sys.stderr)
            return 1
        html = res.text
    resolver = OfflineResolver() if args.offline else Resolver(Http(10))
    for hit in detect(html):
        hit = resolver.canonicalize(hit)
        print(json.dumps({"platform": hit.ref.platform, "canonical_id": hit.ref.canonical_id,
                          "post_url": hit.ref.post_url, "author": hit.ref.author,
                          "embed_type": hit.embed_type}, ensure_ascii=False))
    return 0


def cmd_export(args) -> int:
    from .export import export

    data_dir = args.data_dir or os.environ.get("DATA_DIR") or "data"
    resolver = Resolver(Http(10), max_lookups=5000) if args.resolve_handles else None
    summary = export(JsonlStore(data_dir), args.out or "export", resolver)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def cmd_migrate(args) -> int:
    from .migrate import convert_v1
    from .store.sheets import open_spreadsheet

    if storage_mode(os.environ) == SHEETS:
        log.info("sheets mode imports the v1 tab automatically on its first run; nothing to do")
        return 0

    creds = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    source = args.spreadsheet or os.environ.get("V1_SPREADSHEET_URL", "")
    if not source or not creds:
        log.error("need --spreadsheet (or V1_SPREADSHEET_URL) and GOOGLE_CREDENTIALS_JSON")
        return EXIT_CONFIG
    try:
        ss = open_spreadsheet(source, creds)
        rows = ss.worksheet(args.tab).get_all_values()
    except Exception as exc:
        log.error("cannot read v1 sheet: %s", exc)
        return EXIT_STORE
    data_dir = args.data_dir or os.environ.get("DATA_DIR") or "data"
    target = JsonlStore(data_dir, args.state_dir or os.environ.get("STATE_DIR") or None)
    state = target.load()
    result = convert_v1(rows, Resolver(Http(10), max_lookups=5000) if args.resolve else None, state.posts)
    target.save(occurrences=result.occurrences, articles=[], posts=result.posts, seen=result.seen, health=[],
                queue=state.queue)
    print(f"migrated {len(result.occurrences)} occurrences, {len(result.posts)} new posts, "
          f"{len(result.seen)} articles; skipped {result.skipped_rows} unmatchable rows")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-embed-tracer", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="one scrape run, configured by environment variables (default)")
    p = sub.add_parser("detect", help="print embeds found in one URL or local HTML file")
    p.add_argument("target")
    p.add_argument("--offline", action="store_true", help="no handle/short-link lookups")
    p = sub.add_parser("export", help="write CSV + summary.json from the JSONL data dir")
    p.add_argument("--data-dir")
    p.add_argument("--out", help="output directory (default: export)")
    p.add_argument("--resolve-handles", action="store_true", help="add verified Bluesky handles (network)")
    p = sub.add_parser("migrate-v1", help="import the v1 'All Embeds' sheet into the JSONL store (sheets mode "
                       "imports it automatically)")
    p.add_argument("--spreadsheet", help="v1 spreadsheet URL or id")
    p.add_argument("--tab", default="All Embeds")
    p.add_argument("--data-dir")
    p.add_argument("--state-dir")
    p.add_argument("--resolve", action="store_true", help="resolve Bluesky handles / TikTok short links")
    args = parser.parse_args(argv)
    _setup_logging()
    handlers = {"run": cmd_run, "detect": cmd_detect, "export": cmd_export, "migrate-v1": cmd_migrate}
    return handlers[args.command or "run"](args)
