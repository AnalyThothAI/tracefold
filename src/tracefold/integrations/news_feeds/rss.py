from __future__ import annotations

import calendar
import html
import re
import time
from collections.abc import Mapping
from typing import Any

import feedparser
import httpx

from tracefold.news import (
    NewsFeedEntry,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
)
from tracefold.platform.validation import require_positive_float, require_positive_int


class RssFeedReader(NewsFeedReader):
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        user_agent: str = "Tracefold/0.1 RSS reader",
        max_attempts: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_attempts = require_positive_int(
            max_attempts,
            error_code="news_rss_max_attempts_required",
        )
        self._client = httpx.Client(
            timeout=require_positive_float(
                timeout_seconds,
                error_code="news_rss_timeout_seconds_required",
            ),
            headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            },
            follow_redirects=True,
            transport=transport,
        )

    def fetch(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedFetch:
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        response = self._get_with_retry(source.feed_url, headers=headers)
        response_etag = response.headers.get("etag") or etag
        response_last_modified = response.headers.get("last-modified") or last_modified
        if response.status_code == 304:
            return NewsFeedFetch(
                status_code=304,
                etag=response_etag,
                last_modified=response_last_modified,
                not_modified=True,
            )
        response.raise_for_status()
        if not _looks_like_rss(response.text):
            raise ValueError("news_rss_non_feed_response")
        parsed = feedparser.parse(response.content)
        if bool(getattr(parsed, "bozo", False)) and not getattr(parsed, "entries", None):
            exception = getattr(parsed, "bozo_exception", None)
            raise ValueError(f"news_rss_parse_failed:{type(exception).__name__}:{exception}")
        feed_language = _optional_text(_mapping_get(getattr(parsed, "feed", {}), "language"))
        raw_entries = tuple(getattr(parsed, "entries", ()))
        capped_entries = raw_entries[:5]
        entries = tuple(_entry(entry, feed_language=feed_language) for entry in capped_entries)
        return NewsFeedFetch(
            status_code=int(response.status_code),
            entries=entries,
            entries_seen=len(raw_entries),
            gate_counts={"per_feed_cap": max(0, len(raw_entries) - len(capped_entries))},
            etag=response_etag,
            last_modified=response_last_modified,
            not_modified=False,
        )

    def close(self) -> None:
        self._client.close()

    def _get_with_retry(self, url: str, *, headers: Mapping[str, str]) -> httpx.Response:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.get(url, headers=dict(headers))
            except httpx.TransportError:
                if attempt >= self._max_attempts:
                    raise
                continue
            if response.status_code >= 500 and attempt < self._max_attempts:
                continue
            return response
        raise RuntimeError("news_rss_retry_unreachable")


def _entry(entry: Any, *, feed_language: str | None) -> NewsFeedEntry:
    title = _optional_text(_mapping_get(entry, "title"))
    link = _optional_text(_mapping_get(entry, "link"))
    guid = _optional_text(_mapping_get(entry, "id")) or _optional_text(_mapping_get(entry, "guid"))
    description = _description(entry, title=title or "")
    published_at_ms = _published_at_ms(entry)
    language = _optional_text(_mapping_get(entry, "language")) or feed_language
    reporting_origin = _structured_reporting_origin(entry)
    return NewsFeedEntry(
        guid=guid,
        link=link,
        title=title,
        description=description,
        published_at_ms=published_at_ms,
        language=language,
        reporting_origin=reporting_origin,
        raw={
            "guid": guid,
            "link": link,
            "title": title,
            "description": description,
            "published": _optional_text(_mapping_get(entry, "published")),
            "updated": _optional_text(_mapping_get(entry, "updated")),
            "reporting_origin": reporting_origin,
        },
    )


def _structured_reporting_origin(entry: Any) -> str | None:
    """Read publisher attribution only from structured feed fields.

    Google News exposes ``source.title``; RSSHub and other relays may expose
    ``source`` or ``publisher``. Free-text title suffixes and descriptions are
    deliberately ignored because they are not trusted provenance.
    """

    source = _mapping_get(entry, "source")
    candidates = (
        _mapping_get(source, "title"),
        _mapping_get(source, "name"),
        source if isinstance(source, str) else None,
        _mapping_get(entry, "publisher"),
        _mapping_get(_mapping_get(entry, "publisher_detail"), "name"),
    )
    for candidate in candidates:
        normalized = _normalize_reporting_origin(candidate)
        if normalized:
            return normalized
    return None


def _normalize_reporting_origin(value: object) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    aliases = {
        "associated press": "ap",
        "ap news": "ap",
        "reuters": "reuters",
        "bbc news": "bbc",
        "the guardian": "the-guardian",
        "bloomberg": "bloomberg",
    }
    lowered = text.casefold()
    if lowered in aliases:
        return aliases[lowered]
    normalized = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return normalized or None


def _published_at_ms(entry: Any) -> int | None:
    for field_name in ("published_parsed", "updated_parsed", "created_parsed"):
        value = _mapping_get(entry, field_name)
        if isinstance(value, time.struct_time):
            return int(calendar.timegm(value) * 1000)
        if isinstance(value, tuple) and len(value) >= 9:
            return int(calendar.timegm(value) * 1000)
    return None


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _description(entry: Any, *, title: str) -> str:
    candidates: list[str] = []
    for field in ("description", "summary"):
        value = _optional_text(_mapping_get(entry, field))
        if value:
            candidates.append(value)
    content = _mapping_get(entry, "content")
    if isinstance(content, list | tuple):
        for item in content:
            value = _optional_text(_mapping_get(item, "value"))
            if value:
                candidates.append(value)
    cleaned = [_clean_description(value) for value in candidates]
    normalized_title = " ".join(title.lower().split())
    eligible = [value for value in cleaned if len(value) >= 40 and " ".join(value.lower().split()) != normalized_title]
    if not eligible:
        return ""
    return max(eligible, key=lambda value: (len(value), value))[:400].strip()


def _clean_description(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(without_tags).split())


def _looks_like_rss(value: str) -> bool:
    head = value[:2048].lower()
    if re.search(r"<!doctype\s+html|<html[\s>]", head):
        return False
    return bool(re.search(r"<rss[\s>]|<feed[\s>]|<rdf:rdf[\s>]", head))


__all__ = ["RssFeedReader"]
