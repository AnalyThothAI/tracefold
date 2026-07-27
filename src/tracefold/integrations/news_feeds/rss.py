from __future__ import annotations

import calendar
import html
import re
import time
from collections.abc import Collection, Mapping
from typing import Any
from urllib.parse import urlsplit

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
        relay_base_url: str = "",
        relay_auth_header: str = "x-relay-key",
        relay_auth_token: str | None = None,
        relay_allowed_urls: Collection[str] = (),
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._max_attempts = require_positive_int(
            max_attempts,
            error_code="news_rss_max_attempts_required",
        )
        self._relay_base_url = str(relay_base_url or "").strip().rstrip("/")
        if self._relay_base_url:
            parsed_relay = urlsplit(self._relay_base_url)
            if parsed_relay.scheme not in {"http", "https"} or not parsed_relay.netloc:
                raise ValueError("news_rss_relay_url_invalid")
        self._relay_auth_header = str(relay_auth_header or "").strip().lower()
        self._relay_auth_token = str(relay_auth_token or "").strip() or None
        self._relay_allowed_urls = frozenset(str(url).strip() for url in relay_allowed_urls if str(url).strip())
        if self._relay_auth_token and not self._relay_auth_header:
            raise ValueError("news_rss_relay_auth_header_required")
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
        response, fetch_path, direct_error_code = self._fetch_response(
            source=source,
            headers=headers,
        )
        response_etag = response.headers.get("etag") or etag
        response_last_modified = response.headers.get("last-modified") or last_modified
        if response.status_code == 304:
            return NewsFeedFetch(
                status_code=304,
                fetch_path=fetch_path,
                direct_error_code=direct_error_code,
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
            raise NewsFeedAcquisitionError(
                f"news_rss_parse_failed:{type(exception).__name__}:{exception}",
                status_code=int(response.status_code),
                fetch_path=fetch_path,
                direct_error_code=direct_error_code,
            )
        feed_language = _optional_text(_mapping_get(getattr(parsed, "feed", {}), "language"))
        raw_entries = tuple(getattr(parsed, "entries", ()))
        capped_entries = raw_entries[:5]
        entries = tuple(_entry(entry, feed_language=feed_language) for entry in capped_entries)
        return NewsFeedFetch(
            status_code=int(response.status_code),
            fetch_path=fetch_path,
            direct_error_code=direct_error_code,
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

    def _fetch_response(
        self,
        *,
        source: NewsSourceDefinition,
        headers: Mapping[str, str],
    ) -> tuple[httpx.Response, str, str | None]:
        direct_error_code: str | None = None
        try:
            direct = self._get_with_retry(source.feed_url, headers=headers)
            if direct.status_code == 304:
                return direct, "direct", None
            if direct.status_code in {403, 429} or direct.status_code >= 500:
                direct_error_code = f"http_{direct.status_code}"
            elif direct.status_code >= 400:
                raise NewsFeedAcquisitionError(
                    "news_rss_direct_http_error",
                    status_code=int(direct.status_code),
                    fetch_path="direct",
                    direct_error_code=f"http_{direct.status_code}",
                )
            else:
                if _looks_like_rss(direct.text):
                    return direct, "direct", None
                direct_error_code = "non_feed_response"
        except httpx.TransportError as exc:
            direct_error_code = f"transport_{type(exc).__name__}"

        if not self._relay_base_url:
            raise NewsFeedAcquisitionError(
                "news_rss_direct_failed_no_relay",
                status_code=(int(direct.status_code) if "direct" in locals() and direct is not None else None),
                fetch_path="direct",
                direct_error_code=direct_error_code,
            )
        if source.feed_url not in self._relay_allowed_urls:
            raise NewsFeedAcquisitionError(
                "news_rss_relay_source_not_allowed",
                status_code=(int(direct.status_code) if "direct" in locals() and direct is not None else None),
                fetch_path="direct",
                direct_error_code=direct_error_code,
            )
        relay_headers: dict[str, str] = {}
        if self._relay_auth_token:
            relay_headers[self._relay_auth_header] = self._relay_auth_token
        relay_endpoint = (
            self._relay_base_url
            if urlsplit(self._relay_base_url).path.rstrip("/").endswith("/rss")
            else f"{self._relay_base_url}/rss"
        )
        try:
            relay = self._client.get(
                relay_endpoint,
                params={"url": source.feed_url},
                headers=relay_headers,
            )
            if relay.status_code == 304:
                return relay, "relay", direct_error_code
            relay.raise_for_status()
            if not _looks_like_rss(relay.text):
                raise NewsFeedAcquisitionError(
                    "news_rss_relay_non_feed_response",
                    status_code=int(relay.status_code),
                    fetch_path="relay",
                    direct_error_code=direct_error_code,
                )
            return relay, "relay", direct_error_code
        except NewsFeedAcquisitionError:
            raise
        except httpx.HTTPError as exc:
            raise NewsFeedAcquisitionError(
                f"news_rss_relay_failed:{type(exc).__name__}",
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                fetch_path="relay",
                direct_error_code=direct_error_code,
            ) from exc


class NewsFeedAcquisitionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,
        fetch_path: str,
        direct_error_code: str | None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.fetch_path = fetch_path
        self.direct_error_code = direct_error_code


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


__all__ = ["NewsFeedAcquisitionError", "RssFeedReader"]
