from __future__ import annotations

import httpx

from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.news import NewsBriefStory


def test_world_brief_provider_chain_calls_each_provider_at_most_once() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"lead":"全球政策变化值得关注 [1]",'
                                '"lines":[{"n":1,"text":"主要央行回应政策冲击 [1]"}]}'
                            )
                        }
                    }
                ]
            },
        )

    publisher = ProviderChainNewsBriefPublisher(
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="secret",
        configured_model="deepseek-chat",
        ollama_base_url="https://ollama.test/v1",
        ollama_model="local",
        openrouter_base_url="",
        openrouter_model="",
        openrouter_api_key=None,
        groq_base_url="",
        groq_model="",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        draft = publisher.publish(
            [
                NewsBriefStory(
                    story_id="story-1",
                    title="Central banks respond to a policy shock",
                    source="Reuters",
                    url="https://example.test/1",
                    source_count=2,
                    importance_score=88,
                    level="high",
                    category="economic",
                )
            ]
        )
    finally:
        publisher.close()

    assert calls == ["ollama.test", "deepseek.test"]
    assert draft.provider == "deepseek"
    assert draft.lines == ("主要央行回应政策冲击 [1]",)


def test_world_brief_provider_chain_degrades_malformed_provider_response() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "ollama.test":
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"lead":"全球政策变化值得关注 [1]",'
                                '"lines":[{"n":1,"text":"主要央行回应政策冲击 [1]"}]}'
                            )
                        }
                    }
                ]
            },
        )

    publisher = ProviderChainNewsBriefPublisher(
        configured_base_url="https://deepseek.test/v1",
        configured_api_key="secret",
        configured_model="deepseek-chat",
        ollama_base_url="https://ollama.test/v1",
        ollama_model="local",
        openrouter_base_url="",
        openrouter_model="",
        openrouter_api_key=None,
        groq_base_url="",
        groq_model="",
        groq_api_key=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        draft = publisher.publish(
            [
                NewsBriefStory(
                    story_id="story-1",
                    title="Central banks respond to a policy shock",
                    source="Reuters",
                    url="https://example.test/1",
                    source_count=2,
                    importance_score=88,
                    level="high",
                    category="economic",
                )
            ]
        )
    finally:
        publisher.close()

    assert calls == ["ollama.test", "deepseek.test"]
    assert draft.provider == "deepseek"
