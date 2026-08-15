from __future__ import annotations

import pytest

from tracefold.platform.config.settings import (
    Settings,
    news_push_availability,
    news_title_presentation_availability,
)

_FEISHU_TEST_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id"


@pytest.mark.parametrize(
    ("news", "reason"),
    [
        ({"enabled": True, "push": {"enabled": True}}, "news_item_push_feishu_webhook_missing"),
        (
            {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "feishu_webhook_url": "https://example.test/not-feishu",
                },
            },
            "news_item_push_feishu_webhook_invalid",
        ),
        (
            {
                "enabled": False,
                "push": {"enabled": True, "feishu_webhook_url": _FEISHU_TEST_URL},
            },
            "news_item_push_news_disabled",
        ),
    ],
)
def test_invalid_push_configuration_degrades_without_blocking_startup(
    news: dict[str, object],
    reason: str,
) -> None:
    settings = Settings(news=news)

    availability = news_push_availability(settings)

    assert availability.requested is True
    assert availability.delivery_available is False
    assert availability.reason == reason


def test_push_and_title_presentation_availability_are_independent() -> None:
    settings = Settings(
        news={
            "title_presentation": {"deepl_api_keys": ["first:fx", "second:fx"]},
            "push": {"enabled": True, "feishu_webhook_url": _FEISHU_TEST_URL},
        },
        llm={
            "api_key": "secret",
            "base_url": "https://translator.test/v1",
            "news_brief_model": "translator",
        },
    )

    availability = news_push_availability(settings)

    assert availability.requested is True
    assert availability.delivery_available is True
    assert availability.reason is None
    assert availability.feishu_webhook_url_configured is True
    assert availability.feishu_signing_secret_configured is False

    presentation = news_title_presentation_availability(settings)
    assert presentation.deepl_configured is True
    assert presentation.deepl_key_count == 2
    assert presentation.deepseek_configured is True

    unrequested = news_title_presentation_availability(
        Settings(
            llm={
                "api_key": "secret",
                "base_url": "https://translator.test/v1",
                "news_brief_model": "translator",
            }
        )
    )
    assert unrequested.deepl_configured is False
    assert unrequested.deepl_key_count == 0
    assert unrequested.deepseek_configured is True


def test_invalid_global_llm_url_disables_only_deepseek_fallback() -> None:
    settings = Settings(
        news={"push": {"enabled": True, "feishu_webhook_url": _FEISHU_TEST_URL}},
        llm={
            "api_key": "secret",
            "base_url": "not-a-url",
            "news_brief_model": "translator",
        },
    )

    push_availability = news_push_availability(settings)
    presentation = news_title_presentation_availability(settings)

    assert push_availability.delivery_available is True
    assert push_availability.reason is None
    assert presentation.deepseek_configured is False


def test_deepl_keys_preserve_order_and_reject_duplicates() -> None:
    settings = Settings(news={"title_presentation": {"deepl_api_keys": [" first:fx ", "second"]}})

    assert settings.news.title_presentation.deepl_api_keys == ("first:fx", "second")

    with pytest.raises(ValueError, match="news_title_presentation_deepl_api_keys_duplicate"):
        Settings(news={"title_presentation": {"deepl_api_keys": ["same", " same "]}})
