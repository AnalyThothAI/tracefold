"""Worker wiring fans out Telegram News and adds actions only for private profiles."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tracefold.app.workers.wiring import news as news_wiring
from tracefold.app.workers.wiring.components import _wire_components
from tracefold.platform.config.models import Settings

CHANNEL_ID = -1001234567890
PRIVATE_CHAT_ID = 8385255219
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_ids": [CHANNEL_ID],
                },
            }
        }
    )
    settings.set_config_dir(tmp_path)
    return settings


def test_worker_reads_the_secure_token_and_binds_the_configured_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    captured: list[dict[str, Any]] = []
    sender = object()
    fanout = object()

    def build_sender(**values: Any) -> object:
        captured.append(values)
        return sender

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)
    monkeypatch.setattr(news_wiring, "TelegramNewsFanoutSender", lambda _senders: fanout)

    assert news_wiring._news_push_sender(_settings(tmp_path)) is fanout
    assert captured == [
        {
            "bot_token": BOT_TOKEN,
            "chat_id": CHANNEL_ID,
            "trading_actions_enabled": False,
            "onchain_actions_enabled": False,
        }
    ]


def test_worker_does_not_construct_a_sender_from_an_insecure_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o644)
    constructed = False

    def build_sender(**_kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        return object()

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)

    with pytest.raises(
        RuntimeError,
        match="news_push_unavailable:news_item_push_telegram_bot_token_unavailable",
    ):
        news_wiring._news_push_sender(_settings(tmp_path))
    assert constructed is False


def test_worker_leaves_delivery_off_when_push_is_not_requested(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.news.push.enabled = False

    assert news_wiring._news_push_sender(settings) is None


def test_manual_trading_enables_news_card_actions_without_reading_venue_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_ids": [CHANNEL_ID, PRIVATE_CHAT_ID],
                },
            },
            "trading": {
                "telegram_profiles": [
                    {
                        "user_id": PRIVATE_CHAT_ID,
                        "manual": {
                            "enabled": True,
                            "live_trading_acknowledged": True,
                            "account_ref": "private-profile",
                            "api_key_file": f"trading_profiles/manual/{PRIVATE_CHAT_ID}/binance_api_key",
                            "api_secret_file": f"trading_profiles/manual/{PRIVATE_CHAT_ID}/binance_api_secret",
                        },
                    }
                ]
            },
        }
    )
    settings.set_config_dir(tmp_path)
    captured: dict[int, dict[str, Any]] = {}
    fanout = object()

    def build_sender(
        *, bot_token: str, chat_id: int, trading_actions_enabled: bool, onchain_actions_enabled: bool
    ) -> object:
        captured[chat_id] = {
            "bot_token": bot_token,
            "chat_id": chat_id,
            "trading_actions_enabled": trading_actions_enabled,
            "onchain_actions_enabled": onchain_actions_enabled,
        }
        return object()

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)
    monkeypatch.setattr(news_wiring, "TelegramNewsFanoutSender", lambda _senders: fanout)

    assert news_wiring._news_push_sender(settings) is fanout
    assert captured[CHANNEL_ID] == {
        "bot_token": BOT_TOKEN,
        "chat_id": CHANNEL_ID,
        "trading_actions_enabled": False,
        "onchain_actions_enabled": False,
    }
    assert captured[PRIVATE_CHAT_ID]["trading_actions_enabled"] is True
    assert captured[PRIVATE_CHAT_ID]["onchain_actions_enabled"] is False


def test_worker_keeps_trading_actions_off_without_a_matching_private_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text(BOT_TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    settings = _settings(tmp_path)
    captured: dict[str, Any] = {}

    def build_sender(
        *, bot_token: str, chat_id: int, trading_actions_enabled: bool, onchain_actions_enabled: bool
    ) -> object:
        captured.update(
            bot_token=bot_token,
            chat_id=chat_id,
            trading_actions_enabled=trading_actions_enabled,
            onchain_actions_enabled=onchain_actions_enabled,
        )
        return object()

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)
    monkeypatch.setattr(news_wiring, "TelegramNewsFanoutSender", lambda _senders: object())

    assert news_wiring._news_push_sender(settings) is not None
    assert captured["trading_actions_enabled"] is False
    assert captured["onchain_actions_enabled"] is False


def test_worker_startup_rejects_requested_push_when_news_is_disabled() -> None:
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": False,
                "push": {
                    "enabled": True,
                    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
                },
            }
        }
    )

    with pytest.raises(RuntimeError, match="news_push_unavailable:news_item_push_news_disabled"):
        asyncio.run(
            _wire_components(
                settings=settings,
                db=object(),  # type: ignore[arg-type]
                finite=object(),  # type: ignore[arg-type]
                telemetry=object(),  # type: ignore[arg-type]
            )
        )
