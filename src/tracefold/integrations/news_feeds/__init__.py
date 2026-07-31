"""True-external RSS adapter for the Tracefold News deep module."""

from .rss import NewsFeedWire, RssFeedReader, is_public_https_feed_url, parse_rss_feed_wire

__all__ = ["NewsFeedWire", "RssFeedReader", "is_public_https_feed_url", "parse_rss_feed_wire"]
