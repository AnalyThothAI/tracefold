from __future__ import annotations

import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.news.models import NewsBriefStory


def _story(*, link: str | None = "https://example.test/1") -> NewsBriefStory:
    return NewsBriefStory(
        story_id="story-1",
        primary_title="Iran threatens to close Strait of Hormuz",
        primary_source="Reuters",
        primary_link=link,
        primary_published_at_ms=1_786_928_400_000,
        source_count=2,
        unique_source_count=2,
        sources=("Reuters", "AP News"),
        last_updated_ms=1_786_928_400_000,
        member_titles=("AP reports Iran threat against the Strait of Hormuz",),
        source_tier=1,
        upstream_importance_score=90,
        entity_corroboration=False,
        corroboration_source_count=0,
        importance_score=240,
        effective_importance_score=230,
        is_alert=True,
        threat_level="high",
        category="conflict",
    )


def _response(content: str, *, model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": content}}],
        },
    )


def _valid_l1() -> str:
    return json.dumps(
        {
            "lead": "Iran threatens to close the Strait of Hormuz as regional pressure builds [1].",
            "lines": [{"n": 1, "text": "Iran threatens to close the Strait of Hormuz [1]."}],
        }
    )


def test_l1_composer_rejection_advances_exact_public_provider_chain() -> None:
    requests: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.host, body, dict(request.headers)))
        if request.url.host == "ollama.test":
            return _response(
                json.dumps(
                    {
                        "lead": "President Macron says Iran may close the Strait of Hormuz very soon [1].",
                        "lines": [{"n": 1, "text": "Iran threatens to close the Strait of Hormuz [1]."}],
                    }
                ),
                model="llama3.1:8b",
            )
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert [host for host, _body, _headers in requests] == ["ollama.test", "deepseek.test"]
    assert result.brief_kind == "l1"
    assert result.provider == "deepseek"
    assert requests[0][1]["model"] == "llama3.1:8b"
    assert requests[0][1]["think"] is False
    assert requests[0][1]["max_tokens"] == 900
    assert requests[1][1]["model"] == "deepseek-chat"
    assert "reasoning" not in requests[1][1]
    assert requests[1][2]["authorization"] == "Bearer deepseek-secret"
    assert "http-referer" not in requests[1][2]
    assert "x-title" not in requests[1][2]


def test_public_provider_chain_is_strictly_ollama_then_deepseek_then_groq() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((str(request.url), str(body["model"])))
        if request.url.host != "api.groq.com":
            return httpx.Response(400)
        return _response(_valid_l1(), model="llama-3.3-70b-versatile")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1/",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key="groq-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert requests == [
        ("https://ollama.test/v1/chat/completions", "llama3.1:8b"),
        ("https://deepseek.test/v1/chat/completions", "deepseek-chat"),
        ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
    ]
    assert result.provider == "groq"


@pytest.mark.parametrize(
    "direct",
    [
        {"configured_api_key": "secret"},
        {"configured_base_url": "https://deepseek.test/v1"},
        {"configured_model": "deepseek-chat"},
    ],
)
def test_provider_chain_rejects_partial_direct_configuration(direct: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="news_brief_direct_configuration_incomplete"):
        ProviderChainNewsBriefPublisher(
            ollama_base_url="",
            groq_api_key=None,
            **direct,
        )


def test_l2_uses_remaining_chain_without_an_acceptor_and_stays_degraded() -> None:
    max_tokens: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        max_tokens.append(int(body["max_tokens"]))
        if body["max_tokens"] == 900:
            return _response(
                json.dumps(
                    {
                        "lead": "President Macron says Iran may close the Strait of Hormuz very soon [1].",
                        "lines": [{"n": 1, "text": "Iran threatens to close the Strait of Hormuz [1]."}],
                    }
                ),
                model="llama3.1:8b",
            )
        return _response(
            "President Macron says the Strait of Hormuz may close very soon.",
            model="llama3.1:8b",
        )

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert max_tokens == [900, 300]
    assert result.brief_kind == "l2"
    assert result.quality == "degraded"
    assert result.world_brief == "Iran threatens to close Strait of Hormuz"
    assert result.provider == "ollama+headline-fallback"
    assert result.brief_story_lines == ()
    assert result.validation["failure_code"] == "INSIGHTS_SYNTHESIS_GATE"


def test_same_provider_retries_twice_and_honors_retry_after() -> None:
    calls = 0
    now = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return _response(_valid_l1(), model="llama3.1:8b")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
        monotonic=monotonic,
        wall_clock=lambda: 0.0,
        sleep=sleep,
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert calls == 2
    assert sleeps == [3.0]
    assert result.brief_kind == "l1"
    assert result.provider == "ollama"


def test_nonfinite_retry_after_uses_base_backoff_like_javascript_number() -> None:
    calls = 0
    now = 0.0
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "Infinity"})
        return _response(_valid_l1(), model="llama3.1:8b")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
        monotonic=lambda: now,
        wall_clock=lambda: 0.0,
        sleep=sleep,
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert calls == 2
    assert sleeps == [1.0]
    assert result.brief_kind == "l1"


def test_invalid_utf8_provider_json_advances_to_the_next_provider() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(200, content=b"\xff", headers={"Content-Type": "application/json"})
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert hosts == ["ollama.test", "deepseek.test"]
    assert result.provider == "deepseek"


def test_provider_fetch_follows_redirects_and_accepts_only_2xx() -> None:
    redirect_hosts: list[str] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        redirect_hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(302, headers={"Location": "https://redirected.test/completions"})
        return _response(_valid_l1(), model="llama3.1:8b")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(redirect_handler),
    )
    try:
        redirected = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert redirect_hosts == ["ollama.test", "redirected.test"]
    assert redirected.brief_kind == "l1"

    status_hosts: list[str] = []

    def status_handler(request: httpx.Request) -> httpx.Response:
        status_hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(304)
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        transport=httpx.MockTransport(status_handler),
    )
    try:
        non_2xx = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert status_hosts == ["ollama.test", "deepseek.test"]
    assert non_2xx.provider == "deepseek"


def test_provider_json_rejects_nonstandard_nan_like_json_parse() -> None:
    hosts: list[str] = []
    invalid = (
        '{"model":"llama3.1:8b","choices":[{"message":{"content":'
        + json.dumps(_valid_l1())
        + '}}],"usage":{"total_tokens":NaN}}'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(200, content=invalid, headers={"Content-Type": "application/json"})
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert hosts == ["ollama.test", "deepseek.test"]
    assert result.provider == "deepseek"


def test_recursive_or_nul_provider_output_advances_the_public_chain() -> None:
    hosts: list[str] = []
    deeply_nested_index = ("[" * 1_200) + "1" + ("]" * 1_200)
    recursive_candidate = (
        '{"lead":"Iran threatens to close the Strait of Hormuz as regional pressure builds [1].",'
        '"lines":[{"n":' + deeply_nested_index + ',"text":"Iran threatens to close the Strait of Hormuz [1]."}]}'
    )

    def recursive_handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return _response(recursive_candidate, model="llama3.1:8b")
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        transport=httpx.MockTransport(recursive_handler),
    )
    try:
        recursive_result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert hosts == ["ollama.test", "deepseek.test"]
    assert recursive_result.provider == "deepseek"

    response_hosts: list[str] = []
    deeply_nested_payload = (
        '{"model":"llama3.1:8b","choices":[{"message":{"content":'
        + json.dumps(_valid_l1())
        + '}}],"extra":'
        + ("[" * 100_000)
        + "1"
        + ("]" * 100_000)
        + "}"
    ).encode()

    def response_handler(request: httpx.Request) -> httpx.Response:
        response_hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(200, content=deeply_nested_payload, headers={"Content-Type": "application/json"})
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        transport=httpx.MockTransport(response_handler),
    )
    try:
        response_result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert response_hosts == ["ollama.test", "deepseek.test"]
    assert response_result.provider == "deepseek"

    nul_hosts: list[str] = []

    def nul_handler(request: httpx.Request) -> httpx.Response:
        nul_hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return _response(_valid_l1() + "\x00", model="llama3.1:8b")
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        transport=httpx.MockTransport(nul_handler),
    )
    try:
        nul_result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert nul_hosts == ["ollama.test", "deepseek.test"]
    assert nul_result.provider == "deepseek"


def test_unreachable_retry_after_fails_over_without_sleeping() -> None:
    hosts: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(429, headers={"Retry-After": "7"})
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        total_timeout_seconds=12,
        transport=httpx.MockTransport(handler),
        monotonic=lambda: 0.0,
        wall_clock=lambda: 0.0,
        sleep=sleeps.append,
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert hosts == ["ollama.test", "deepseek.test"]
    assert sleeps == []
    assert result.brief_kind == "l1"
    assert result.provider == "deepseek"


def test_zero_retry_after_matches_pinned_date_fallback_and_preserves_next_provider() -> None:
    hosts: list[str] = []
    sleeps: list[float] = []
    now = 0.0

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(429, headers={"Retry-After": "0"})
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        total_timeout_seconds=5.5,
        transport=httpx.MockTransport(handler),
        monotonic=lambda: now,
        wall_clock=lambda: 0.0,
        sleep=sleeps.append,
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert hosts == ["ollama.test", "deepseek.test"]
    assert sleeps == []
    assert result.provider == "deepseek"


def test_retry_backoff_that_consumes_the_remainder_terminates_the_chain_after_sleep() -> None:
    hosts: list[str] = []
    sleeps: list[float] = []
    now = 0.0

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            raise httpx.ConnectError("offline", request=request)
        return _response(_valid_l1(), model="deepseek-chat")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="deepseek-secret",
        configured_model="deepseek-chat",
        groq_api_key=None,
        total_timeout_seconds=5.5,
        transport=httpx.MockTransport(handler),
        monotonic=monotonic,
        sleep=sleep,
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert hosts == ["ollama.test"]
    assert sleeps == [1.0]
    assert result.brief_kind == "none"
    assert result.validation["failure_code"] == "INSIGHTS_SYNTHESIS_PROVIDER"


def test_transport_valid_minimum_length_uses_javascript_utf16_units() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400)
        return _response("😀" * 10, model="llama3.1:8b")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert calls == 2
    assert result.brief_kind == "l2"


def test_provider_text_uses_javascript_newline_cleanup_and_scalar_conversion() -> None:
    candidates = iter(
        (
            httpx.Response(400),
            _response(
                "We need to explain\rIran threatens to close the Strait of Hormuz.",
                model="llama3.1:8b",
            ),
        )
    )
    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(lambda _request: next(candidates)),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert result.world_brief == "We need to explain\rIran threatens to close the Strait of Hormuz."

    surrogate_response = httpx.Response(
        200,
        content=json.dumps(
            {
                "model": "llama3.1:8b",
                "choices": [{"message": {"content": "\ud83d" * 20}}],
            },
            ensure_ascii=True,
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    surrogate_candidates = iter((httpx.Response(400), surrogate_response))
    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(lambda _request: next(surrogate_candidates)),
    )
    try:
        scalar_result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert scalar_result.brief_kind == "l2"
    assert scalar_result.world_brief == "\ufffd" * 20


def test_provider_cleanup_uses_javascript_ascii_case_insensitive_tags() -> None:
    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(
            lambda _request: _response(f"<thİnk>metadata\n{_valid_l1()}", model="llama3.1:8b")
        ),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert result.brief_kind == "l1"


def test_l2_receives_only_the_shared_sixty_second_budget_remainder() -> None:
    now = 0.0
    request_timeouts: list[float] = []
    max_tokens: list[int] = []

    def monotonic() -> float:
        return now

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal now
        body = json.loads(request.content)
        max_tokens.append(int(body["max_tokens"]))
        request_timeouts.append(float(request.extensions["timeout"]["read"]))
        if body["max_tokens"] == 900:
            now = 54.0
            return _response("This is not structured synthesis JSON at all.", model="llama3.1:8b")
        return _response(
            "Iran may close the Strait of Hormuz as regional pressure builds.",
            model="llama3.1:8b",
        )

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
        monotonic=monotonic,
        sleep=lambda _seconds: None,
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert max_tokens == [900, 300]
    assert request_timeouts == [25.0, 1.0]
    assert result.brief_kind == "l2"
    assert result.validation["failure_code"] == "INSIGHTS_SYNTHESIS_PARSE"


def test_provider_timeout_is_total_wall_clock_not_read_inactivity() -> None:
    body = json.dumps(
        {
            "model": "llama3.1:8b",
            "choices": [{"message": {"content": _valid_l1()}}],
        }
    ).encode()
    chunk_size = max(1, len(body) // 40)

    class _SlowDripHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(content_length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for offset in range(0, len(body), chunk_size):
                    self.wfile.write(body[offset : offset + chunk_size])
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowDripHandler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        groq_api_key=None,
        total_timeout_seconds=5.3,
        sleep=lambda _seconds: None,
    )
    started = time.monotonic()
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)

    assert result.brief_kind == "none"
    assert time.monotonic() - started < 1.25


def test_provider_timeout_does_not_wait_for_a_cancelled_dns_executor(monkeypatch) -> None:
    def slow_getaddrinfo(
        _host: str,
        port: int,
        _family: int = 0,
        _type: int = 0,
        _proto: int = 0,
        _flags: int = 0,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        time.sleep(1.5)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", int(port)))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="http://slow-dns.test:9/v1",
        groq_api_key=None,
        total_timeout_seconds=5.2,
        sleep=lambda _seconds: None,
    )
    started = time.monotonic()
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
        elapsed = time.monotonic() - started
    finally:
        publisher.close()

    assert result.brief_kind == "none"
    assert elapsed < 0.8


def test_no_eligible_cluster_and_provider_exhaustion_are_normal_degraded_results() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )
    try:
        ineligible = _story().model_copy(
            update={
                "unique_source_count": 1,
                "sources": ("Reuters",),
                "entity_corroboration": False,
            }
        )
        missing_cluster = publisher.publish((ineligible,), date_iso="2026-08-07")
        exhausted = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert calls == 2
    assert missing_cluster.brief_kind == "none"
    assert missing_cluster.validation["failure_code"] == "INSIGHTS_SYNTHESIS_MISSING_CLUSTER"
    assert exhausted.brief_kind == "none"
    assert exhausted.validation["failure_code"] == "INSIGHTS_SYNTHESIS_PROVIDER"
