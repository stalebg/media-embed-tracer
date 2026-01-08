"""Bluesky poster for quote-posting discovered embeds."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple

from atproto import Client
from atproto_client.models.com.atproto.repo.get_record import Params


logger = logging.getLogger(__name__)


class BlueskyPoster:
    """Handles posting quote skeets to Bluesky."""

    def __init__(self, account_name: str = "default"):
        """
        Initialize the Bluesky poster.

        Args:
            account_name: Name of the account for environment variable lookup
                         (e.g., "international" looks for BLUESKY_INTERNATIONAL_USERNAME)
        """
        self.account_name = account_name
        self._client: Optional[Client] = None
        self._did: Optional[str] = None

        # Feed name dictionary - maps domains to friendly names
        self.feed_names: dict[str, str] = {}

    def _get_credentials(self) -> tuple[str, str]:
        """Get Bluesky credentials from environment variables."""
        # Try account-specific credentials first
        prefix = f"BLUESKY_{self.account_name.upper()}"
        username = os.environ.get(f"{prefix}_USERNAME")
        password = os.environ.get(f"{prefix}_PASSWORD")

        if username and password:
            return username, password

        # Fall back to generic credentials
        username = os.environ.get("BLUESKY_USERNAME")
        password = os.environ.get("BLUESKY_PASSWORD")

        if username and password:
            return username, password

        raise ValueError(
            f"Missing Bluesky credentials. Set {prefix}_USERNAME and {prefix}_PASSWORD, "
            "or BLUESKY_USERNAME and BLUESKY_PASSWORD"
        )

    def connect(self):
        """Connect and authenticate to Bluesky."""
        if self._client is not None:
            return

        try:
            username, password = self._get_credentials()
            self._client = Client()
            profile = self._client.login(username, password)
            self._did = profile.did
            logger.info(f"Connected to Bluesky as @{username}")
        except Exception as e:
            logger.error(f"Failed to connect to Bluesky: {e}")
            raise

    def set_feed_names(self, feed_names: dict[str, str]):
        """
        Set the feed name dictionary for formatting posts.

        Args:
            feed_names: Dict mapping domains to friendly names
        """
        self.feed_names = feed_names

    def _convert_to_at_uri(self, post_url: str) -> Optional[str]:
        """
        Convert a Bluesky URL to an at:// URI format.

        Args:
            post_url: URL of the Bluesky post (e.g., https://bsky.app/profile/handle/post/id)

        Returns:
            The at:// URI for the post, or None if invalid
        """
        try:
            parts = post_url.split('/')
            handle = parts[-3]  # Extract handle from URL
            post_id = parts[-1]  # Extract post ID from URL
            return f'at://{handle}/app.bsky.feed.post/{post_id}'
        except IndexError:
            logger.error(f"Invalid post URL format: {post_url}")
            return None

    def _fetch_cid(self, post_url: str) -> Optional[tuple[str, str]]:
        """
        Fetch the CID for a post to be quoted.

        Args:
            post_url: URL of the Bluesky post

        Returns:
            Tuple of (uri, cid) or None if fetch fails
        """
        self.connect()

        try:
            at_uri = self._convert_to_at_uri(post_url)
            if not at_uri:
                return None

            # Parse the at_uri to get repository information
            uri_parts = at_uri.split("/")
            repo = uri_parts[2]  # DID or handle of the user
            collection = "app.bsky.feed.post"
            rkey = uri_parts[-1]  # The unique record key

            # Create params and fetch the record
            params = Params(repo=repo, collection=collection, rkey=rkey)
            response = self._client.com.atproto.repo.get_record(params)

            if response.cid and response.uri:
                return response.uri, response.cid

            logger.error(f"Failed to fetch CID for post: {post_url}")
            return None

        except Exception as e:
            logger.error(f"Error fetching CID for {post_url}: {e}")
            return None

    def _prepare_quote_record(
        self,
        uri: str,
        cid: str,
        article_url: str,
        article_title: str,
        article_domain: str,
    ) -> dict:
        """
        Prepare the data for posting a quote skeet.

        Args:
            uri: URI of the post to quote
            cid: CID of the post to quote
            article_url: URL of the article
            article_title: Title of the article
            article_domain: Domain of the article

        Returns:
            Record dict ready to post
        """
        # Get friendly feed name or use domain
        feed_name = self.feed_names.get(article_domain, article_domain)

        # Build the post text
        text = f"Quoted by {feed_name} → {article_title} {article_url}"

        # Calculate byte positions for the link facet
        text_bytes = text.encode('utf-8')
        link_bytes = article_url.encode('utf-8')
        link_start = text_bytes.find(link_bytes)
        link_end = link_start + len(link_bytes)

        # Create facets to make the link clickable
        facets = [
            {
                "index": {
                    "byteStart": link_start,
                    "byteEnd": link_end
                },
                "features": [{
                    "$type": "app.bsky.richtext.facet#link",
                    "uri": article_url
                }]
            }
        ]

        # Build the record
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "facets": facets,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "embed": {
                "$type": "app.bsky.embed.record",
                "record": {
                    "uri": uri,
                    "cid": cid
                }
            }
        }

        return record

    def post_quote(
        self,
        post_url: str,
        article_url: str,
        article_title: str,
        article_domain: str,
    ) -> bool:
        """
        Post a quote skeet about a discovered embed.

        Args:
            post_url: URL of the Bluesky post to quote
            article_url: URL of the article containing the embed
            article_title: Title of the article
            article_domain: Domain of the article

        Returns:
            True if posted successfully, False otherwise
        """
        self.connect()

        try:
            # Fetch CID for the post
            cid_data = self._fetch_cid(post_url)
            if not cid_data:
                logger.error(f"Failed to fetch CID for {post_url}")
                return False

            uri, cid = cid_data

            # Prepare the record
            record = self._prepare_quote_record(
                uri=uri,
                cid=cid,
                article_url=article_url,
                article_title=article_title,
                article_domain=article_domain,
            )

            # Post it
            response = self._client.com.atproto.repo.create_record(
                data={
                    "collection": "app.bsky.feed.post",
                    "repo": self._did,
                    "record": record
                }
            )

            if hasattr(response, 'uri') and response.uri:
                logger.info(f"Posted quote skeet: {response.uri}")
                return True

            logger.error("Post response missing URI")
            return False

        except Exception as e:
            logger.error(f"Failed to post quote skeet: {e}")
            return False
