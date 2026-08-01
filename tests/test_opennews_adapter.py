from __future__ import annotations

import pytest

from tracefold.news import (
    NewsSourceDefinition,
    OpenNewsExpectedError,
    parse_opennews_message,
    parse_opennews_rest_response,
)
from tracefold.news.sources import OPENNEWS_SOURCE_ID, opennews_source
from tracefold.platform.config.settings import NewsSettings


def test_opennews_source_is_one_code_owned_additive_source() -> None:
    source = opennews_source()

    assert source.source_id == OPENNEWS_SOURCE_ID
    assert source.source_kind == "opennews"
    assert source.memberships == ("opennews",)
    assert source.feed_url == "https://ai.6551.io/open/news_search"


def test_news_source_defaults_to_rss() -> None:
    source = NewsSourceDefinition(
        source_id="rss",
        name="RSS",
        feed_url="https://example.com/rss",
        tier=2,
        memberships=("finance",),
    )

    assert source.source_kind == "rss"


def test_opennews_token_is_trimmed_and_optional() -> None:
    assert NewsSettings(opennews_token="  secret  ").opennews_token == "secret"
    assert NewsSettings(opennews_token="  ").opennews_token is None


def test_report_normalization_removes_tracking_and_ignores_ai_for_product() -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-1",
                "text": "Fed holds rates steady",
                "newsType": "Reuters",
                "engineType": "news",
                "link": "HTTPS://Example.COM/article/1/?utm_source=x&b=2&a=1#fragment",
                "ts": "2026-08-01T05:00:00Z",
                "received_at_ms": 123,
                "token": "must-not-survive",
                "aiRating": {"score": 99, "signal": "long"},
            },
        }
    )

    assert event is not None
    assert event.observation_kind == "report"
    assert event.source_item_key == "url:https://example.com/article/1?a=1&b=2"
    assert event.entry is not None
    assert event.entry.link == "https://example.com/article/1?a=1&b=2"
    assert event.entry.reporting_origin == "reuters"
    assert event.entry.published_at_ms == 1_785_560_400_000
    assert "received_at_ms" not in event.raw
    assert "token" not in event.raw
    assert event.raw["aiRating"] == {"score": 99, "signal": "long"}


@pytest.mark.parametrize("link", [None, "#fragment", "https://reuters.com", "https://reuters.com/"])
def test_linkless_or_homepage_wire_uses_dispatch_identity(link: str | None) -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-2",
                "text": "Linkless wire",
                "newsType": "Reuters",
                "engineType": "news",
                "link": link,
                "ts": 1_775_195_200_000,
            },
        }
    )

    assert event is not None
    assert event.source_item_key == "dispatch:opennews:wire-2"
    assert event.entry is not None
    assert event.entry.link is None


def test_translation_and_ai_update_are_observation_only() -> None:
    translation = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-3",
                "text": "翻译文本",
                "translationOf": "wire-1",
                "newsType": "Reuters",
                "engineType": "news",
                "ts": 1_775_195_200_000,
            },
        }
    )
    annotation = parse_opennews_message(
        {
            "method": "news.ai_update",
            "params": {"id": "wire-1", "aiRating": {"score": 90}},
        }
    )

    assert translation is not None and translation.observation_kind == "translation"
    assert translation.entry is None
    assert annotation is not None and annotation.observation_kind == "provider_annotation"
    assert annotation.entry is None


def test_strategy_and_non_news_engine_are_ignored() -> None:
    assert parse_opennews_message({"method": "strategy.triggered", "params": {"id": "x"}}) is None
    assert (
        parse_opennews_message(
            {
                "method": "news.update",
                "params": {"id": "x", "engineType": "listing", "text": "listed"},
            }
        )
        is None
    )


def test_rest_page_uses_the_same_message_normalizer_and_is_bounded() -> None:
    rows = [
        {
            "id": f"wire-{index}",
            "text": f"headline {index}",
            "newsType": "Reuters",
            "engineType": "news",
            "ts": 1_775_195_200_000,
        }
        for index in range(105)
    ]

    events = parse_opennews_rest_response({"success": True, "data": rows})

    assert len(events) == 100
    assert events[0].source_item_key == "dispatch:opennews:wire-0"


def test_invalid_rest_shape_fails_closed() -> None:
    with pytest.raises(OpenNewsExpectedError, match="opennews_rest_payload_invalid"):
        parse_opennews_rest_response({"data": "not-a-list"})
