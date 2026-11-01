"""Configuration from environment variables (see README for the full table)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .mode import storage_mode
from .platforms.bluesky import is_did
from .quote import DEFAULT_TEMPLATE, parse_langs, validate_template


class ConfigError(ValueError):
    pass


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _num(env: Mapping[str, str], name: str, default, cast=int, minimum=None):
    raw = env.get(name, "")
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def load_opt_out(env_value: str, file_path: str) -> frozenset[str]:
    """DIDs that must never be quoted: BLUESKY_OPT_OUT_DIDS (comma/space separated) plus
    one DID per line in BLUESKY_OPT_OUT_FILE ('#' starts a comment)."""
    items = set((env_value or "").replace(",", " ").split())
    if file_path and Path(file_path).is_file():
        for line in Path(file_path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                items.add(line)
    bad = [d for d in items if not is_did(d)]
    if bad:
        raise ConfigError(f"opt-out list contains non-DIDs: {bad[:3]}")
    return frozenset(items)


@dataclass
class Config:
    storage_mode: str = "none"   # private | public | sheets | none (see mode.py)
    feeds_json: str = ""
    feeds_file: str = ""
    feed_names_json: str = ""
    data_dir: str = "data"
    state_dir: str = "state"
    spreadsheet_url: str = ""
    google_credentials_json: str = field(default="", repr=False)
    seen_retention_days: float = 30
    max_articles: int = 150
    per_feed_cap: int = 25
    max_age_hours: float = 168
    recheck_after_hours: float = 24
    max_run_minutes: float = 10
    resolve_max_attempts: int = 12
    request_timeout: float = 20
    article_delay: float = 0.5
    fail_threshold: float = 0.5
    user_agent: str = ""
    posting_enabled: bool = False
    bluesky_handle: str = field(default="", repr=False)
    bluesky_app_password: str = field(default="", repr=False)
    bluesky_service: str = "https://bsky.social"
    bluesky_appview: str = "https://public.api.bsky.app"
    repost_max_age_hours: float = 24
    max_posts_per_run: int = 10
    quote_links: bool = True
    daily_cap: int = 0          # 0 = unlimited (as v1)
    author_daily_cap: int = 0   # 0 = unlimited (as v1)
    opt_out_dids: frozenset[str] = frozenset()
    repost_max_attempts: int = 5
    post_delay: float = 2.0
    quote_template: str = DEFAULT_TEMPLATE
    quote_langs: list[str] = field(default_factory=lambda: ["en"])


def from_env(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    data_dir = env.get("DATA_DIR") or "data"
    c = Config(
        storage_mode=storage_mode(env),
        feeds_json=env.get("FEEDS_JSON", ""),
        feeds_file=env.get("FEEDS_FILE", ""),
        feed_names_json=env.get("FEED_NAMES_JSON", ""),
        data_dir=data_dir,
        state_dir=env.get("STATE_DIR") or str(Path(data_dir) / "state"),
        spreadsheet_url=(env.get("SPREADSHEET_URL") or "").strip(),
        google_credentials_json=env.get("GOOGLE_CREDENTIALS_JSON", ""),
        seen_retention_days=_num(env, "SEEN_RETENTION_DAYS", 30.0, float, 2),
        max_articles=_num(env, "MAX_ARTICLES", 150, int, 0),
        per_feed_cap=_num(env, "PER_FEED_CAP", 25, int, 1),
        max_age_hours=_num(env, "MAX_AGE_HOURS", 168.0, float, 1),
        recheck_after_hours=_num(env, "RECHECK_AFTER_HOURS", 24.0, float, 0),
        max_run_minutes=_num(env, "MAX_RUN_MINUTES", 10.0, float, 1),
        resolve_max_attempts=_num(env, "RESOLVE_MAX_ATTEMPTS", 12, int, 1),
        request_timeout=_num(env, "REQUEST_TIMEOUT", 20.0, float, 1),
        article_delay=_num(env, "ARTICLE_DELAY", 0.5, float, 0),
        fail_threshold=_num(env, "FAIL_THRESHOLD", 0.5, float, 0),
        user_agent=env.get("USER_AGENT", ""),
        posting_enabled=_bool(env.get("BLUESKY_POSTING_ENABLED")),
        bluesky_handle=(env.get("BLUESKY_HANDLE") or "").strip(),
        bluesky_app_password=(env.get("BLUESKY_APP_PASSWORD") or "").strip(),
        bluesky_service=env.get("BLUESKY_SERVICE") or "https://bsky.social",
        bluesky_appview=env.get("BLUESKY_APPVIEW") or "https://public.api.bsky.app",
        repost_max_age_hours=_num(env, "REPOST_MAX_AGE_HOURS", 24.0, float, 0),
        max_posts_per_run=_num(env, "MAX_POSTS_PER_RUN", 10, int, 0),
        quote_links=_bool(env.get("QUOTE_LINKS"), True),
        daily_cap=_num(env, "BLUESKY_DAILY_CAP", 0, int, 0),
        author_daily_cap=_num(env, "BLUESKY_AUTHOR_DAILY_CAP", 0, int, 0),
        opt_out_dids=load_opt_out(env.get("BLUESKY_OPT_OUT_DIDS", ""),
                                  env.get("BLUESKY_OPT_OUT_FILE", "config/bluesky-opt-out.txt")),
        repost_max_attempts=_num(env, "REPOST_MAX_ATTEMPTS", 5, int, 1),
        post_delay=_num(env, "POST_DELAY_SECONDS", 2.0, float, 0),
        quote_template=env.get("QUOTE_TEMPLATE") or DEFAULT_TEMPLATE,
        quote_langs=parse_langs(env.get("QUOTE_LANGS") or "en"),
    )
    if c.spreadsheet_url and not c.google_credentials_json:
        raise ConfigError("SPREADSHEET_URL is set but GOOGLE_CREDENTIALS_JSON is missing")
    if not 0 < c.fail_threshold <= 1:
        raise ConfigError("FAIL_THRESHOLD must be in (0, 1]")
    try:
        validate_template(c.quote_template)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return c
