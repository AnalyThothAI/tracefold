"""Bounded public RSS/Atom transport and pinned parser."""

from .rss import (
    NewsFeedAcquisitionError,
    NewsFeedWire,
    RssFeedReader,
    is_public_https_feed_url,
    looks_like_rss_xml,
    parse_rss_feed_wire,
)

__all__ = [
    "NewsFeedAcquisitionError",
    "NewsFeedWire",
    "RssFeedReader",
    "is_public_https_feed_url",
    "looks_like_rss_xml",
    "parse_rss_feed_wire",
]
