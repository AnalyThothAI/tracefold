from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
from langchain_core.messages import AIMessage

from tracefold.integrations.news_feeds import RssFeedReader
from tracefold.integrations.news_story_analysis import DeepSeekStoryAnalyzer
from tracefold.news import (
    NewsAnalysisEvidence,
    NewsSourceDefinition,
    NewsStoryAnalysisDraft,
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


def test_deepseek_story_analyzer_returns_evidence_bound_chinese_draft_and_receipt() -> None:
    model = FakeChatModel()
    analyzer = DeepSeekStoryAnalyzer(
        model=cast(Any, model),
        model_name="openai/deepseek-chat",
    )

    result = asyncio.run(analyzer.analyze(news_evidence()))

    assert result.draft.what_happened == "政策事件已经得到权威来源确认。"
    assert result.draft.evidence_references == ("article-1",)
    assert result.receipt == {
        "model": "openai/deepseek-chat",
        "prompt_version": "news_story_analysis_v2",
        "workflow_version": "news_story_analysis_workflow_v2",
        "schema_version": "news_story_analysis_schema_v1",
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
    assert "只分析输入的一个 Story" in system_message["content"]
    assert "JSON schema" in user_message["content"]
    assert "article-1" in user_message["content"]


class FakeChatModel:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    async def ainvoke(self, messages: list[dict[str, str]]) -> AIMessage:
        self.messages = messages
        return AIMessage(
            content=NewsStoryAnalysisDraft(
                what_happened="政策事件已经得到权威来源确认。",
                why_it_matters="它改变了全球政策预期。",
                political_impact="政策协调压力上升。",
                economic_market_impact="利率与汇率预期可能重估。",
                confirmed_facts=("权威来源已经发布正式信息。",),
                disagreements_unknowns=("后续政策路径未知。",),
                next_checkpoint="等待下一份官方公告。",
                evidence_references=("article-1",),
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


def news_evidence() -> NewsAnalysisEvidence:
    return NewsAnalysisEvidence(
        story_id="story-1",
        evidence_set_hash="evidence-1",
        title="Policy response confirmed",
        snippet="Officials published a formal response.",
        verification_status="trusted",
        phase="breaking",
        importance_score=70,
        source_count=1,
        article_count=1,
        trusted_source_count=1,
        independent_origin_count=1,
        articles=(
            {
                "article_id": "article-1",
                "title": "Policy response confirmed",
                "provenance_status": "verified",
                "source_name": "Example News",
            },
        ),
    )
