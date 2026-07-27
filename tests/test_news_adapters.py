from __future__ import annotations

import httpx

from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.news import NewsBriefStory, NewsSourceDefinition


def source() -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id="example",
        name="Example",
        feed_url="https://example.test/rss",
        reporting_origin="example",
        tier=2,
        category_hint="economic",
    )


def test_rss_reader_honors_conditionals_limits_first_five_and_cleans_description() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match") == "etag-1":
            return httpx.Response(304, headers={"etag": "etag-1"})
        items = "".join(
            f"""
            <item>
              <guid>story-{index}</guid>
              <link>https://example.test/story-{index}</link>
              <title>Policy response confirmed {index}</title>
              <source url="https://reuters.com">Reuters</source>
              <description>
                <![CDATA[<p>Officials published a formal and detailed policy response number {index}.</p>]]>
              </description>
              <pubDate>Sun, 26 Jul 2026 09:00:00 GMT</pubDate>
            </item>
            """
            for index in range(8)
        )
        return httpx.Response(
            200,
            headers={"etag": "etag-1"},
            content=f"<rss version='2.0'><channel>{items}</channel></rss>".encode(),
        )

    reader = RssFeedReader(transport=httpx.MockTransport(handler), max_attempts=1)
    try:
        fetched = reader.fetch(source=source(), etag=None, last_modified=None)
        not_modified = reader.fetch(source=source(), etag=fetched.etag, last_modified=None)
    finally:
        reader.close()

    assert len(fetched.entries) == 5
    assert fetched.entries_seen == 8
    assert fetched.gate_counts == {"per_feed_cap": 3}
    assert fetched.entries[0].reporting_origin == "reuters"
    assert fetched.entries[0].description == ("Officials published a formal and detailed policy response number 0.")
    assert not_modified.not_modified is True
    assert requests[1].headers["if-none-match"] == "etag-1"


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
