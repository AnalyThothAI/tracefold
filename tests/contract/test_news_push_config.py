"""Push configuration selects exactly one provider without exposing credentials."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.platform.config.models import Settings, news_push_availability

CHANNEL_ID = -1001234567890


def _telegram_settings(tmp_path: Path, *, chat_id: object = CHANNEL_ID) -> Settings:
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": chat_id,
                },
            }
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def test_telegram_push_requires_a_secure_token_file_and_private_channel(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyzABCDE_12345\n", encoding="utf-8")
    token_file.chmod(0o600)
    availability = news_push_availability(_telegram_settings(tmp_path))

    assert availability.provider == "telegram"
    assert availability.delivery_available is True
    assert availability.telegram_bot_token_file_configured is True
    assert availability.telegram_chat_id_configured is True


def test_telegram_push_fails_closed_when_token_file_permissions_are_open(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("not-returned-in-diagnostics\n", encoding="utf-8")
    token_file.chmod(0o644)

    availability = news_push_availability(_telegram_settings(tmp_path))

    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_telegram_bot_token_unavailable"
    assert availability.telegram_bot_token_file_configured is False


def test_telegram_push_fails_closed_when_token_file_content_is_not_a_bot_token(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("not-a-telegram-bot-token\n", encoding="utf-8")
    token_file.chmod(0o600)

    availability = news_push_availability(_telegram_settings(tmp_path))

    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_telegram_bot_token_unavailable"
    assert availability.telegram_bot_token_file_configured is False


def test_serve_can_report_configured_push_without_reading_the_workers_only_secret(tmp_path: Path) -> None:
    availability = news_push_availability(_telegram_settings(tmp_path), inspect_secret_file=False)

    assert availability.provider == "telegram"
    assert availability.delivery_available is True
    assert availability.telegram_bot_token_file_configured is True


@pytest.mark.parametrize("chat_id", ["@channel", "not-a-number", True])
def test_telegram_push_reads_a_chat_id_that_is_a_number_and_nothing_else(tmp_path: Path, chat_id: object) -> None:
    """`Settings` reads the operator's number; the adapter owns what a valid target looks like.

    #562 §5 rows 1 and 8: the private-channel shape used to be enforced here as well as in
    `TelegramNewsPushSender`, and this copy refused the whole configuration -- so a mistyped digit took
    reception, triage and the market loop down with the process, with nothing left running to say so.
    """

    with pytest.raises(ValidationError, match="news_push_telegram_chat_id_invalid"):
        _telegram_settings(tmp_path, chat_id=chat_id)


@pytest.mark.parametrize("chat_id", [0, 123456789, -123456789])
def test_a_chat_id_that_is_not_a_private_channel_still_loads_and_reports_itself(tmp_path: Path, chat_id: int) -> None:
    settings = _telegram_settings(tmp_path, chat_id=chat_id)

    assert settings.news.push.telegram_chat_id == chat_id
    # The configuration is loadable, so the process starts and the capability can name the fault --
    # `tests/test_news_push_wiring.py` proves the sender refuses it and delivery reads `unavailable`.
    assert news_push_availability(settings).telegram_chat_id_configured is True


def test_push_rejects_ambiguous_feishu_and_telegram_provider_configuration(tmp_path: Path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyzABCDE_12345\n", encoding="utf-8")
    token_file.chmod(0o600)
    settings = _telegram_settings(tmp_path)
    settings.news.push.feishu_webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/example"

    availability = news_push_availability(settings)

    assert availability.provider is None
    assert availability.delivery_available is False
    assert availability.reason == "news_item_push_provider_conflict"
