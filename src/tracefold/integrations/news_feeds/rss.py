from __future__ import annotations

import calendar
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
        parsed = feedparser.parse(response.content)
        if bool(getattr(parsed, "bozo", False)) and not getattr(parsed, "entries", None):
            exception = getattr(parsed, "bozo_exception", None)
            raise ValueError(f"news_rss_parse_failed:{type(exception).__name__}:{exception}")
        feed_language = _optional_text(_mapping_get(getattr(parsed, "feed", {}), "language"))
        entries = tuple(
            _entry(entry, feed_language=feed_language)
            for entry in getattr(parsed, "entries", ())
            if _optional_text(_mapping_get(entry, "title"))
        )
        return NewsFeedFetch(
            status_code=int(response.status_code),
            entries=entries,
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
    if title is None:
        raise ValueError("news_rss_entry_title_required")
    link = _optional_text(_mapping_get(entry, "link"))
    guid = _optional_text(_mapping_get(entry, "id")) or _optional_text(_mapping_get(entry, "guid"))
    summary = (
        _optional_text(_mapping_get(entry, "summary"))
        or _optional_text(_mapping_get(entry, "description"))
        or ""
    )
    published_at_ms = _published_at_ms(entry)
    language = _optional_text(_mapping_get(entry, "language")) or feed_language
    return NewsFeedEntry(
        guid=guid,
        link=link,
        title=title,
        summary=summary,
        published_at_ms=published_at_ms,
        language=language,
        raw={
            "guid": guid,
            "link": link,
            "title": title,
            "summary": summary,
            "published": _optional_text(_mapping_get(entry, "published")),
            "updated": _optional_text(_mapping_get(entry, "updated")),
        },
    )


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


__all__ = ["RssFeedReader"]
