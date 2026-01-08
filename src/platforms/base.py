"""Base platform class and data structures for embed detection."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from urllib.parse import urlparse
import logging

from bs4 import BeautifulSoup


def extract_article_content(html: str) -> str:
    """
    Extract the main article content from HTML, excluding headers, footers, sidebars.

    Tries multiple strategies:
    1. <article> tags
    2. <main> tag
    3. Common article container classes/IDs
    4. Falls back to full HTML if nothing found

    Args:
        html: Full HTML content

    Returns:
        HTML string containing just the article content
    """
    soup = BeautifulSoup(html, 'html.parser')

    # Remove obvious non-content elements first
    for tag in soup.find_all(['header', 'footer', 'nav', 'aside', 'script', 'style', 'noscript']):
        tag.decompose()

    # Strategy 1: Look for <article> tags
    articles = soup.find_all('article')
    if articles:
        # If multiple articles, concatenate them (some sites have article + comments as separate articles)
        return '\n'.join(str(article) for article in articles)

    # Strategy 2: Look for <main> tag
    main = soup.find('main')
    if main:
        return str(main)

    # Strategy 3: Look for common article container classes/IDs
    content_selectors = [
        {'class_': 'article-content'},
        {'class_': 'article-body'},
        {'class_': 'post-content'},
        {'class_': 'entry-content'},
        {'class_': 'story-body'},
        {'class_': 'content-body'},
        {'id': 'article-body'},
        {'id': 'story-body'},
        {'id': 'main-content'},
        {'class_': 'c-entry-content'},  # Vox Media sites
        {'class_': 'article__content'},
        {'class_': 'article__body'},
    ]

    for selector in content_selectors:
        content = soup.find(['div', 'section'], **selector)
        if content:
            return str(content)

    # Strategy 4: Fall back to body content (without header/footer/nav which we removed)
    body = soup.find('body')
    if body:
        return str(body)

    # Last resort: return cleaned HTML
    return str(soup)


@dataclass
class EmbedPost:
    """Represents a detected social media embed in an article."""

    post_url: str
    author_handle: str
    platform: str
    article_url: str
    article_title: str
    article_domain: str
    discovered_at: datetime = field(default_factory=datetime.utcnow)
    article_published: Optional[datetime] = None
    article_summary: Optional[str] = None

    def to_sheet_row(self) -> list:
        """Convert to a row for Google Sheets."""
        # Only Bluesky posts can be reposted, others get "n/a"
        repost_status = "pending" if self.platform == "bluesky" else "n/a"
        return [
            self.discovered_at.strftime("%Y-%m-%d"),
            self.discovered_at.strftime("%H:%M:%S"),
            self.platform,
            self.article_domain,
            self.author_handle,
            self.article_url,
            self.post_url,
            self.article_title,
            self.article_summary or "",
            self.article_published.strftime("%Y-%m-%d %H:%M") if self.article_published else "",
            repost_status,
        ]

    @staticmethod
    def sheet_headers() -> list:
        """Return headers for the Google Sheet."""
        return [
            "Date",
            "Time",
            "Platform",
            "Domain",
            "Author Handle",
            "Article URL",
            "Post URL",
            "Article Title",
            "Article Summary",
            "Published Date",
            "Repost Status",
        ]

    def __hash__(self):
        """Hash based on post URL for deduplication."""
        return hash(self.post_url)

    def __eq__(self, other):
        """Compare based on post URL."""
        if not isinstance(other, EmbedPost):
            return False
        return self.post_url == other.post_url


class BasePlatform(ABC):
    """Abstract base class for social media platform detection."""

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the platform name (e.g., 'bluesky', 'twitter')."""
        pass

    @abstractmethod
    def detect_embeds(self, html: str, article_url: str) -> list[str]:
        """
        Detect embedded posts in HTML content.

        Args:
            html: The HTML content to search
            article_url: The URL of the article (for context)

        Returns:
            List of detected post URLs
        """
        pass

    @abstractmethod
    def extract_author(self, post_url: str) -> str:
        """
        Extract the author handle from a post URL.

        Args:
            post_url: The URL of the social media post

        Returns:
            The author's handle/username
        """
        pass

    def normalize_url(self, url: str) -> str:
        """
        Normalize a post URL to a canonical form.
        Override in subclasses for platform-specific normalization.

        Args:
            url: The raw URL

        Returns:
            Normalized URL
        """
        return url.strip()

    def create_embed(
        self,
        post_url: str,
        article_url: str,
        article_title: str,
        article_published: Optional[datetime] = None,
        article_summary: Optional[str] = None,
    ) -> EmbedPost:
        """
        Create an EmbedPost object for a detected embed.

        Args:
            post_url: URL of the embedded post
            article_url: URL of the article containing the embed
            article_title: Title of the article
            article_published: When the article was published
            article_summary: Brief summary of the article

        Returns:
            EmbedPost object
        """
        parsed = urlparse(article_url)
        domain = parsed.netloc.replace("www.", "")

        return EmbedPost(
            post_url=self.normalize_url(post_url),
            author_handle=self.extract_author(post_url),
            platform=self.name,
            article_url=article_url,
            article_title=article_title,
            article_domain=domain,
            article_published=article_published,
            article_summary=article_summary,
        )

    def process_article(
        self,
        html: str,
        article_url: str,
        article_title: str,
        article_published: Optional[datetime] = None,
        article_summary: Optional[str] = None,
    ) -> list[EmbedPost]:
        """
        Process an article and return all detected embeds.

        Args:
            html: HTML content of the article
            article_url: URL of the article
            article_title: Title of the article
            article_published: When the article was published
            article_summary: Brief summary of the article

        Returns:
            List of EmbedPost objects
        """
        embeds = []
        seen_urls = set()

        try:
            # Extract just the article content, excluding headers/footers/sidebars
            article_html = extract_article_content(html)
            post_urls = self.detect_embeds(article_html, article_url)

            for post_url in post_urls:
                normalized = self.normalize_url(post_url)

                if normalized in seen_urls:
                    continue
                seen_urls.add(normalized)

                try:
                    embed = self.create_embed(
                        post_url=normalized,
                        article_url=article_url,
                        article_title=article_title,
                        article_published=article_published,
                        article_summary=article_summary,
                    )
                    embeds.append(embed)
                    self.logger.debug(f"Found {self.name} embed: {normalized}")
                except Exception as e:
                    self.logger.warning(f"Failed to create embed for {post_url}: {e}")

        except Exception as e:
            self.logger.error(f"Error detecting {self.name} embeds in {article_url}: {e}")

        return embeds
