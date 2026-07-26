from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
from langchain_core.messages import AIMessage

from tracefold.integrations.news_ai import StructuredNewsPublisher
from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.integrations.news_pages import BoundedNewsPageReader
from tracefold.news import (
    NewsSourceDefinition,
    StoryAnalysisDraft,
    StoryAnalysisEvidence,
)


def test_rss_feed_reader_parses_entries_and_honors_conditional_fetch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match") == "etag-1":
            return httpx.Response(304, headers={"etag": "etag-1"})
        return httpx.Response(
            200,
            headers={"etag": "etag-1"},
            content=b"""<?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel>
                <title>Example</title>
                <language>en</language>
                <item>
                  <guid>story-1</guid>
                  <link>https://example.test/story-1</link>
                  <title>Policy response confirmed</title>
                  <description>Officials published a formal response.</description>
                  <pubDate>Sun, 26 Jul 2026 09:00:00 GMT</pubDate>
                </item>
              </channel>
            </rss>""",
        )

    reader = RssFeedReader(transport=httpx.MockTransport(handler))
    source = news_source()
    try:
        fetched = reader.fetch(source=source, etag=None, last_modified=None)
        not_modified = reader.fetch(source=source, etag=fetched.etag, last_modified=None)
    finally:
        reader.close()

    assert fetched.status_code == 200
    assert fetched.etag == "etag-1"
    assert fetched.entries[0].model_dump(exclude={"raw"}) == {
        "guid": "story-1",
        "link": "https://example.test/story-1",
        "title": "Policy response confirmed",
        "summary": "Officials published a formal response.",
        "published_at_ms": 1_785_056_400_000,
        "language": "en",
    }
    assert not_modified.not_modified is True
    assert not_modified.entries == ()
    assert requests[0].headers["user-agent"].startswith("Tracefold/")
    assert requests[1].headers["if-none-match"] == "etag-1"


def test_structured_news_publisher_returns_evidence_bound_chinese_payload_and_receipt() -> None:
    model = FakeChatModel()
    publisher = StructuredNewsPublisher(
        model=cast(Any, model),
        model_name="openai/deepseek-chat",
    )

    result = asyncio.run(publisher.analyze_story(news_evidence()))
    draft = StoryAnalysisDraft.model_validate(result.payload)

    assert draft.what_happened[0].text == "政策事件已经得到权威来源确认。"
    assert draft.what_happened[0].evidence_references == ("revision-1",)
    assert result.receipt == {
        "model": "openai/deepseek-chat",
        "response_id": "response-1",
        "model_name": "deepseek-chat",
        "finish_reason": "stop",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 80,
            "total_tokens": 200,
        },
    }
    [system_message, user_message] = model.messages
    assert system_message["role"] == "system"
    assert "新闻证据数据" in system_message["content"]
    assert "JSON Schema" in user_message["content"]
    assert "revision-1" in user_message["content"]


class FakeChatModel:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def ainvoke(self, messages: list[dict[str, str]]) -> AIMessage:
        self.messages = messages
        return AIMessage(
            content=StoryAnalysisDraft(
                what_happened=(
                    {
                        "text": "政策事件已经得到权威来源确认。",
                        "evidence_references": ("revision-1",),
                    },
                ),
                why_it_matters="它改变了全球政策预期。",
                political_impact="政策协调压力上升。",
                economic_market_impact="利率与汇率预期可能重估。",
                disagreements_unknowns=("后续政策路径未知。",),
                transmission_scenarios=(
                    {
                        "condition": "若政策延续",
                        "mechanism": "政策预期通过利率渠道传导",
                        "possible_effect": "利率预期可能继续调整",
                        "confidence": "medium",
                    },
                ),
                next_checkpoint="等待下一份官方公告。",
            ).model_dump_json(),
            id="response-1",
            response_metadata={
                "model_name": "deepseek-chat",
                "finish_reason": "stop",
            },
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 80,
                "total_tokens": 200,
            },
        )


def test_bounded_page_reader_honors_robots_and_extracts_bounded_text() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><nav>menu</nav><article><h1>Policy decision</h1>"
            "<p>Officials published the formal decision.</p></article></html>",
        )

    reader = BoundedNewsPageReader(
        transport=httpx.MockTransport(handler),
        resolver=lambda _: ("8.8.8.8",),
        clock_ms=lambda: 123,
    )
    try:
        result = reader.fetch(url="https://example.test/policy/decision")
    finally:
        reader.close()

    assert result.status == "available"
    assert result.fetched_at_ms == 123
    assert result.extracted_text == "Policy decision\nOfficials published the formal decision."
    assert result.content_hash
    assert requests == [
        "https://example.test/robots.txt",
        "https://example.test/policy/decision",
    ]


def test_bounded_page_reader_records_robots_paywall_content_type_and_size_failures() -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        raise AssertionError("denied page must not be fetched")

    denied_reader = BoundedNewsPageReader(
        transport=httpx.MockTransport(denied),
        resolver=lambda _: ("8.8.8.8",),
        clock_ms=lambda: 123,
    )
    try:
        assert denied_reader.fetch(url="https://example.test/private/story").status == "robots_denied"
    finally:
        denied_reader.close()

    responses = {
        "/paywall": httpx.Response(403, headers={"content-type": "text/html"}),
        "/binary": httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF",
        ),
        "/large": httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * 20_000,
        ),
    }

    def bounded(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return responses[request.url.path]

    reader = BoundedNewsPageReader(
        max_bytes=16_384,
        transport=httpx.MockTransport(bounded),
        resolver=lambda _: ("8.8.8.8",),
        clock_ms=lambda: 123,
    )
    try:
        assert reader.fetch(url="https://example.test/paywall").status == "paywalled"
        assert reader.fetch(url="https://example.test/binary").status == "unsupported_content"
        truncated = reader.fetch(url="https://example.test/large")
    finally:
        reader.close()
    assert truncated.status == "truncated"
    assert truncated.byte_count == 16_384


def news_source() -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id="example",
        name="Example News",
        feed_url="https://example.test/feed.xml",
        source_domain="example.test",
        source_role="original_publisher",
        trust_tier="authoritative",
        source_chain_id="example",
    )


def news_evidence() -> StoryAnalysisEvidence:
    return StoryAnalysisEvidence(
        story_id="story-1",
        material_evidence_hash="evidence-1",
        title="Policy response confirmed",
        snippet="Officials published a formal response.",
        event_core={"entities": ["central-bank"], "actions": ["approve"]},
        evidence_posture="primary_source_confirmed",
        evidence_factors={"has_primary_authority": True},
        impact_profile={"policy_impact": 80},
        material_change="initial",
        articles=(
            {
                "evidence_ref": "revision-1",
                "article_id": "article-1",
                "revision_id": "revision-1",
                "title": "Policy response confirmed",
                "snippet": "Officials published a formal response.",
                "source_name": "Example News",
            },
        ),
    )
