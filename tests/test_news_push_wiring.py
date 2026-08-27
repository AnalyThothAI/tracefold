"""Worker wiring binds Telegram delivery to one secure, configured target."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tracefold.app.workers.wiring import news as news_wiring
from tracefold.app.workers.wiring.components import _wire_components
from tracefold.platform.config.models import Settings

CHANNEL_ID = -1001234567890
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings.model_validate(
        {
            "news": {
                "enabled": True,
                "push": {
                    "enabled": True,
                    "telegram_bot_token_file": "telegram_bot_token",
                    "telegram_chat_id": CHANNEL_ID,
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
    captured: dict[str, Any] = {}
    sender = object()

    def build_sender(*, bot_token: str, chat_id: int) -> object:
        captured.update(bot_token=bot_token, chat_id=chat_id)
        return sender

    monkeypatch.setattr(news_wiring, "TelegramNewsPushSender", build_sender)

    assert news_wiring._news_push_sender(_settings(tmp_path)) is sender
    assert captured == {"bot_token": BOT_TOKEN, "chat_id": CHANNEL_ID}


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
