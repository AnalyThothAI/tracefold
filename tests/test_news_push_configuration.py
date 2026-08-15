from __future__ import annotations

import pytest

from tracefold.platform.config.settings import Settings, news_push_availability

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
    assert availability.translation_available is False
    assert availability.reason == reason


def test_push_and_translation_availability_are_independent() -> None:
    settings = Settings(
        news={"push": {"enabled": True, "feishu_webhook_url": _FEISHU_TEST_URL}},
        llm={
            "api_key": "secret",
            "base_url": "https://translator.test/v1",
            "news_brief_model": "translator",
        },
    )

    availability = news_push_availability(settings)

    assert availability.requested is True
    assert availability.delivery_available is True
    assert availability.translation_available is True
    assert availability.reason is None
    assert availability.feishu_webhook_url_configured is True
    assert availability.feishu_signing_secret_configured is False

    unrequested = news_push_availability(
        Settings(
            llm={
                "api_key": "secret",
                "base_url": "https://translator.test/v1",
                "news_brief_model": "translator",
            }
        )
    )
    assert unrequested.requested is False
    assert unrequested.delivery_available is False
    assert unrequested.translation_available is True


def test_invalid_global_llm_url_disables_only_best_effort_translation() -> None:
    settings = Settings(
        news={"push": {"enabled": True, "feishu_webhook_url": _FEISHU_TEST_URL}},
        llm={
            "api_key": "secret",
            "base_url": "not-a-url",
            "news_brief_model": "translator",
        },
    )

    availability = news_push_availability(settings)

    assert availability.delivery_available is True
    assert availability.translation_available is False
    assert availability.reason is None
