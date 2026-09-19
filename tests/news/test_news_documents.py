from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest

from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.integrations import news_documents as documents
from tracefold.integrations.news_documents import (
    NewsDocumentClient,
    PublicNetworkBackend,
    UnsafeDocumentURL,
    public_url,
)
from tracefold.news.evidence import DOCUMENT_MAX_BYTES
from tracefold.platform.observability import TelemetryRegistry


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/",
        "http://10.0.0.1",
        "http://user:pass@example.org",
        "file:///etc/passwd",
        "http://localhost",
        "http://example.org:22/",
        "http://224.0.0.1/",
        "http://example.org:invalid/",
    ],
)
def test_nonpublic_or_credentialed_urls_are_refused(url):
    with pytest.raises(UnsafeDocumentURL):
        public_url(url)


def run_read(handler, url="https://example.org/story"):
    async def run():
        finite = FiniteOperations(telemetry=TelemetryRegistry())
        client = NewsDocumentClient(finite_operations=finite, transport=httpx.MockTransport(handler))
        try:
            return await client.read(url)
        finally:
            await client.close()
            await finite.drain(timeout_seconds=3)
            finite.close()

    return asyncio.run(run())


def test_success_has_immutable_identity_and_real_availability():
    text = "Acme acquisition remains subject to approval. " * 12

    def response(request):
        assert "authorization" not in request.headers
        assert not request.headers.get("cookie")
        return httpx.Response(200, text=text, headers={"content-type": "text/plain"})

    a, b = run_read(response), run_read(response)
    assert a.status == b.status == "success"
    assert a.document_id == b.document_id
    assert a.extracted_text == text.strip()
    assert a.available_at_ms >= a.observed_at_ms > 0
    assert a.physical_requests == 1


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(429, headers={"retry-after": "120"}), "rate_limited"),
        (httpx.Response(200, headers={"content-type": "application/pdf"}), "unsupported"),
        (httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"}), "unsafe_url"),
        (
            httpx.Response(200, text="subscribe to continue " * 10, headers={"content-type": "text/plain"}),
            "unsupported",
        ),
        (
            httpx.Response(200, content=b"x" * (DOCUMENT_MAX_BYTES + 1), headers={"content-type": "text/plain"}),
            "response_too_large",
        ),
    ],
)
def test_expected_http_failures_are_bounded_receipts(response, status):
    receipt = run_read(lambda _: response)
    assert receipt.status == status
    assert receipt.physical_requests == 1
    assert receipt.document_id == ""


def test_html_extraction_uses_packaged_library():
    article = "Acme announced the acquisition on Friday. Regulatory approval is still pending. " * 20
    receipt = run_read(
        lambda _: httpx.Response(
            200,
            text=f"<html><body><article><h1>Acme acquisition</h1><p>{article}</p></article></body></html>",
            headers={"content-type": "text/html"},
        )
    )
    assert receipt.status == "success"
    assert "approval is still pending" in receipt.extracted_text


def test_dns_resolution_pins_public_ip_and_refuses_mixed_private_answer(monkeypatch):
    async def run():
        backend = PublicNetworkBackend()
        connected = []

        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        async def connect(host, port, **kwargs):
            connected.append(host)
            return object()

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", resolve)
        monkeypatch.setattr(backend.backend, "connect_tcp", connect)
        await backend.connect_tcp("example.org", 443)
        assert connected == ["93.184.216.34"]

        async def rebinding(*args, **kwargs):
            return await resolve() + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        monkeypatch.setattr(loop, "getaddrinfo", rebinding)
        with pytest.raises(UnsafeDocumentURL):
            await backend.connect_tcp("example.org", 443)
        assert connected == ["93.184.216.34"]

    asyncio.run(run())


def test_timeout_holds_physical_extraction_permit_until_thread_finishes(monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def extraction(*args):
        entered.set()
        release.wait(2)
        return "Current source document. " * 20

    monkeypatch.setattr(documents, "extract_document", extraction)
    monkeypatch.setattr(documents, "DOCUMENT_TIMEOUT_SECONDS", 0.08)

    async def run():
        finite = FiniteOperations(telemetry=TelemetryRegistry())
        client = NewsDocumentClient(
            finite_operations=finite,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, text="x" * 100, headers={"content-type": "text/plain"})
            ),
        )
        try:
            result = await client.read("https://example.org")
            assert entered.is_set() and result.status == "timeout"
            assert client.permits._value == 1
        finally:
            release.set()
            await client.close()
            await finite.drain(timeout_seconds=3)
            finite.close()
        assert client.permits._value == 2

    asyncio.run(run())


def test_programming_errors_and_cancellation_are_not_missing_material(monkeypatch):
    def defect(*args):
        raise TypeError("extractor defect")

    monkeypatch.setattr(documents, "extract_document", defect)
    with pytest.raises(TypeError, match="extractor defect"):
        run_read(lambda _: httpx.Response(200, text="x" * 100, headers={"content-type": "text/plain"}))

    async def cancelled(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run_read(cancelled)


def test_rate_limit_cools_down_without_another_physical_request():
    async def run():
        finite = FiniteOperations(telemetry=TelemetryRegistry())
        client = NewsDocumentClient(
            finite_operations=finite,
            transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "120"})),
        )
        try:
            first = await client.read("https://example.org/one")
            second = await client.read("https://example.org/two")
            assert first.status == second.status == "rate_limited"
            assert first.physical_requests == 1 and second.physical_requests == 0
        finally:
            await client.close()
            finite.close()

    asyncio.run(run())
