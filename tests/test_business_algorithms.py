from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from tracefold.macro import resolve_completed_session
from tracefold.market import canonical_chain_address, canonical_chain_id, chain_address_key, market_tick_id
from tracefold.news import (
    NewsFeedEntry,
    NewsSourceDefinition,
    next_story_state_refresh,
    normalize_feed_entry,
    story_similarity,
)

_NEW_YORK = ZoneInfo("America/New_York")


def test_market_identity_normalizes_evm_without_corrupting_solana_case() -> None:
    assert canonical_chain_id("Ethereum") == "eip155:1"
    assert canonical_chain_address("ethereum", "0xAbCd") == "0xabcd"
    assert chain_address_key("SOL", "AbCd") == ("solana", "AbCd")
    assert chain_address_key("SOL", "AbCd") != chain_address_key("solana", "abcd")


def test_market_tick_identity_is_stable_and_source_specific() -> None:
    first = market_tick_id(
        target_type="Asset",
        target_id="asset:solana:token:abc",
        source_provider="gmgn",
        observed_at_ms=1_778_145_100_000,
    )
    replay = market_tick_id(
        target_type="Asset",
        target_id="asset:solana:token:abc",
        source_provider="gmgn",
        observed_at_ms=1_778_145_100_000,
    )
    other_source = market_tick_id(
        target_type="Asset",
        target_id="asset:solana:token:abc",
        source_provider="binance",
        observed_at_ms=1_778_145_100_000,
    )

    assert first == replay
    assert first.startswith("market_tick:")
    assert first != other_source


def test_news_feed_normalization_requires_public_identity_and_cleans_content() -> None:
    source = NewsSourceDefinition(
        source_id="example",
        name="Example",
        feed_url="https://example.com/feed",
        source_domain="example.com",
        source_role="original_publisher",
        trust_tier="authoritative",
        source_chain_id="example",
    )
    item = normalize_feed_entry(
        source=source,
        entry=NewsFeedEntry(
            guid="story-1",
            link="https://example.com/markets/story-1?utm_source=feed",
            title="<b>Market update</b>",
            summary="<p>Evidence <a href='https://example.com'>details</a></p>",
            published_at_ms=1_778_145_100_000,
        ),
        observed_at_ms=1_778_145_200_000,
    )

    assert item.source_guid == "story-1"
    assert item.identity_method == "canonical_url"
    assert item.canonical_url == "https://example.com/markets/story-1"
    assert item.title == "Market update"
    assert item.snippet == "Evidence details"
    assert item.origin_domain == "example.com"
    assert item.provenance_status == "verified"


def test_news_story_similarity_accepts_one_event_and_rejects_fact_conflicts() -> None:
    same_event = story_similarity(
        article_title="Federal Reserve cuts rates by 25 basis points",
        article_snippet="Officials lowered the target after the meeting",
        candidate_title="Fed cuts interest rate by 25 basis points",
        candidate_snippet="The central bank lowered its policy target",
    )
    conflicting_event = story_similarity(
        article_title="Federal Reserve cuts rates by 25 basis points",
        article_snippet="Officials lowered the target after the meeting",
        candidate_title="Federal Reserve raises rates by 50 basis points",
        candidate_snippet="Officials increased the target after the meeting",
    )

    assert same_event["accepted"] is True
    assert conflicting_event["accepted"] is False
    assert conflicting_event["hard_conflicts"] == {
        "number": True,
        "action": True,
        "subject": False,
    }

    same_event_zh = story_similarity(
        article_title="美联储宣布降息25个基点",
        article_snippet="政策制定者在会议后下调目标利率",
        candidate_title="美联储降息25个基点",
        candidate_snippet="央行会议决定下调政策利率",
    )
    conflicting_event_zh = story_similarity(
        article_title="美联储宣布降息25个基点",
        article_snippet="政策制定者在会议后下调目标利率",
        candidate_title="美联储宣布加息50个基点",
        candidate_snippet="政策制定者提高目标利率",
    )
    different_event_zh = story_similarity(
        article_title="中国公布最新贸易数据",
        article_snippet="出口增速发生变化",
        candidate_title="美国公布最新就业数据",
        candidate_snippet="新增就业人数发生变化",
    )

    assert same_event_zh["accepted"] is True
    assert conflicting_event_zh["accepted"] is False
    assert conflicting_event_zh["hard_conflicts"]["number"] is True
    assert conflicting_event_zh["hard_conflicts"]["action"] is True
    assert different_event_zh["accepted"] is False


def test_news_language_detection_and_story_state_refresh_are_deterministic() -> None:
    source = NewsSourceDefinition(
        source_id="news6551",
        name="6551News",
        feed_url="http://rsshub:1200/telegram/channel/news6551",
        source_domain="t.me",
        source_role="trusted_aggregator",
        trust_tier="authoritative",
        source_chain_id="6551",
        default_language="en",
    )
    item = normalize_feed_entry(
        source=source,
        entry=NewsFeedEntry(
            guid="telegram-1",
            link="https://t.me/news6551/1",
            title="美联储宣布维持利率不变",
            summary="等待下一次政策会议。",
            published_at_ms=1_778_145_100_000,
        ),
        observed_at_ms=1_778_145_200_000,
    )
    next_refresh = next_story_state_refresh(
        first_seen_at_ms=1_778_145_200_000,
        last_seen_at_ms=1_778_145_200_000,
        article_count=1,
        now_ms=1_778_145_200_000,
    )

    assert item.language == "zh"
    assert next_refresh == 1_778_152_400_001


def test_news_article_identity_fallbacks_are_stable_and_source_scoped() -> None:
    first_source = NewsSourceDefinition(
        source_id="first",
        name="First",
        feed_url="https://first.example/feed",
        source_domain="first.example",
        source_role="original_publisher",
        trust_tier="authoritative",
        source_chain_id="first",
    )
    second_source = first_source.model_copy(
        update={
            "source_id": "second",
            "name": "Second",
            "feed_url": "https://second.example/feed",
            "source_domain": "second.example",
            "source_chain_id": "second",
        }
    )
    guid_entry = NewsFeedEntry(
        guid="guid-1",
        title="Policy decision",
        published_at_ms=1_778_145_100_000,
    )
    fallback_entry = NewsFeedEntry(
        title="Policy decision",
        published_at_ms=1_778_145_100_000,
    )

    guid_first = normalize_feed_entry(
        source=first_source,
        entry=guid_entry,
        observed_at_ms=1_778_145_200_000,
    )
    guid_replay = normalize_feed_entry(
        source=first_source,
        entry=guid_entry,
        observed_at_ms=1_778_145_300_000,
    )
    title_first = normalize_feed_entry(
        source=first_source,
        entry=fallback_entry,
        observed_at_ms=1_778_145_200_000,
    )
    title_second = normalize_feed_entry(
        source=second_source,
        entry=fallback_entry,
        observed_at_ms=1_778_145_200_000,
    )

    assert guid_first.identity_method == "source_guid"
    assert guid_first.article_id == guid_replay.article_id
    assert guid_first.identity_version == "news_article_identity_v1"
    assert title_first.identity_method == "title_time_bucket"
    assert title_first.article_id != title_second.article_id


def test_news_aggregator_provenance_distinguishes_verified_attributed_and_unknown() -> None:
    source = NewsSourceDefinition(
        source_id="news6551",
        name="6551News",
        feed_url="http://rsshub:1200/telegram/channel/news6551",
        source_domain="t.me",
        source_role="trusted_aggregator",
        trust_tier="authoritative",
        source_chain_id="6551",
        default_language="zh",
    )

    verified = normalize_feed_entry(
        source=source,
        entry=NewsFeedEntry(
            link="https://t.me/news6551/1",
            title="政策更新",
            summary="原文 https://www.reuters.com/world/policy-update",
        ),
        observed_at_ms=1_778_145_200_000,
    )
    attributed = normalize_feed_entry(
        source=source,
        entry=NewsFeedEntry(
            link="https://t.me/news6551/2",
            title="政策消息",
            summary="来源：路透社",
        ),
        observed_at_ms=1_778_145_200_000,
    )
    unknown = normalize_feed_entry(
        source=source,
        entry=NewsFeedEntry(
            link="https://t.me/news6551/3",
            title="未经归属的政策消息",
        ),
        observed_at_ms=1_778_145_200_000,
    )
    tagged = normalize_feed_entry(
        source=source,
        entry=NewsFeedEntry(
            link="https://t.me/news6551/4",
            title="Policy update",
            language="en-US",
        ),
        observed_at_ms=1_778_145_200_000,
    )

    assert (verified.origin_domain, verified.provenance_status) == (
        "reuters.com",
        "verified",
    )
    assert (attributed.origin_name, attributed.provenance_status) == (
        "路透社",
        "attributed",
    )
    assert unknown.provenance_status == "unknown"
    assert tagged.language == "en"


def test_macro_completed_session_obeys_settle_delay_and_market_calendar() -> None:
    before_settle = _epoch_ms(datetime(2026, 7, 23, 16, 15, tzinfo=_NEW_YORK))
    after_settle = _epoch_ms(datetime(2026, 7, 23, 16, 30, tzinfo=_NEW_YORK))
    independence_day = _epoch_ms(datetime(2026, 7, 4, 18, 0, tzinfo=_NEW_YORK))

    assert resolve_completed_session(now_ms=before_settle, settle_delay_seconds=1_800) == date(2026, 7, 22)
    assert resolve_completed_session(now_ms=after_settle, settle_delay_seconds=1_800) == date(2026, 7, 23)
    assert resolve_completed_session(now_ms=independence_day, settle_delay_seconds=0) == date(2026, 7, 2)


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(UTC).timestamp() * 1_000)
