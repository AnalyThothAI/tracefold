from __future__ import annotations

import json

import httpx

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
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.host, body))
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
        return _response(_valid_l1(), model="deepseek/deepseek-v4-flash")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        openrouter_api_key="openrouter-secret",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = publisher.publish((_story(),), date_iso="2026-08-07")
    finally:
        publisher.close()

    assert [host for host, _body in requests] == ["ollama.test", "openrouter.ai"]
    assert result.brief_kind == "l1"
    assert result.provider == "openrouter"
    assert requests[0][1]["model"] == "llama3.1:8b"
    assert requests[0][1]["think"] is False
    assert requests[0][1]["max_tokens"] == 900
    assert requests[1][1]["model"] == "deepseek/deepseek-v4-flash"
    assert requests[1][1]["reasoning"] == {"enabled": False}


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
        openrouter_api_key=None,
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
        openrouter_api_key=None,
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


def test_unreachable_retry_after_fails_over_without_sleeping() -> None:
    hosts: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(429, headers={"Retry-After": "7"})
        return _response(_valid_l1(), model="deepseek/deepseek-v4-flash")

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        openrouter_api_key="openrouter-secret",
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

    assert hosts == ["ollama.test", "openrouter.ai"]
    assert sleeps == []
    assert result.brief_kind == "l1"
    assert result.provider == "openrouter"


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
        openrouter_api_key=None,
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


def test_no_eligible_cluster_and_provider_exhaustion_are_normal_degraded_results() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    publisher = ProviderChainNewsBriefPublisher(
        ollama_base_url="https://ollama.test/v1",
        openrouter_api_key=None,
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
