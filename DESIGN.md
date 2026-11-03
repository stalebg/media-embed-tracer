# Design

## Run pipeline (`pipeline.py`)

| Step | What happens |
|---|---|
| 1. Feeds | Fetched with `requests` (timeouts, size cap), parsed from bytes by feedparser. Health tracked per feed. |
| 2. Queue | Retry hits whose canonical id needs a lookup that failed earlier. |
| 3. Schedule | Pick articles (see *Scheduling*). |
| 4. Fetch + detect | Every check is logged, including failures and zero-embed pages. |
| 5. Save | Data and state to the store. A failure exits 2 and skips posting. |
| 6. Post | Quote eligible Bluesky embeds, saving status after each action. |
| 7. Finish | Run log row, verdict, optional Sheets mirror (git modes). |

## Data model (`models.py`)

| Record | One row per | Fields |
|---|---|---|
| Occurrence | (article, post), append-only: the research unit | platform, canonical_id, post_url, author, embed_type, article_url, final_url, outlet, domain, feed, article_title, article_published, first_seen, detector_version, article_summary |
| ArticleCheck | fetch attempt: the denominator | url, final_url, outlet, domain, feed, title, published, checked_at, check_n, fetch_ok, http_status, error, embeds{platform: n}, embed_types{type: n}, detector_version |
| RunLog | run | totals, planned, skipped_by_cap{feed: n}, queued/dropped lookups, posting summary, exit code |
| Post | `platform:canonical_id` | post_url, author, first_seen, first_embed_seen, repost_status, attempts, next_try, quoted_uri, last_error, updated_at, via_article_url, via_title, via_outlet |
| SeenArticle (state) | checked article | checks, fetch_errors, found_keys (so a recheck adds only new posts), done |
| QueuedHit (state) | hit awaiting a lookup | retried each run, dropped after `RESOLVE_MAX_ATTEMPTS` |

- Outlet and domain come from the final URL after redirects.
- Content root: the main `<article>` only (article body, else the `<h1>` holder, else most
  text; others if at least half its size), minus nested `<article>`s, aside/nav/footer and
  teaser/related/recommendation/preview/promo boxes. Inline scripts stay page-wide.
  Teaser-like `<article>`s alone do not outrank `<main>` or a content container.
- Page furniture: a post found in 3+ articles of one outlet in one run is recorded as
  `<type>+template` for that outlet, never quoted from it (a pending one from that outlet
  becomes skipped) and not written to `All Embeds`. A real embed at another outlet still counts. Earlier runs are not
  counted, so a post re-embedded over days keeps one label; the content root above is the
  real protection.
- `article_summary` is the feed summary, HTML stripped, max 500 chars, never logged. It is
  the last column, so older sheets just gain one.
- `detector_version` is bumped when detection changes.
- Unresolved ids (Bluesky handles, TikTok short links) are queued, never written to data.

### Canonical ids

| Platform | canonical_id |
|---|---|
| x | numeric status id |
| bluesky | `at://<did>/app.bsky.feed.post/<rkey>`; author stored as the DID |
| tiktok | numeric video/photo id (short links expanded) |
| instagram | shortcode (`/p/`, `/reel/`, `/tv/` share it) |
| facebook | `post:`, `video:`, `photo:`, `share:<p\|v\|r\|x>:`, `fbwatch:` |
| youtube | 11-character video id |
| threads | shortcode |
| mastodon | `<host>/<status id>` |
| reddit | `t3_<post>`, `t1_<comment>`, or `share:<code>` |
| telegram | `<channel>/<message id>`, channel lower-cased |
| linkedin | `urn:li:<share\|ugcPost\|activity>:<id>` |

### Detection (`detect.py`)

- Markup is scanned in the article body: `<article>`, else `<main>`, else common content
  containers, else `<body>`. `<nav>` is skipped. Inline scripts are scanned page-wide.
- Links inside a recognised embed are not counted again.
- Wayback Machine (`web.archive.org/web/<digits>[xx_]/…`) and archive.today snapshot links
  are matched as the URL they wrap (`platforms/_common.parse`).
- Each post is reported once per article, with its strongest type.
- Detection is pure. Lookups happen in `resolve.py`, cached and capped per run.
- Unverified or legacy formats are marked `UNVERIFIED` in the platform modules.

## Scheduling (`scheduler.py`)

- Rechecks (checked once, at least `RECHECK_AFTER_HOURS` ago) take up to half of `MAX_ARTICLES`.
- New and retry articles follow, round-robin across feeds, newest first, capped by
  `PER_FEED_CAP`. The start feed rotates each run.
- Leftover budget goes to more rechecks.
- Articles left by the caps or by `MAX_RUN_MINUTES` are counted in `skipped_by_cap`.
- Defaults (150/run, 25/feed) cover about 30 feeds × 5 new articles per 30 minutes.

## Storage modes (`mode.py`, `scripts/branches.sh`)

Same rules in both files; first match wins.

| Mode | Trigger | Store | Export |
|---|---|---|---|
| private | `DATA_REPO` | `JsonlStore`, branches in a separate repo (remote `store`) | skipped |
| public | `PUBLIC_DATA_BRANCH=true` | `JsonlStore`, branches in this repo | weekly release |
| sheets | `SPREADSHEET_URL` + `GOOGLE_CREDENTIALS_JSON` | `SheetsStore` | skipped |
| none | nothing | CI fails before scraping; locally JSONL in `./data` | – |

`Store` protocol (`store/base.py`):

| Method | Contract |
|---|---|
| `load()` | Returns posts, seen, health, queue. Fails rather than start from empty state. |
| `save(...)` | Append occurrences/articles, upsert posts/seen/health, replace queue. |
| `update_post(post)` | After every Bluesky action. JSONL writes at once; sheets writes `posted` at once, buffers the rest. |
| `append_run(run)` | Last write of a run; flushes buffers. |

Every failure raises `StoreError` (exit 2).

### JSONL (git modes)

| Branch | Files | Commits |
|---|---|---|
| `data` | `<instance>/occurrences/YYYY-MM.jsonl`, `articles/YYYY-MM.jsonl`, `runs/YYYY-MM.jsonl`, `posts.jsonl` | fast-forward only |
| `state` | `seen_articles.jsonl`, `feed_health.json`, `resolve_queue.jsonl` | one commit, force-pushed |

- Data is pushed before state. A failed state push causes a recheck and duplicate rows,
  never a lost occurrence.
- Export dedupes occurrences on (article_url, platform, canonical_id) and checks on
  (url, check_n), keeping the earliest. CSV cells starting with `= + - @` get a leading `'`.
- Appends are one write plus fsync and start on a fresh line. Readers skip malformed lines.
- Tokens: checkout uses `persist-credentials: false`. Only branch steps get a token, sent
  as a URL-scoped header via `GIT_CONFIG_*`, masked, never in `.git/config`.

### Sheets (`store/sheets_store.py`)

| Tab | Key | Write |
|---|---|---|
| `occurrences`, `articles`, `runs` | – | append |
| `posts` | platform, canonical_id | upsert; source of truth for posting |
| `article_counts` | UTC date, feed | upsert, counts added each run |
| `feed_health` | feed | upsert |
| `_seen` | url | upsert; rows past `SEEN_RETENTION_DAYS` deleted; no summaries |
| `_queue` | url, embed_type, article_url | made equal to the queue |
| `_meta` | key | `instance`, `v1_imported` |
| `All Embeds` | columns F, G | v1 layout, only if it exists; appended after posting and the `runs` row |

- **Load:** one metadata request plus one `values.batchGet`. Unexpected headers, rows
  without a header, or a foreign `instance` raise `StoreError` before scraping. Missing
  tabs are created; a tab missing only a newer trailing column gets it added.
- **Writes per tab:** re-read keys, then one bottom-up delete batch, one update, one append.
  RAW only. A failed delete is a warning plus a fresh key read.
- **Order:** occurrences, articles, posts, `_queue`, `_seen`, `_meta`, article_counts,
  feed_health. A failure before `_seen` means a recheck. article_counts after `_seen`
  never double-counts.
- **Requests:** 5–25 per run plus up to 3 per quote; ≥1.1 s apart; 60 s timeout.

| Request | Retried on | Backoff |
|---|---|---|
| read, update | 408, 429, 5xx | max(Retry-After, 5 s × 2^n) + jitter, ~90 s total |
| append, create tab | 429 only | same |
| delete rows | never | – |

- **v1 import:** if `v1_imported` is unset and an `All Embeds` tab exists, its rows seed
  posts and seen articles (no occurrences). Posted stays posted, pending becomes skipped.
  Up to 200 handle lookups.
- **All Embeds dedup:** one row per (article, post) as v1, keyed by column F (canonical
  article URL, compared with the occurrence's article and final URL) and the canonical id
  from column G. A handle URL is resolved only on an rkey match; if the handle cannot be
  resolved now (cap, deadline, error) a row with the same rkey and article counts as known
  (no duplicate; at worst another author's post with the same rkey in that article gets no
  row). Rows skipped earlier are not backfilled. Repost Status of every row of a post
  changed this run is updated.
- **All Embeds format:** as v1 (UTC): Date `2026-11-01`, Time `0:27:38`, Platform `twitter`
  for X, Bluesky Author Handle with the verified handle, Published Date `2026-10-29 8:00`.
  Post URL: `V1_POST_URL_FORM` in `store/sheets.py`, `"did"` (default: always the DID,
  matched offline) or `"handle"` (as v1; the DID when no handle is verified). Existing
  rows in either form are matched. Handle lookups:
  20 per run, own cap, none past MAX_RUN_MINUTES (then the DID).
- **All Embeds failures:** the `runs` row is written first; a failed All Embeds write is a
  warning with the number of rows left out, not retried (occurrences has them).
- **Grid:** ranges start at row 1; new tabs get 100 rows; a delete never removes the last
  data row (cleared instead), so a drained tab keeps a row and a frozen header is fine.
- **Size:** about 365 × (15·C + 15·O + 26·P + 19·F) + 263k cells a year (C checks, O
  occurrences, P new posts, F feeds per day). 30 feeds and 1,200 checks a day ≈ 8.3M.
  Warning above 80 % of 10M; never deletes or stops.
- **Errors** carry the exception class only (`StoreError(..., safe=True)`: shown in full
  in redact mode).

### Mirror (`store/sheets.py`, git modes)

Written once at the end of a run: appends occurrences, articles, runs; upserts posts and
feed_health. RAW, `spreadsheets` scope only, never read. A failure exits 2 after data is
saved. An existing v1 `All Embeds` tab keeps filling.

## Redaction (`redact.py`)

On with `LOG_REDACT=true`, `DATA_REPO` set, or sheets mode.

| Input | Output |
|---|---|
| URLs, `at://` URIs, DIDs, handles, e-mail, `host/path` | replaced by placeholders |
| args wrapped in `private()` (titles, ids) | dropped whole |
| errors wrapped in `private_error()` | class name or fixed label (`HTTP 404`) |
| `StoreError(..., safe=True)` (own text, tab names, class names) | shown in full |
| urllib3 retry warnings | silenced |

Applies to log lines, stdout and stderr (prints, tracebacks, annotations).

## Repost state machine (`repost.py`, `posting.py`)

```
n/a --embed seen--> pending (posting on) | skipped (posting off)
pending|retry --> posted | retry (30 min × 2^n, cap 12 h) | failed | skipped
posted, skipped, failed: terminal
```

- Eligible: Bluesky `blockquote`, `iframe`, `widget`, and `link` unless `QUOTE_LINKS=false`.
- Pending and retry posts older than `REPOST_MAX_AGE_HOURS` become skipped.
- Before posting: read the bot's feed (≥100, or daily cap × window days + 1; max 1000).
  Already-quoted subjects become `posted` first and count toward the daily caps. If the
  read fails, nothing is posted (exit 5).
- Subject fetched via `getPosts` through the AppView for the CID and `embeddingDisabled`.
- Caps per account: `MAX_POSTS_PER_RUN`, `BLUESKY_DAILY_CAP`, `BLUESKY_AUTHOR_DAILY_CAP`,
  opt-out list. Held posts stay pending until they expire.
- Quote text fits 300 graphemes and 3000 bytes; link facet uses UTF-8 byte offsets.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | config, including an empty feed list |
| 2 | store or mirror; posting skipped if save failed |
| 3 | Bluesky login; statuses stay `pending` |
| 4 | more than `FAIL_THRESHOLD` of feed fetches, or of ≥4 article fetches, failed; or an article check crashed (class name logged, article untouched and retried next run) |
| 5 | required Bluesky read failed, or an unexpected error while creating a quote (status left as is, posting stopped) |

## v1 migration (`migrate.py`)

`migrate-v1` reads the v1 sheet (never writes it) into a JSONL store. Post URLs are
re-matched to canonical ids; imported rows get `embed_type=unknown`, `detector_version=v1`.
Statuses: posted and failed kept, pending → skipped, rest → n/a. Bluesky handle URLs need
`--resolve`. Not needed in sheets mode, which imports automatically.
