"""One bounded public original-link read, without credentials or follow-on crawling."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpcore
import httpx
import trafilatura

from tracefold.news.evidence import (
    DOCUMENT_CONCURRENCY,
    DOCUMENT_MAX_BYTES,
    DOCUMENT_MAX_CHARS,
    DOCUMENT_REDIRECT_MAX,
    DOCUMENT_TIMEOUT_SECONDS,
    DocumentResult,
    document_identity,
    text_sha,
)

from .http_bounds import ResponseTooLarge, read_bounded

EXTRACTOR_VERSION = "trafilatura_2.0.0_precision_v1"


def retry_delay(value: str) -> float:
    if value.isdigit():
        return max(1, int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(1, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return 60


class UnsafeDocumentURL(ValueError):
    pass


def public_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise UnsafeDocumentURL("malformed_url") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or port not in {None, 80, 443}
        or any(ord(c) < 33 for c in value)
    ):
        raise UnsafeDocumentURL("unsafe_url")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "%" in host:
        raise UnsafeDocumentURL("unsafe_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global or address.is_multicast:
            raise UnsafeDocumentURL("nonpublic_address")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, reject *all* non-public answers, connect to the pinned IP.

    httpcore still negotiates TLS using the original hostname. A second DNS lookup
    cannot redirect a validated hostname to metadata/private services.
    """

    def __init__(self) -> None:
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore backend interface
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
        if not addresses or any(
            (not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast)
            for address in addresses
        ):
            raise UnsafeDocumentURL("nonpublic_dns_answer")
        return await self.backend.connect_tcp(
            addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self.stream:
            yield part

    async def aclose(self) -> None:
        await self.stream.aclose()


class PublicDocumentTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PublicNetworkBackend(),
            max_connections=DOCUMENT_CONCURRENCY,
            max_keepalive_connections=0,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.pool.aclose()


def extract_document(body: bytes, content_type: str, url: str) -> str | None:
    if content_type == "text/plain":
        return body.decode("utf-8", errors="replace").strip()
    return trafilatura.extract(
        body, url=url, favor_precision=True, include_comments=False, include_tables=True, fast=True
    )


class NewsDocumentClient:
    def __init__(self, *, finite_operations: Any, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.finite = finite_operations
        self.client = httpx.AsyncClient(
            transport=transport or PublicDocumentTransport(),
            trust_env=False,
            follow_redirects=False,
            timeout=DOCUMENT_TIMEOUT_SECONDS,
            headers={"User-Agent": "Tracefold-Evidence/1", "Accept-Encoding": "identity"},
        )
        self.permits = asyncio.Semaphore(DOCUMENT_CONCURRENCY)
        self.cooldowns: OrderedDict[str, float] = OrderedDict()

    async def close(self) -> None:
        await self.client.aclose()

    async def read(self, url: str) -> DocumentResult:
        start = time.monotonic()
        requests = 0
        acquired = False
        submitted = False
        final = url
        observed = int(time.time() * 1000)

        def receipt(status: str, **values: Any) -> DocumentResult:
            return DocumentResult(
                status=status,
                requested_url=url,
                final_url=final,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                physical_requests=requests,
                **values,
            )

        try:
            async with asyncio.timeout(DOCUMENT_TIMEOUT_SECONDS):
                normalized = public_url(url)
                final = normalized
                host = urlsplit(normalized).netloc
                if self.cooldowns.get(host, 0) > time.monotonic():
                    return receipt("rate_limited")
                await self.permits.acquire()
                acquired = True
                for redirect in range(DOCUMENT_REDIRECT_MAX + 1):
                    final = public_url(final)
                    if self.cooldowns.get(urlsplit(final).netloc, 0) > time.monotonic():
                        return receipt("rate_limited")
                    # Explicit empty Cookie also prevents a response cookie from travelling to the
                    # next URL; no auth/proxy/environment credentials are accepted by this client.
                    requests += 1
                    async with self.client.stream("GET", final, headers={"Cookie": ""}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or redirect == DOCUMENT_REDIRECT_MAX:
                                return receipt("redirect_limit")
                            final = public_url(urljoin(final, location))
                            continue
                        if response.status_code == 429:
                            retry_after = response.headers.get("retry-after", "")[:100]
                            delay = retry_delay(retry_after)
                            self.cooldowns[urlsplit(final).netloc] = time.monotonic() + max(1, delay)
                            while len(self.cooldowns) > 128:
                                self.cooldowns.popitem(last=False)
                            return receipt("rate_limited", retry_after=retry_after)
                        if response.status_code != 200:
                            return receipt("http_unavailable")
                        content_type = response.headers.get("content-type", "").split(";")[0].lower()
                        if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                            return receipt("unsupported")
                        # Refuse unsolicited compression rather than allowing an unbounded decoder allocation.
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            return receipt("unsupported_encoding")
                        body = await read_bounded(response, max_bytes=DOCUMENT_MAX_BYTES)
                    loop = asyncio.get_running_loop()

                    def submitted_extraction() -> None:
                        nonlocal submitted
                        submitted = True

                    def extract(
                        body: bytes = body,
                        content_type: str = content_type,
                        final: str = final,
                        loop: asyncio.AbstractEventLoop = loop,
                    ) -> str | None:
                        try:
                            return extract_document(body, content_type, final)
                        finally:
                            # The physical worker owns this permit after submission, including
                            # caller cancellation/timeout. The process-wide resource drains it.
                            loop.call_soon_threadsafe(self.permits.release)

                    text = await self.finite.run(
                        "news_document_extract",
                        extract,
                        timeout_seconds=DOCUMENT_TIMEOUT_SECONDS,
                        on_submitted=submitted_extraction,
                    )
                    if not text or len(text) < 80:
                        return receipt("extraction_failed")
                    if len(text) > DOCUMENT_MAX_CHARS:
                        return receipt("extracted_text_too_large")
                    if any(
                        marker in text.lower()
                        for marker in (
                            "verify you are human",
                            "enable javascript",
                            "subscribe to continue",
                            "sign in to continue",
                        )
                    ):
                        return receipt("unsupported")
                    response_sha = __import__("hashlib").sha256(body).hexdigest()
                    return receipt(
                        "success",
                        normalized_url=normalized,
                        response_sha256=response_sha,
                        extracted_text_sha256=text_sha(text),
                        extractor_version=EXTRACTOR_VERSION,
                        extracted_text=text,
                        document_id=document_identity(normalized, response_sha, EXTRACTOR_VERSION),
                        observed_at_ms=observed,
                        available_at_ms=int(time.time() * 1000),
                        content_type=content_type,
                    )
                raise AssertionError("redirect_loop_unreachable")
        except UnsafeDocumentURL:
            return receipt("unsafe_url")
        except (TimeoutError, httpx.TimeoutException, httpcore.TimeoutException):
            return receipt("timeout")
        except ResponseTooLarge:
            return receipt("response_too_large")
        except (httpx.TransportError, httpcore.NetworkError, httpcore.ProtocolError, socket.gaierror):
            return receipt("connection_failed")
        finally:
            if acquired and not submitted:
                self.permits.release()
