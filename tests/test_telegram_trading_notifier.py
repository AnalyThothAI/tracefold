from __future__ import annotations

import json

import httpx
import pytest

from tracefold.integrations.telegram import (
    TelegramDeliveryError,
    TelegramTradingNotifier,
)

_TOKEN = "1234567:" + "a" * 40
_CHAT_ID = -1001234567890


def test_trading_notifier_preflights_target_and_returns_native_message_id() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = dict(json.loads(request.content))
        calls.append((request.url.path, payload))
        if request.url.path == "/getMe":
            result = {"id": 7001, "is_bot": True}
        elif request.url.path == "/getChat":
            result = {"id": _CHAT_ID, "type": "supergroup"}
        else:
            result = {"message_id": 91, "chat": {"id": _CHAT_ID}}
        return httpx.Response(200, json={"ok": True, "result": result})

    notifier = TelegramTradingNotifier(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(handler),
    )
    try:
        notifier.prepare()
        assert notifier.send("Runtime accepted command 1234") == 91
    finally:
        notifier.close()

    assert [path for path, _payload in calls] == ["/getMe", "/getChat", "/sendMessage"]
    assert calls[-1][1]["chat_id"] == _CHAT_ID
    assert calls[-1][1]["text"] == "Runtime accepted command 1234"
    assert len(notifier.target_sha256) == 64
    assert notifier.bot_id == 1234567


def test_trading_notifier_outage_is_a_sanitized_expected_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False})

    notifier = TelegramTradingNotifier(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TelegramDeliveryError, match="trading_notification_telegram_http_failed"):
            notifier.prepare()
    finally:
        notifier.close()


def test_trading_notification_target_survives_token_rotation_for_the_same_bot_and_chat() -> None:
    first = TelegramTradingNotifier(
        bot_token="1234567:" + "a" * 40,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    rotated = TelegramTradingNotifier(
        bot_token="1234567:" + "b" * 40,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    other_bot = TelegramTradingNotifier(
        bot_token="7654321:" + "a" * 40,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    try:
        assert first.target_sha256 == rotated.target_sha256
        assert first.target_sha256 != other_bot.target_sha256
    finally:
        first.close()
        rotated.close()
        other_bot.close()
