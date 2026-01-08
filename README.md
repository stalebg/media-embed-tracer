# Media Embed Tracer

Automatically scrape RSS feeds and detect embedded social media posts (Bluesky, Twitter/X, TikTok, Instagram, Facebook). Log findings to Google Sheets and optionally repost Bluesky embeds to a Bluesky account.

## Features

- **Multi-platform detection**: Bluesky, Twitter/X, TikTok, Instagram, Facebook
- **RSS feed scraping**: Configurable list of feeds to monitor
- **Google Sheets logging**: Track all discovered embeds with duplicate prevention
- **Bluesky reposting**: Automatically quote-post discovered Bluesky embeds
- **GitHub Actions**: Runs on a schedule (every 30 minutes by default)

## Setup

### 1. Fork or Clone This Repository

```bash
git clone https://github.com/your-username/media-embed-tracer.git
```

### 2. Create a Google Sheets Spreadsheet

1. Create a new Google Spreadsheet
2. Note the URL (you'll need it for `SPREADSHEET_URL`)
3. Share the spreadsheet with your service account email (see setup instruction below)

### 3. Set Up Google Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project or select an existing one
3. Enable the Google Sheets API and Google Drive API
4. Create a Service Account:
   - Go to "IAM & Admin" > "Service Accounts"
   - Click "Create Service Account"
   - Give it a name and create
5. Create a key:
   - Click on the service account
   - Go to "Keys" tab
   - Add Key > Create new key > JSON
   - Download the JSON file
6. Share your spreadsheet:
   - Open the JSON file and copy the `client_email`
   - Share your Google Spreadsheet with this email (Editor access)

### 4. Set Up Bluesky App Password (Optional)

If you want to repost Bluesky embeds:

1. Go to [Bluesky Settings](https://bsky.app/settings)
2. Navigate to "App Passwords"
3. Create a new app password
4. Save it for the `BLUESKY_PASSWORD` secret

### 5. Configure GitHub Secrets

Go to your repository's Settings > Secrets and variables > Actions, and add:

#### Required Secrets

| Secret | Description |
|--------|-------------|
| `FEEDS_JSON` | JSON array of feeds (see format below) |
| `SPREADSHEET_URL` | Full URL of your Google Spreadsheet |
| `GOOGLE_CREDENTIALS_JSON` | Full contents of your service account JSON file |

#### Optional Secrets (for Bluesky posting)

| Secret | Description |
|--------|-------------|
| `BLUESKY_POSTING_ENABLED` | Set to `true` to enable posting |
| `BLUESKY_ACCOUNT` | Account name: `international` or `localized` |
| `BLUESKY_USERNAME` | Your Bluesky handle (e.g., `yourbot.bsky.social`) |
| `BLUESKY_PASSWORD` | Your Bluesky app password |
| `FEED_NAMES_JSON` | JSON mapping domains to friendly names |

#### Account-Specific Bluesky Credentials

For running multiple instances (e.g., international + localized regional feeds):

| Secret | Description |
|--------|-------------|
| `BLUESKY_INTERNATIONAL_USERNAME` | International bot handle |
| `BLUESKY_INTERNATIONAL_PASSWORD` | International bot app password |
| `BLUESKY_LOCALIZED_USERNAME` | Localized/regional bot handle |
| `BLUESKY_LOCALIZED_PASSWORD` | Localized/regional bot app password |

### 6. Configure Feeds

Create a JSON array with your RSS feeds:

```json
[
  {
    "name": "The Guardian",
    "url": "https://www.theguardian.com/world/rss"
  },
  {
    "name": "BBC News",
    "url": "https://feeds.bbci.co.uk/news/rss.xml"
  }
]
```

Store this as the `FEEDS_JSON` secret.

### 7. Configure Feed Names (Optional)

For nicer Bluesky post formatting, create a JSON feed dictionary:

```json
{
  "theguardian.com": "The Guardian",
  "bbc.co.uk": "BBC",
  "nytimes.com": "NYT"
}
```

Store this as the `FEED_NAMES_JSON` secret.

## Running Multiple Instances

To run separate instances for international and localized outlets:

1. **Option A**: Fork the repo twice with different secrets
2. **Option B**: Create two workflow files with different configurations

For Option B, create `.github/workflows/scrape-localized.yml`:

```yaml
name: Scrape Localized Feeds

on:
  schedule:
    - cron: '15,45 * * * *'  # Offset from main workflow
  workflow_dispatch:

jobs:
  scrape:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'
      - run: pip install -r requirements.txt
      - run: python -m src.main
        env:
          FEEDS_JSON: ${{ secrets.LOCALIZED_FEEDS_JSON }}
          SPREADSHEET_URL: ${{ secrets.LOCALIZED_SPREADSHEET_URL }}
          GOOGLE_CREDENTIALS_JSON: ${{ secrets.GOOGLE_CREDENTIALS_JSON }}
          BLUESKY_POSTING_ENABLED: 'true'
          BLUESKY_ACCOUNT: localized
          BLUESKY_LOCALIZED_USERNAME: ${{ secrets.BLUESKY_LOCALIZED_USERNAME }}
          BLUESKY_LOCALIZED_PASSWORD: ${{ secrets.BLUESKY_LOCALIZED_PASSWORD }}
          FEED_NAMES_JSON: ${{ secrets.LOCALIZED_FEED_NAMES_JSON }}
```

## Google Sheet Format

The scraper creates/uses a worksheet called "All Embeds" with these columns:

| Column | Description |
|--------|-------------|
| Date | Date discovered (YYYY-MM-DD) |
| Time | Time discovered (HH:MM:SS) |
| Platform | Social media platform |
| Domain | Source article domain |
| Author Handle | Post author's handle |
| Article URL | URL of the article |
| Post URL | URL of the embedded post |
| Article Title | Title of the article |
| Article Summary | Brief summary |
| Published Date | When the article was published |
| Repost Status | `pending`, `posted`, or `failed` |

## Supported Platforms

### Bluesky
- Direct bsky.app links
- at:// URI format
- Bluesky embed blockquotes

### Twitter/X
- twitter.com and x.com links
- Twitter embed blockquotes

### TikTok
- Full tiktok.com video URLs
- Short vm.tiktok.com links (automatically expanded)
- TikTok embed blockquotes

### Instagram
- Posts (/p/)
- Reels (/reel/, /reels/)
- IGTV (/tv/)
- Instagram embed blockquotes

### Facebook
- Posts
- Videos and Watch
- Reels
- Photos
- fb.watch short links
