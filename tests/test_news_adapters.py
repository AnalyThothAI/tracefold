from __future__ import annotations

import httpx
import pytest

from tracefold.integrations.news_ai import ProviderChainNewsBriefPublisher
from tracefold.integrations.news_feeds import (
    RssFeedReader,
    is_public_https_feed_url,
    parse_rss_feed_wire,
)
from tracefold.news import NewsBriefStory, NewsSourceDefinition


def source() -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id="example",
        name="Example",
        feed_url="https://feed.example.com/rss",
        tier=2,
        memberships=("economic",),
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
              <link>https://feed.example.com/story-{index}</link>
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
        fetched = parse_rss_feed_wire(reader.fetch_wire(source=source(), etag=None, last_modified=None))
        not_modified = parse_rss_feed_wire(reader.fetch_wire(source=source(), etag=fetched.etag, last_modified=None))
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


def test_rss_reader_preserves_wallstengine_quote_comment_as_title_only() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"""
            <rss version="2.0"><channel><item>
              <guid>wallstengine-quote</guid>
              <link>https://x.com/wallstengine/status/201</link>
              <title>Fed pricing now implies two cuts before year end</title>
              <description><![CDATA[
                Fed pricing now implies two cuts before year end
                <hr>
                Federal Reserve: The committee will remain data dependent
                while monitoring inflation and employment risks.
              ]]></description>
              <pubDate>Sun, 26 Jul 2026 09:00:00 GMT</pubDate>
            </item></channel></rss>
            """,
        )

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
    )
    wallstengine = NewsSourceDefinition(
        source_id="wallstengine",
        name="WallStEngine",
        feed_url=(
            "http://rsshub:1200/twitter/user/wallstengine/"
            "includeReplies=0&includeRts=0&showRetweetTextInTitle=1&showQuotedInTitle=0"
        ),
        tier=4,
        lang="en",
        memberships=("finance",),
    )
    try:
        fetched = parse_rss_feed_wire(
            reader.fetch_wire(
                source=wallstengine,
                etag=None,
                last_modified=None,
            )
        )
    finally:
        reader.close()

    assert len(fetched.entries) == 1
    assert fetched.entries[0].link == "https://x.com/wallstengine/status/201"
    assert fetched.entries[0].title == ("Fed pricing now implies two cuts before year end")
    assert "Federal Reserve" not in str(fetched.entries[0].title)
    assert "Federal Reserve" in fetched.entries[0].description


def test_rss_reader_falls_back_to_allowlisted_relay_after_direct_403() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "feed.example.com":
            return httpx.Response(403, text="<html>blocked</html>")
        return httpx.Response(
            200,
            content=b"""
            <rss version="2.0"><channel><item>
              <guid>relay-story</guid>
              <link>https://feed.example.com/relay-story</link>
              <title>Central bank announces emergency policy</title>
              <pubDate>Sun, 26 Jul 2026 09:00:00 GMT</pubDate>
            </item></channel></rss>
            """,
        )

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.example.com",
        relay_auth_token="secret",
        relay_allowed_urls={source().feed_url},
    )
    try:
        fetched = parse_rss_feed_wire(
            reader.fetch_wire(
                source=source(),
                etag=None,
                last_modified=None,
            )
        )
    finally:
        reader.close()

    assert [request.url.host for request in requests] == [
        "feed.example.com",
        "relay.example.com",
    ]
    assert requests[1].url.params["url"] == source().feed_url
    assert requests[1].headers["x-relay-key"] == "secret"
    assert fetched.fetch_path == "relay"
    assert fetched.direct_error_code == "http_403"
    assert fetched.entries[0].guid == "relay-story"


def test_rss_reader_translates_invalid_content_encoding_and_uses_relay() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "feed.example.com":
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                content=b"<rss version='2.0'><channel></channel></rss>",
            )
        return httpx.Response(
            200,
            content=b"""
            <rss version="2.0"><channel><item>
              <guid>relay-after-invalid-gzip</guid>
              <link>https://feed.example.com/relay-after-invalid-gzip</link>
              <title>Relay recovered the invalid encoded feed</title>
              <pubDate>Sun, 26 Jul 2026 09:00:00 GMT</pubDate>
            </item></channel></rss>
            """,
        )

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.example.com",
        relay_auth_token="secret",
        relay_allowed_urls={source().feed_url},
    )
    try:
        fetched = parse_rss_feed_wire(reader.fetch_wire(source=source(), etag=None, last_modified=None))
    finally:
        reader.close()

    assert [request.url.host for request in requests] == [
        "feed.example.com",
        "relay.example.com",
    ]
    assert fetched.fetch_path == "relay"
    assert fetched.direct_error_code == "protocol_DecodingError"
    assert fetched.entries[0].guid == "relay-after-invalid-gzip"


def test_rss_reader_applies_body_bound_to_each_attempt_not_cumulative_failures() -> None:
    large_failed_body = b"blocked" + (b"x" * 2_600_000)
    large_feed = (
        b"<rss version='2.0'><channel><item>"
        b"<guid>relay-large</guid>"
        b"<link>https://feed.example.com/relay-large</link>"
        b"<title>Central bank publishes a policy decision</title>"
        b"<pubDate>Sun, 26 Jul 2026 09:00:00 GMT</pubDate>"
        b"</item></channel>" + (b" " * 2_600_000) + b"</rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "feed.example.com":
            return httpx.Response(403, content=large_failed_body)
        return httpx.Response(200, content=large_feed)

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.example.com",
        relay_auth_token="secret",
        relay_allowed_urls={source().feed_url},
    )
    try:
        fetched = parse_rss_feed_wire(reader.fetch_wire(source=source(), etag=None, last_modified=None))
    finally:
        reader.close()

    assert fetched.fetch_path == "relay"
    assert fetched.entries[0].guid == "relay-large"


def test_rss_reader_never_sends_an_unconfigured_url_or_secret_to_relay() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(403, text="<html>blocked</html>")

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.example.com",
        relay_auth_token="do-not-log",
        relay_allowed_urls={"https://configured.example.com/rss"},
    )
    try:
        try:
            reader.fetch_wire(source=source(), etag=None, last_modified=None)
        except RuntimeError as exc:
            assert str(exc) == "news_rss_relay_source_not_allowed"
            assert "do-not-log" not in str(exc)
        else:
            raise AssertionError("unconfigured relay target was accepted")
    finally:
        reader.close()

    assert [request.url.host for request in requests] == ["feed.example.com"]


def test_rss_reader_records_both_failed_when_relay_returns_html() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "feed.example.com":
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, text="<html>challenge</html>")

    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.example.com",
        relay_auth_token="do-not-log",
        relay_allowed_urls={source().feed_url},
    )
    try:
        try:
            reader.fetch_wire(source=source(), etag=None, last_modified=None)
        except RuntimeError as exc:
            assert str(exc) == "news_rss_relay_non_feed_response"
            assert exc.fetch_path == "relay"
            assert exc.direct_error_code == "http_429"
            assert "do-not-log" not in str(exc)
        else:
            raise AssertionError("relay HTML was accepted as RSS")
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("url", "eligible"),
    [
        ("https://feed.example.com/rss", True),
        ("https://8.8.8.8/rss", True),
        ("http://feed.example.com/rss", False),
        ("http://rsshub:1200/twitter/user/wallstengine", False),
        ("https://rsshub:1200/twitter/user/wallstengine", False),
        ("https://localhost/rss", False),
        ("https://feeds.local/rss", False),
        ("https://feed.example.test/rss", False),
        ("https://127.0.0.1/rss", False),
        ("https://169.254.169.254/latest/meta-data", False),
        ("https://10.0.0.8/rss", False),
        ("https://[::1]/rss", False),
        ("https://[fc00::1]/rss", False),
        ("https://user@feed.example.com/rss", False),
        ("https://feed.example.com./rss", False),
    ],
)
def test_relay_eligibility_requires_a_public_https_destination(
    url: str,
    eligible: bool,
) -> None:
    assert is_public_https_feed_url(url) is eligible


def test_rss_reader_never_relays_an_internal_rsshub_source() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="unavailable")

    rsshub_source = NewsSourceDefinition(
        source_id="internal-rsshub",
        name="Internal RSSHub",
        feed_url="http://rsshub:1200/twitter/user/wallstengine",
        tier=4,
        memberships=("finance",),
    )
    reader = RssFeedReader(
        transport=httpx.MockTransport(handler),
        max_attempts=1,
        relay_base_url="https://relay.example.com",
        relay_auth_token="do-not-send",
        relay_allowed_urls={rsshub_source.feed_url},
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="news_rss_relay_source_not_allowed",
        ):
            reader.fetch_wire(
                source=rsshub_source,
                etag=None,
                last_modified=None,
            )
    finally:
        reader.close()

    assert [request.url.host for request in requests] == ["rsshub"]
    assert all("do-not-send" not in str(request.headers) for request in requests)
