from __future__ import annotations

import httpx

from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.news import NewsBriefStory, NewsSourceDefinition
from tracefold.platform.config.settings import NewsWorldBriefWorkerSettings


def source() -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id="example",
        name="Example",
        feed_url="https://example.test/rss",
        tier=2,
        memberships=("economic",),
    )


def test_world_brief_worker_settings_owns_a_bounded_retry_budget() -> None:
    settings = NewsWorldBriefWorkerSettings()
    assert settings.max_attempts == 3


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
    assert fetched.fetch_path == "direct"
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


def test_rss_reader_falls_back_to_allowlisted_relay_after_direct_403() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "example.test":
            return httpx.Response(403, text="<html>blocked</html>")
        return httpx.Response(
            200,
            content=b"""
            <rss version="2.0"><channel><item>
              <guid>relay-story</guid>
              <link>https://example.test/relay-story</link>
              <title>Central bank announces emergency policy</title>
              <pubDate>Sun, 26 Jul 2026 09:00:00 GMT</pubDate>
            </item></channel></rss>
            """,
        )

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.test",
        relay_auth_token="secret",
        relay_allowed_urls={source().feed_url},
    )
    try:
        fetched = reader.fetch(
            source=source(),
            etag=None,
            last_modified=None,
        )
    finally:
        reader.close()

    assert [request.url.host for request in requests] == [
        "example.test",
        "relay.test",
    ]
    assert requests[1].url.params["url"] == source().feed_url
    assert requests[1].headers["x-relay-key"] == "secret"
    assert fetched.fetch_path == "relay"
    assert fetched.direct_error_code == "http_403"
    assert fetched.entries[0].guid == "relay-story"


def test_rss_reader_never_sends_an_unconfigured_url_or_secret_to_relay() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, text="<html>blocked</html>")

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.test",
        relay_auth_token="do-not-log",
        relay_allowed_urls={"https://configured.test/rss"},
    )
    try:
        try:
            reader.fetch(source=source(), etag=None, last_modified=None)
        except RuntimeError as exc:
            assert str(exc) == "news_rss_relay_source_not_allowed"
            assert "do-not-log" not in str(exc)
        else:
            raise AssertionError("unconfigured relay target was accepted")
    finally:
        reader.close()

    assert [request.url.host for request in requests] == ["example.test"]


def test_rss_reader_records_both_failed_when_relay_returns_html() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.test":
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, text="<html>challenge</html>")

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.test",
        relay_auth_token="do-not-log",
        relay_allowed_urls={source().feed_url},
    )
    try:
        try:
            reader.fetch(source=source(), etag=None, last_modified=None)
        except RuntimeError as exc:
            assert str(exc) == "news_rss_relay_non_feed_response"
            assert exc.fetch_path == "relay"
            assert exc.direct_error_code == "http_429"
            assert "do-not-log" not in str(exc)
        else:
            raise AssertionError("relay HTML was accepted as RSS")
    finally:
        reader.close()
