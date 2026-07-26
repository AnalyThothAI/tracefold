from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from collections.abc import Callable, Sequence
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from tracefold.news import NewsPageFetch, NewsPageReader
from tracefold.platform.validation import require_positive_float, require_positive_int

_EXTRACTOR_VERSION = "news_page_text_v1"
_SUPPORTED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_PAYWALL_MARKERS = (
    "subscribe to continue",
    "subscription required",
    "sign in to continue",
    "register to continue",
    "订阅后继续",
    "登录后继续",
)


class BoundedNewsPageReader(NewsPageReader):
    """Best-effort, robots-aware page enrichment with explicit safety bounds."""

    extractor_version = _EXTRACTOR_VERSION

    def __init__(
        self,
        *,
        timeout_seconds: float = 12.0,
        max_bytes: int = 512_000,
        user_agent: str = "Tracefold/0.1 News evidence reader",
        transport: httpx.BaseTransport | None = None,
        resolver: Callable[[str], Sequence[str]] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._max_bytes = require_positive_int(
            max_bytes,
            error_code="news_page_max_bytes_required",
        )
        self._user_agent = str(user_agent).strip()
        if not self._user_agent:
            raise ValueError("news_page_user_agent_required")
        self._resolver = resolver or _resolve_addresses
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._client = httpx.Client(
            timeout=require_positive_float(
                timeout_seconds,
                error_code="news_page_timeout_seconds_required",
            ),
            headers={
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
            },
            follow_redirects=False,
            transport=transport,
        )

    def fetch(self, *, url: str) -> NewsPageFetch:
        try:
            normalized = _safe_public_url(url, resolver=self._resolver)
        except ValueError as exc:
            return self._failure(url=url, reason=str(exc))
        try:
            if not self._robots_allows(normalized):
                return NewsPageFetch(
                    status="robots_denied",
                    fetched_at_ms=self._clock_ms(),
                    failure_reason="robots_denied",
                    final_url=normalized,
                )
            response, final_url, body, truncated = self._read_page(normalized)
        except Exception as exc:
            return self._failure(
                url=normalized,
                reason=f"{type(exc).__name__}:{_bounded(exc)}",
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if response.status_code in {401, 402, 403}:
            return NewsPageFetch(
                status="paywalled",
                fetched_at_ms=self._clock_ms(),
                http_status=response.status_code,
                content_type=content_type or None,
                byte_count=len(body),
                failure_reason=f"http_{response.status_code}",
                final_url=final_url,
            )
        if response.status_code >= 400:
            return NewsPageFetch(
                status="failed",
                fetched_at_ms=self._clock_ms(),
                http_status=response.status_code,
                content_type=content_type or None,
                byte_count=len(body),
                failure_reason=f"http_{response.status_code}",
                final_url=final_url,
            )
        if not any(content_type.startswith(value) for value in _SUPPORTED_CONTENT_TYPES):
            return NewsPageFetch(
                status="unsupported_content",
                fetched_at_ms=self._clock_ms(),
                http_status=response.status_code,
                content_type=content_type or None,
                byte_count=len(body),
                failure_reason="unsupported_content_type",
                final_url=final_url,
            )
        text = _extract_text(body, content_type=content_type)
        lowered = text.casefold()
        if any(marker in lowered for marker in _PAYWALL_MARKERS) and len(text) < 2_000:
            return NewsPageFetch(
                status="paywalled",
                fetched_at_ms=self._clock_ms(),
                http_status=response.status_code,
                content_type=content_type or None,
                byte_count=len(body),
                failure_reason="paywall_marker",
                final_url=final_url,
            )
        if not text:
            return NewsPageFetch(
                status="failed",
                fetched_at_ms=self._clock_ms(),
                http_status=response.status_code,
                content_type=content_type or None,
                byte_count=len(body),
                failure_reason="empty_extracted_text",
                final_url=final_url,
            )
        return NewsPageFetch(
            status="truncated" if truncated else "available",
            fetched_at_ms=self._clock_ms(),
            http_status=response.status_code,
            content_type=content_type or None,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            extracted_text=text,
            byte_count=len(body),
            failure_reason="byte_limit" if truncated else None,
            final_url=final_url,
        )

    def close(self) -> None:
        self._client.close()

    def _robots_allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        response = self._client.get(robots_url)
        if response.status_code in {401, 403}:
            return False
        if response.status_code == 404:
            return True
        response.raise_for_status()
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch(self._user_agent, url)

    def _read_page(self, url: str) -> tuple[httpx.Response, str, bytes, bool]:
        current = url
        for _ in range(4):
            with self._client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("news_page_redirect_location_missing")
                    current = _safe_public_url(
                        urljoin(current, location),
                        resolver=self._resolver,
                    )
                    continue
                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in response.iter_bytes():
                    remaining = self._max_bytes + 1 - size
                    if remaining <= 0:
                        truncated = True
                        break
                    bounded = chunk[:remaining]
                    chunks.append(bounded)
                    size += len(bounded)
                    if size > self._max_bytes:
                        truncated = True
                        break
                body = b"".join(chunks)[: self._max_bytes]
                return response, current, body, truncated
        raise ValueError("news_page_redirect_limit")

    def _failure(self, *, url: str, reason: str) -> NewsPageFetch:
        return NewsPageFetch(
            status="failed",
            fetched_at_ms=self._clock_ms(),
            failure_reason=_bounded(reason),
            final_url=str(url),
        )


class _ArticleTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        normalized = " ".join(data.split())
        if normalized:
            self._values.append(normalized)

    def text(self) -> str:
        return "\n".join(self._values)


def _extract_text(body: bytes, *, content_type: str) -> str:
    decoded = body.decode("utf-8", errors="replace")
    if content_type.startswith("text/plain"):
        return "\n".join(line.strip() for line in decoded.splitlines() if line.strip())
    parser = _ArticleTextExtractor()
    parser.feed(decoded)
    parser.close()
    return parser.text()


def _safe_public_url(
    value: str,
    *,
    resolver: Callable[[str], Sequence[str]],
) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("news_page_url_invalid")
    if parsed.username or parsed.password:
        raise ValueError("news_page_url_credentials_forbidden")
    addresses = tuple(resolver(parsed.hostname))
    if not addresses:
        raise ValueError("news_page_host_unresolved")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise ValueError("news_page_host_not_public")
    return parsed.geturl()


def _resolve_addresses(host: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)))


def _bounded(value: object) -> str:
    return " ".join(str(value).split())[:800] or "unknown_error"


__all__ = ["BoundedNewsPageReader"]
