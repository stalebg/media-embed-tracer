"""Google Sheets manager for logging embeds and tracking duplicates."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional, List, Dict, Tuple, Set

import gspread
from google.oauth2.service_account import Credentials

from .platforms.base import EmbedPost


logger = logging.getLogger(__name__)

# Google API scopes
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]


class SheetsManager:
    """Manages Google Sheets operations for embed logging."""

    def __init__(self, spreadsheet_url: str):
        """
        Initialize the Sheets manager.

        Args:
            spreadsheet_url: URL of the Google Spreadsheet
        """
        self.spreadsheet_url = spreadsheet_url
        self._client: Optional[gspread.Client] = None
        self._spreadsheet: Optional[gspread.Spreadsheet] = None
        self._worksheet_cache: dict = {}

    def _get_credentials(self) -> Credentials:
        """Get Google credentials from environment variables."""
        # Try to load from JSON string in environment
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')

        if creds_json:
            try:
                creds_data = json.loads(creds_json)
                return Credentials.from_service_account_info(creds_data, scopes=SCOPES)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse GOOGLE_CREDENTIALS_JSON: {e}")
                raise

        # Try to load from individual environment variables
        required_fields = [
            'GOOGLE_PROJECT_ID',
            'GOOGLE_PRIVATE_KEY_ID',
            'GOOGLE_PRIVATE_KEY',
            'GOOGLE_CLIENT_EMAIL',
            'GOOGLE_CLIENT_ID',
        ]

        missing = [f for f in required_fields if not os.environ.get(f)]
        if missing:
            raise ValueError(
                f"Missing Google credentials. Set GOOGLE_CREDENTIALS_JSON or these vars: {missing}"
            )

        creds_data = {
            'type': 'service_account',
            'project_id': os.environ['GOOGLE_PROJECT_ID'],
            'private_key_id': os.environ['GOOGLE_PRIVATE_KEY_ID'],
            'private_key': os.environ['GOOGLE_PRIVATE_KEY'].replace('\\n', '\n'),
            'client_email': os.environ['GOOGLE_CLIENT_EMAIL'],
            'client_id': os.environ['GOOGLE_CLIENT_ID'],
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
        }

        return Credentials.from_service_account_info(creds_data, scopes=SCOPES)

    def connect(self):
        """Connect to Google Sheets."""
        if self._client is not None:
            return

        try:
            creds = self._get_credentials()
            self._client = gspread.authorize(creds)
            self._spreadsheet = self._client.open_by_url(self.spreadsheet_url)
            logger.info(f"Connected to spreadsheet: {self._spreadsheet.title}")
        except Exception as e:
            logger.error(f"Failed to connect to Google Sheets: {e}")
            raise

    def get_worksheet(self, name: str) -> gspread.Worksheet:
        """
        Get or create a worksheet by name.

        Args:
            name: Name of the worksheet

        Returns:
            The worksheet object
        """
        self.connect()

        if name in self._worksheet_cache:
            return self._worksheet_cache[name]

        try:
            worksheet = self._spreadsheet.worksheet(name)
        except gspread.WorksheetNotFound:
            # Create the worksheet with headers
            worksheet = self._spreadsheet.add_worksheet(title=name, rows=1000, cols=15)
            headers = EmbedPost.sheet_headers()
            worksheet.update('A1', [headers])
            logger.info(f"Created new worksheet: {name}")

        self._worksheet_cache[name] = worksheet
        return worksheet

    def ensure_headers(self, worksheet: gspread.Worksheet):
        """Ensure the worksheet has the correct headers."""
        headers = EmbedPost.sheet_headers()
        current_headers = worksheet.row_values(1)

        if current_headers != headers:
            worksheet.update('A1', [headers])
            logger.info(f"Updated headers in {worksheet.title}")

    def is_duplicate(self, worksheet: gspread.Worksheet, post_url: str) -> bool:
        """
        Check if a post URL already exists in the worksheet.

        Args:
            worksheet: The worksheet to check
            post_url: The post URL to look for

        Returns:
            True if the URL exists, False otherwise
        """
        try:
            # Get the Post URL column (column 7 = G)
            post_url_col = 7  # "Post URL" is the 7th column
            all_values = worksheet.col_values(post_url_col)

            # Check if the URL exists (skip header)
            return post_url in all_values[1:]

        except Exception as e:
            logger.warning(f"Error checking duplicates: {e}")
            return False

    def get_existing_post_urls(self, worksheet: gspread.Worksheet) -> set[str]:
        """
        Get all existing post URLs from a worksheet.

        Args:
            worksheet: The worksheet to read from

        Returns:
            Set of post URLs
        """
        try:
            post_url_col = 7  # "Post URL" is the 7th column
            all_values = worksheet.col_values(post_url_col)
            return set(all_values[1:])  # Skip header
        except Exception as e:
            logger.warning(f"Error getting existing URLs: {e}")
            return set()

    def write_embed(self, embed: EmbedPost, worksheet_name: str = "All Embeds") -> bool:
        """
        Write an embed to the spreadsheet if it's not a duplicate.

        Args:
            embed: The EmbedPost to write
            worksheet_name: Name of the worksheet to write to

        Returns:
            True if written, False if duplicate or error
        """
        try:
            worksheet = self.get_worksheet(worksheet_name)
            self.ensure_headers(worksheet)

            # Check for duplicate
            if self.is_duplicate(worksheet, embed.post_url):
                logger.debug(f"Duplicate found, skipping: {embed.post_url}")
                return False

            # Append the row
            row = embed.to_sheet_row()
            worksheet.append_row(row, value_input_option='USER_ENTERED')
            logger.info(f"Wrote embed: {embed.post_url}")
            return True

        except Exception as e:
            logger.error(f"Failed to write embed: {e}")
            return False

    def write_embeds_batch(
        self,
        embeds: list[EmbedPost],
        worksheet_name: str = "All Embeds"
    ) -> tuple[int, int]:
        """
        Write multiple embeds, skipping duplicates.

        Args:
            embeds: List of EmbedPost objects
            worksheet_name: Name of the worksheet

        Returns:
            Tuple of (written_count, skipped_count)
        """
        if not embeds:
            return (0, 0)

        try:
            worksheet = self.get_worksheet(worksheet_name)
            self.ensure_headers(worksheet)

            # Get existing URLs for efficient duplicate checking
            existing_urls = self.get_existing_post_urls(worksheet)

            # Filter out duplicates
            new_embeds = [e for e in embeds if e.post_url not in existing_urls]
            skipped = len(embeds) - len(new_embeds)

            if not new_embeds:
                logger.info(f"All {skipped} embeds were duplicates")
                return (0, skipped)

            # Batch write new embeds
            rows = [e.to_sheet_row() for e in new_embeds]
            worksheet.append_rows(rows, value_input_option='USER_ENTERED')

            logger.info(f"Wrote {len(new_embeds)} embeds, skipped {skipped} duplicates")
            return (len(new_embeds), skipped)

        except Exception as e:
            logger.error(f"Failed to write embeds batch: {e}")
            return (0, len(embeds))

    def get_pending_bluesky_posts(self, worksheet_name: str = "All Embeds") -> list[dict]:
        """
        Get Bluesky posts that haven't been reposted yet.

        Args:
            worksheet_name: Name of the worksheet

        Returns:
            List of dicts with post info and row numbers
        """
        try:
            worksheet = self.get_worksheet(worksheet_name)
            all_rows = worksheet.get_all_values()

            if len(all_rows) <= 1:
                return []

            headers = all_rows[0]
            pending = []

            for row_num, row in enumerate(all_rows[1:], start=2):
                # Check if it's a Bluesky post with pending status
                if len(row) >= 11:
                    platform = row[2] if len(row) > 2 else ""
                    status = row[10] if len(row) > 10 else ""

                    if platform.lower() == "bluesky" and status.lower() == "pending":
                        pending.append({
                            'row_number': row_num,
                            'domain': row[3] if len(row) > 3 else "",
                            'author_handle': row[4] if len(row) > 4 else "",
                            'article_url': row[5] if len(row) > 5 else "",
                            'post_url': row[6] if len(row) > 6 else "",
                            'article_title': row[7] if len(row) > 7 else "",
                        })

            return pending

        except Exception as e:
            logger.error(f"Failed to get pending posts: {e}")
            return []

    def update_repost_status(
        self,
        worksheet_name: str,
        row_number: int,
        status: str
    ):
        """
        Update the repost status for a row.

        Args:
            worksheet_name: Name of the worksheet
            row_number: Row number (1-indexed)
            status: New status (e.g., "posted", "failed")
        """
        try:
            worksheet = self.get_worksheet(worksheet_name)
            # Column K (11) is the Repost Status
            worksheet.update_cell(row_number, 11, status)
            logger.debug(f"Updated row {row_number} status to: {status}")
        except Exception as e:
            logger.error(f"Failed to update repost status: {e}")
