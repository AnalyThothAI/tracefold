"""True-external RSS adapter for the Tracefold News deep module."""

from .rss import RssFeedReader, is_public_https_feed_url

__all__ = ["RssFeedReader", "is_public_https_feed_url"]
