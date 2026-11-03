# media-embed-tracer

News sites embed social media posts all the time. This tool finds them. It reads your RSS
feeds, checks each new article for embedded posts from eleven platforms, and logs every
(article, post) pair to a dataset you control. A Bluesky bot can quote-post the Bluesky
posts it finds.

It runs on GitHub Actions every 30 minutes. No server, no database: data lives in a Google
Sheet or in git branches.

**Platforms:** X/Twitter, Bluesky, TikTok, Instagram, Facebook, YouTube, Threads, Mastodon,
Reddit, Telegram, LinkedIn.

## What you get

A research dataset that answers "which outlets embed which posts, and how?"

- **occurrences:** one row per post found in an article, with outlet, feed, title, summary,
  post URL, author and `embed_type`.
- **articles:** one row per article check, including failed fetches and pages with no
  embeds. This is your denominator.
- **posts:** one row per unique post, with its quote-bot status.
- **runs** and **feed_health:** what each run did and which feeds are struggling.

Every article is checked twice: once when it appears and again a day later, because outlets
often add embeds after publishing. Each post gets a stable canonical id, so `twitter.com` and
`x.com` links, or a Bluesky handle and its DID, count as one post. All times are UTC.

How an embed was found:

| `embed_type` | Meaning | Quotable |
|---|---|---|
| `blockquote` | Official embed blockquote in the article body | yes |
| `iframe` | `<iframe>`/`<amp-iframe>` pointing at a platform embed URL | yes |
| `widget` | Other official embed placeholders (`div.fb-post`, `lite-youtube`, `amp-twitter`, ...) | yes |
| `link` | Plain link in the article body | unless `QUOTE_LINKS=false` |
| `script-data` | URL in inline page data; often just a serialised link, not an embed | no |
| `unknown` | Imported from v1 | no |

Only the page's main `<article>` is scanned, without teaser cards, related-story boxes or
other page furniture, so a post in a teaser is not credited to every page that shows it.
A post still found in 3 or more articles of one outlet in one run is recorded as
`<type>+template` there and never quoted from that outlet.

Field lists, canonical id formats and storage internals are in [DESIGN.md](DESIGN.md).

## Quick setup (sheets mode)

The simplest setup: a Google Sheet holds both the bot's state and the dataset. Share it
with whoever you like.

1. Fork or copy this repository.
2. Put your feeds in `config/feeds.international.toml` (and `config/feeds.localized.toml` if
   you run a second instance; it ships empty and fails until you add feeds).
3. Create a Google Cloud service account, enable the Sheets API, and download a JSON key.
4. Create a spreadsheet and share it with the service account's address as Editor.
5. Under *Settings → Secrets and variables → Actions*, add the secrets
   `GOOGLE_CREDENTIALS_JSON` (the whole key) and `SPREADSHEET_URL` (the sheet's URL or id).
6. Enable Actions. Start a run by hand with *Run workflow* to check it works.

Tabs are created on the first run. Read, filter and chart them freely, but leave the tabs
starting with `_` alone: they are the bot's memory. Use filter views rather than sorting
`posts` while a run is going.

Each instance needs its own spreadsheet (`LOCALIZED_SPREADSHEET_URL` for the second one).
If the sheet already has a v1 "All Embeds" tab, the first run imports its state and keeps
appending to it in the v1 layout.

## Configuration

Repository secrets and variables. Each scrape workflow maps its own names to the generic
ones the code reads. The names match v1, so a v1 repository needs no renames.

| International | Localized | Kind | Purpose |
|---|---|---|---|
| `SPREADSHEET_URL` | `LOCALIZED_SPREADSHEET_URL` | secret | The store in sheets mode; a mirror otherwise |
| `GOOGLE_CREDENTIALS_JSON` | (shared) | secret | Service account key |
| `BLUESKY_INTERNATIONAL_USERNAME` | `BLUESKY_LOCALIZED_USERNAME` | secret | Bot handle |
| `BLUESKY_INTERNATIONAL_PASSWORD` | `BLUESKY_LOCALIZED_PASSWORD` | secret | Bot app password |
| `BLUESKY_POSTING_ENABLED` | `LOCALIZED_BLUESKY_POSTING_ENABLED` | secret or variable | `true` to post |
| `INTERNATIONAL_QUOTE_TEMPLATE` | `LOCALIZED_QUOTE_TEMPLATE` | variable | Quote text |
| `INTERNATIONAL_QUOTE_LANGS` | `LOCALIZED_QUOTE_LANGS` | variable | Post languages, e.g. `en` |
| `QUOTE_LINKS` | (shared) | variable | `false` quotes real embeds only |
| `FEEDS_JSON` | `LOCALIZED_FEEDS_JSON` | secret | Replaces the feed file |
| `FEED_NAMES_JSON` | `LOCALIZED_FEED_NAMES_JSON` | secret | `{"domain": "Display name"}` |
| `DATA_REPO`, `DATA_REPO_TOKEN` | (shared) | variable, secret | Private git store (below) |
| `PUBLIC_DATA_BRANCH` | (shared) | variable | `true` for public git branches (below) |

`BLUESKY_USERNAME` and `BLUESKY_PASSWORD` also work for the international instance.

Environment variables the code reads (defaults in brackets):

| Variable | Meaning |
|---|---|
| `FEEDS_FILE`, `FEEDS_JSON`, `FEED_NAMES_JSON` | Feed list (TOML/JSON file, or JSON) and outlet display names |
| `MAX_ARTICLES` [150], `PER_FEED_CAP` [25] | Article fetches per run, new articles per feed per run |
| `MAX_AGE_HOURS` [168] | Ignore older feed entries |
| `RECHECK_AFTER_HOURS` [24] | When to check an article again; `0` disables |
| `MAX_RUN_MINUTES` [10] | Fetch budget; keeps runs inside the 15-minute gap between instances |
| `SEEN_RETENTION_DAYS` [30] | How long checked articles are remembered |
| `RESOLVE_MAX_ATTEMPTS` [12] | Runs to retry a failed handle or short-link lookup |
| `REQUEST_TIMEOUT` [20], `ARTICLE_DELAY` [0.5] | Seconds per request, seconds between articles |
| `FAIL_THRESHOLD` [0.5] | Exit 4 above this share of failed fetches |
| `USER_AGENT` [`media-embed-tracer/<version>`] | HTTP User-Agent |
| `LOG_LEVEL` [INFO], `LOG_REDACT` [false] | Logging; redaction is forced on for private stores |
| `INSTANCE_NAME` | Set by the workflow; a sheet belongs to one instance |
| `DATA_DIR` [data], `STATE_DIR` [`$DATA_DIR/state`] | JSONL locations (git modes) |
| `SPREADSHEET_URL`, `GOOGLE_CREDENTIALS_JSON` | Google Sheet and service account key |
| `V1_SPREADSHEET_URL` | v1 sheet for `migrate-v1` |
| `BLUESKY_POSTING_ENABLED` [false] | Turn on quote-posting |
| `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` | Bot account |
| `BLUESKY_SERVICE` [bsky.social], `BLUESKY_APPVIEW` [public.api.bsky.app] | Login service, AppView for lookups |
| `QUOTE_TEMPLATE` [`Embedded by {outlet}: {title}`] | Quote text; only `{outlet}` and `{title}` |
| `QUOTE_LANGS` [en], `QUOTE_LINKS` [true] | Post languages (max 3); quote plain links too |
| `REPOST_MAX_AGE_HOURS` [24] | Only quote posts first embedded within this window |
| `MAX_POSTS_PER_RUN` [10], `POST_DELAY_SECONDS` [2] | Quotes per run, seconds between quotes |
| `BLUESKY_DAILY_CAP` [0], `BLUESKY_AUTHOR_DAILY_CAP` [0] | Quotes per rolling 24 h, in total and per author; `0` = no cap |
| `BLUESKY_OPT_OUT_DIDS`, `BLUESKY_OPT_OUT_FILE` [`config/bluesky-opt-out.txt`] | Authors never to quote |
| `REPOST_MAX_ATTEMPTS` [5] | Transient errors before `failed` |

## How the bot posts

Posting is off until you set `BLUESKY_POSTING_ENABLED=true` and add the bot's handle and an
app password. Then, for each Bluesky post found as an embed (or link), the bot quote-posts
it with `Embedded by {outlet}: {title}` and the article link.

- It only quotes posts first embedded in the last 24 hours, so turning posting on never
  floods the account with a backlog.
- Before posting it reads its own recent posts and never quotes the same post twice. If
  that read fails, it posts nothing.
- It skips authors who disabled quoting and DIDs on the opt-out list
  (`config/bluesky-opt-out.txt`).
- Two instances are two accounts. Both may quote a post that different outlets embed.

Mark the bot's profile as automated and say in the bio how authors can opt out.

| Status | Meaning |
|---|---|
| `n/a` | Not quotable (other platform, or script-data only) |
| `pending` | Waiting to be quoted |
| `posted` | Quoted |
| `retry` | Transient error; retried with backoff |
| `skipped` | Will not be quoted (posting off, too old, opted out, quoting disabled) |
| `failed` | Permanent error, or too many retries |

## Limits and gotchas

- **The 60-day rule.** GitHub disables scheduled workflows in a public repository after 60
  days without activity. With a private store nothing is committed there, so
  `keepalive.yml` re-enables the schedules twice a month. If one gets disabled anyway,
  re-enable it in the Actions tab.
- **10 million cells.** A Google Sheet holds at most 10M cells. At 30 feeds that is about a
  year, mostly the `articles` log. The bot warns at 80 %. Then move old `articles` rows to
  an archive sheet or start a new one. At 100 % it stops saving and posting.
- **Public logs are redacted.** Actions logs of a public repository are public. With a
  private store, the scraper strips URLs, handles, DIDs, titles and error details from all
  output. Counts, feed names and status codes remain. Your feed list in `config/` stays
  public.
- **No browser.** Embeds added by JavaScript, or hidden behind consent walls and paywalls,
  are invisible. Sites that block bots count as failed fetches.
- **Some ids don't merge.** Mastodon ids are per instance. Facebook numeric and `pfbid` ids,
  and Facebook and Reddit share links, stay separate.

A failing run exits non-zero and turns red:

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Configuration error |
| 2 | Store or mirror failure (nothing is posted if saving failed) |
| 3 | Bluesky login failed |
| 4 | Too many feed or article fetches failed, or an article check crashed (retried next run) |
| 5 | Could not read the bot's recent posts (nothing was posted), or creating a quote failed unexpectedly |

## Other storage modes

Prefer git? Two more modes keep data and state on branches (`data` and `state`). The first
match wins:

| Mode | Set | Data goes to |
|---|---|---|
| Private store | variable `DATA_REPO=owner/embed-data`, secret `DATA_REPO_TOKEN` | branches in that private repository |
| Public branch | variable `PUBLIC_DATA_BRANCH=true` | branches in this repository, plus a weekly CSV release |
| Sheets | secrets `SPREADSHEET_URL`, `GOOGLE_CREDENTIALS_JSON` | the Google Sheet |

With nothing set, the scrape stops before doing anything. For the private store, create an
empty private repository and a fine-grained token with *Contents: Read and write* on it
only. In the git modes a configured spreadsheet becomes a write-only mirror.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[sheets,dev]'
FEEDS_FILE=config/feeds.international.toml MAX_ARTICLES=10 python -m media_embed_tracer run
python -m media_embed_tracer detect https://example.com/some-article   # embeds on one page
python -m media_embed_tracer export --data-dir data --out export        # CSV + summary.json
python -m media_embed_tracer migrate-v1 --spreadsheet "<v1 sheet URL>"  # v1 sheet to JSONL
pytest && ruff check .
```

Local runs write to `./data`. Tests make no network calls.

## License

MIT, see [LICENSE](LICENSE).
