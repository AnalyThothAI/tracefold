from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from tracefold.news import (
    NewsFeedEntry,
    NewsFeedExpectedError,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
)
from tracefold.news.identity import collapse_javascript_whitespace, javascript_trim, utf16_length, utf16_slice
from tracefold.platform.validation import require_positive_float, require_positive_int

MAX_ENTRIES_PER_FEED: Final = 5
MAX_DECODED_BODY_BYTES: Final = 5_000_000
MAX_RESULT_DESCRIPTION_CHARS: Final = 400
MIN_RESULT_DESCRIPTION_CHARS: Final = 40
FUTURE_DATE_TOLERANCE_MS: Final = 60 * 60 * 1000
MAX_REDIRECTS: Final = 2

_HTTP_TOTAL_SECONDS = 20.0
_ITEM_RE = re.compile(r"<item[\s>]([\s\S]*?)</item>", re.IGNORECASE)
_ENTRY_RE = re.compile(r"<entry[\s>]([\s\S]*?)</entry>", re.IGNORECASE)
_XML_ENCODING_RE = re.compile(rb"<\?xml[^>]+encoding=[\"']([^\"']+)[\"']", re.IGNORECASE)
_NON_PUBLIC_HOST_SUFFIXES = (
    ".arpa",
    ".example",
    ".home",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localhost",
    ".test",
)
_PUBLIC_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_REDIRECT_STATUS_CODES = frozenset((301, 302, 303, 307, 308))


@dataclass(frozen=True, slots=True)
class NewsFeedWire:
    status_code: int
    source_name: str
    source_lang: str
    body: bytes
    etag: str | None
    last_modified: str | None
    not_modified: bool


@dataclass(slots=True)
class _FetchBudget:
    deadline_at: float

    def remaining(self) -> float:
        remaining = self.deadline_at - time.monotonic()
        if remaining <= 0:
            raise NewsFeedAcquisitionError("news_rss_total_timeout")
        return remaining


class RssFeedReader(NewsFeedReader):
    """Fetch one code-owned HTTPS feed within fixed request, byte, and time bounds."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        user_agent: str = "Tracefold/0.1 public RSS reader",
        max_attempts: int = 2,
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._timeout_seconds = require_positive_float(
            timeout_seconds,
            error_code="news_rss_timeout_seconds_required",
        )
        self._max_attempts = require_positive_int(
            max_attempts,
            error_code="news_rss_max_attempts_required",
        )
        if self._max_attempts > 2:
            raise ValueError("news_rss_max_attempts_exceeded")
        self._resolver = resolver or _resolve_host_addresses
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=False,
            transport=transport,
        )

    def fetch_wire(
        self,
        *,
        source: NewsSourceDefinition,
        etag: str | None,
        last_modified: str | None,
    ) -> NewsFeedWire:
        if source.source_kind != "rss" or not source.feed_url:
            raise ValueError("news_rss_source_required")
        if not is_public_https_feed_url(source.feed_url):
            raise ValueError("news_rss_feed_url_not_public_https")

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        budget = _FetchBudget(deadline_at=time.monotonic() + _HTTP_TOTAL_SECONDS)

        last_error: NewsFeedAcquisitionError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._bounded_get(source.feed_url, headers=headers, budget=budget)
            except NewsFeedAcquisitionError as exc:
                last_error = exc
            except httpx.TimeoutException:
                last_error = NewsFeedAcquisitionError("news_rss_timeout")
            except httpx.HTTPError as exc:
                last_error = NewsFeedAcquisitionError(
                    f"news_rss_protocol_{type(exc).__name__}",
                )
            else:
                response_etag = _optional_header(response.headers, "etag") or etag
                response_last_modified = _optional_header(response.headers, "last-modified") or last_modified
                if response.status_code == 304:
                    return NewsFeedWire(
                        status_code=304,
                        source_name=source.name,
                        source_lang=source.lang,
                        body=b"",
                        etag=response_etag,
                        last_modified=response_last_modified,
                        not_modified=True,
                    )
                if 200 <= response.status_code < 300:
                    if looks_like_rss_xml(response.text):
                        return NewsFeedWire(
                            status_code=int(response.status_code),
                            source_name=source.name,
                            source_lang=source.lang,
                            body=bytes(response.content),
                            etag=response_etag,
                            last_modified=response_last_modified,
                            not_modified=False,
                        )
                    last_error = NewsFeedAcquisitionError(
                        "news_rss_non_feed_response",
                        status_code=int(response.status_code),
                    )
                else:
                    last_error = NewsFeedAcquisitionError(
                        f"news_rss_http_{response.status_code}",
                        status_code=int(response.status_code),
                    )
            if attempt < self._max_attempts:
                budget.remaining()

        if last_error is None:
            raise RuntimeError("news_rss_fetch_retry_unreachable")
        raise last_error

    def close(self) -> None:
        self._client.close()

    def _bounded_get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        budget: _FetchBudget,
    ) -> httpx.Response:
        request_url = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            self._require_public_resolution(request_url)
            timeout_seconds = min(self._timeout_seconds, budget.remaining())
            timeout = httpx.Timeout(
                timeout_seconds,
                connect=min(5.0, timeout_seconds),
                read=timeout_seconds,
                write=min(5.0, timeout_seconds),
                pool=min(5.0, timeout_seconds),
            )
            with self._client.stream("GET", request_url, headers=dict(headers), timeout=timeout) as response:
                if response.status_code in _REDIRECT_STATUS_CODES and response.headers.get("location"):
                    if redirect_count == MAX_REDIRECTS:
                        raise NewsFeedAcquisitionError(
                            "news_rss_redirect_limit",
                            status_code=int(response.status_code),
                        )
                    try:
                        redirect_url = urljoin(str(response.url), response.headers["location"])
                    except ValueError as exc:
                        raise NewsFeedAcquisitionError(
                            "news_rss_redirect_not_public_https",
                            status_code=int(response.status_code),
                        ) from exc
                    if not is_public_https_feed_url(redirect_url):
                        raise NewsFeedAcquisitionError(
                            "news_rss_redirect_not_public_https",
                            status_code=int(response.status_code),
                        )
                    request_url = redirect_url
                    budget.remaining()
                    continue

                body = bytearray()
                for chunk in response.iter_bytes():
                    budget.remaining()
                    if len(body) + len(chunk) > MAX_DECODED_BODY_BYTES:
                        raise NewsFeedAcquisitionError(
                            "news_rss_body_oversized",
                            status_code=int(response.status_code),
                        )
                    body.extend(chunk)
                budget.remaining()
                decoded_headers = {
                    name: value
                    for name, value in response.headers.multi_items()
                    if name.casefold() not in {"content-encoding", "content-length", "transfer-encoding"}
                }
                return httpx.Response(
                    status_code=response.status_code,
                    headers=decoded_headers,
                    content=bytes(body),
                    request=response.request,
                )
        raise RuntimeError("news_rss_redirect_loop_unreachable")

    def _require_public_resolution(self, url: str) -> None:
        hostname = urlsplit(url).hostname
        if hostname is None:
            raise NewsFeedAcquisitionError("news_rss_host_resolution_failed")
        try:
            addresses = tuple(self._resolver(hostname))
        except OSError as exc:
            raise NewsFeedAcquisitionError("news_rss_host_resolution_failed") from exc
        if not addresses:
            raise NewsFeedAcquisitionError("news_rss_host_resolution_failed")
        try:
            has_non_public_address = any(not ipaddress.ip_address(address).is_global for address in addresses)
        except ValueError as exc:
            raise NewsFeedAcquisitionError("news_rss_host_resolution_failed") from exc
        if has_non_public_address:
            raise NewsFeedAcquisitionError("news_rss_resolved_address_not_public")


class NewsFeedAcquisitionError(NewsFeedExpectedError):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.code = str(code)
        self.status_code = status_code
        super().__init__(self.code)


def _resolve_host_addresses(hostname: str) -> tuple[str, ...]:
    return tuple(str(address[0]) for *_prefix, address in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM))


def parse_rss_feed_wire(wire: NewsFeedWire, *, now_ms: int | None = None) -> NewsFeedFetch:
    """Port the pinned first-five-before-validation RSS/Atom parser."""

    if wire.not_modified:
        return NewsFeedFetch(
            status_code=wire.status_code,
            etag=wire.etag,
            last_modified=wire.last_modified,
            not_modified=True,
        )
    text = _decode_xml(wire.body)
    if not looks_like_rss_xml(text):
        raise NewsFeedAcquisitionError(
            "news_rss_non_feed_response",
            status_code=wire.status_code,
        )

    matches = list(_ITEM_RE.finditer(text))
    is_atom = not matches
    if is_atom:
        matches = list(_ENTRY_RE.finditer(text))
    if not matches:
        raise NewsFeedAcquisitionError(
            "news_rss_parse_no_entries",
            status_code=wire.status_code,
        )

    observed_at_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    gates: Counter[str] = Counter()
    gates["per_feed_cap"] = max(0, len(matches) - MAX_ENTRIES_PER_FEED)
    entries: list[NewsFeedEntry] = []
    entries_seen = 0
    for match in matches[:MAX_ENTRIES_PER_FEED]:
        block = match.group(1)
        title = _extract_tag(block, "title")
        if not title:
            gates["missing_title"] += 1
            continue
        entries_seen += 1

        link = _atom_link(block) if is_atom else _extract_tag(block, "link")
        if link and not _is_http_link(link):
            gates["non_http_link"] += 1
            link = ""

        date_text = _extract_first_date_tag(block, is_atom=is_atom)
        if not date_text:
            gates["missing_date"] += 1
            continue
        published_at_ms = _parse_date_ms(date_text)
        if published_at_ms is None:
            gates["invalid_date"] += 1
            continue
        if published_at_ms > observed_at_ms + FUTURE_DATE_TOLERANCE_MS:
            gates["future_date"] += 1
            continue

        description = _extract_description(block, is_atom=is_atom, title=title)
        guid = _extract_tag(block, "guid") or _extract_tag(block, "id") or None
        entries.append(
            NewsFeedEntry(
                guid=guid,
                link=link or None,
                title=title,
                description=description,
                published_at_ms=published_at_ms,
                language=wire.source_lang,
                reporting_origin=wire.source_name,
            )
        )

    if entries_seen == 0:
        raise NewsFeedAcquisitionError(
            "news_rss_parse_no_entries",
            status_code=wire.status_code,
        )
    gates["undated"] = gates["missing_date"] + gates["invalid_date"] + gates["future_date"]
    return NewsFeedFetch(
        status_code=wire.status_code,
        entries=tuple(entries),
        entries_seen=entries_seen,
        gate_counts=dict(sorted(gates.items())),
        etag=wire.etag,
        last_modified=wire.last_modified,
        not_modified=False,
    )


def looks_like_rss_xml(value: str) -> bool:
    head = value[:2048].lower()
    if re.search(r"<!doctype\s+html|<html[\s>]", head):
        return False
    return bool(re.search(r"<rss[\s>]|<feed[\s>]|<rdf:rdf[\s>]", head))


def is_public_https_feed_url(value: object) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or hostname.endswith(".")
    ):
        return False
    normalized = hostname.casefold()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            normalized = normalized.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if "." not in normalized or normalized == "localhost":
            return False
        if normalized.endswith(_NON_PUBLIC_HOST_SUFFIXES):
            return False
        return all(_PUBLIC_HOST_LABEL.fullmatch(label) for label in normalized.split("."))
    return address.is_global


def _decode_xml(body: bytes) -> str:
    encoding_match = _XML_ENCODING_RE.search(body[:256])
    encodings = [encoding_match.group(1).decode("ascii", errors="ignore")] if encoding_match else []
    encodings.extend(("utf-8-sig", "utf-8"))
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _extract_first_date_tag(block: str, *, is_atom: bool) -> str:
    tags = (
        ("published", "updated", "dc:date", "dc:Date.Issued")
        if is_atom
        else ("pubDate", "dc:date", "dc:Date.Issued", "published")
    )
    for tag in tags:
        value = _extract_tag(block, tag)
        if value:
            return value
    return ""


def _extract_tag(xml: str, tag: str) -> str:
    escaped_tag = re.escape(tag)
    cdata_match = re.search(
        rf"<{escaped_tag}[^>]*>\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*</{escaped_tag}>",
        xml,
        re.IGNORECASE,
    )
    if cdata_match:
        return javascript_trim(cdata_match.group(1))
    plain_match = re.search(
        rf"<{escaped_tag}[^>]*>([^<]*)</{escaped_tag}>",
        xml,
        re.IGNORECASE,
    )
    return _decode_xml_entities(javascript_trim(plain_match.group(1))) if plain_match else ""


def _atom_link(block: str) -> str:
    match = re.search(r"<link[^>]+href=[\"']([^\"']+)[\"']", block, re.IGNORECASE)
    return match.group(1) if match else ""


def _extract_description(block: str, *, is_atom: bool, title: str) -> str:
    tags = ("summary", "content") if is_atom else ("description", "content:encoded")
    best = ""
    for tag in tags:
        raw = _extract_raw_tag_body(block, tag)
        if not raw:
            continue
        cleaned = re.sub(r"<[^>]+>", " ", _decode_xml_entities(raw))
        cleaned = collapse_javascript_whitespace(cleaned)
        if utf16_length(cleaned) > utf16_length(best):
            best = cleaned
    if utf16_length(best) < MIN_RESULT_DESCRIPTION_CHARS:
        return ""
    if _normalize_description(best) == _normalize_description(title):
        return ""
    return utf16_slice(best, MAX_RESULT_DESCRIPTION_CHARS)


def _extract_raw_tag_body(xml: str, tag: str) -> str:
    escaped_tag = re.escape(tag)
    cdata_match = re.search(
        rf"<{escaped_tag}[^>]*>\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*</{escaped_tag}>",
        xml,
        re.IGNORECASE,
    )
    if cdata_match:
        return cdata_match.group(1)
    plain_match = re.search(
        rf"<{escaped_tag}[^>]*>([\s\S]*?)</{escaped_tag}>",
        xml,
        re.IGNORECASE,
    )
    return plain_match.group(1) if plain_match else ""


def _normalize_description(value: str) -> str:
    return collapse_javascript_whitespace(value.lower())


def _decode_xml_entities(value: str) -> str:
    def decimal(match: re.Match[str]) -> str:
        return _decode_numeric_reference(int(match.group(1), 10))

    def hexadecimal(match: re.Match[str]) -> str:
        return _decode_numeric_reference(int(match.group(1), 16))

    decoded = value.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
    decoded = re.sub(r"&#(\d+);", decimal, decoded)
    decoded = re.sub(r"&#x([0-9a-fA-F]+);", hexadecimal, decoded)
    return decoded.replace("&amp;", "&")


def _decode_numeric_reference(code_point: int) -> str:
    if not 0 <= code_point <= 0x10FFFF:
        return ""
    try:
        return chr(code_point)
    except ValueError:
        return ""


def _parse_date_ms(value: str) -> int | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        iso = value.strip()
        if iso.endswith(("Z", "z")):
            iso = f"{iso[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(iso)
        except (ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _is_http_link(value: str) -> bool:
    return bool(re.match(r"^https?://", value, re.IGNORECASE))


def _optional_header(headers: httpx.Headers, name: str) -> str | None:
    normalized = str(headers.get(name) or "").strip()
    return normalized or None


__all__ = [
    "FUTURE_DATE_TOLERANCE_MS",
    "MAX_DECODED_BODY_BYTES",
    "MAX_ENTRIES_PER_FEED",
    "NewsFeedAcquisitionError",
    "NewsFeedWire",
    "RssFeedReader",
    "is_public_https_feed_url",
    "looks_like_rss_xml",
    "parse_rss_feed_wire",
]
