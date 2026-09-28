from __future__ import annotations

import json

import httpx

from tracefold.integrations.telegram import TelegramNewsPushSender
from tracefold.news import ReaderDeliveryPresentation
from tracefold.news.feishu_card import feishu_card
from tracefold.news.reader_card import ReaderCard

CHANNEL_ID = -1001234567890

BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"

BOT_TOKEN_ROTATED = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12346"

BOT_ID = 123456


def _preflight_response(request: httpx.Request) -> httpx.Response | None:
    method = request.url.path.rsplit("/", maxsplit=1)[-1]
    payload = json.loads(request.content)
    if method == "getChat":
        assert payload == {"chat_id": CHANNEL_ID}
        return httpx.Response(200, json={"ok": True, "result": {"id": CHANNEL_ID, "type": "channel"}})
    if method == "getMe":
        assert payload == {}
        return httpx.Response(200, json={"ok": True, "result": {"id": BOT_ID, "is_bot": True}})
    if method == "getChatMember":
        assert payload == {"chat_id": CHANNEL_ID, "user_id": BOT_ID}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "status": "administrator",
                    "user": {"id": BOT_ID, "is_bot": True},
                    "can_post_messages": True,
                },
            },
        )
    return None


def _sent_text(card: ReaderCard, *, presentation: ReaderDeliveryPresentation | None = None) -> str:
    """One card through the real adapter and a fake Telegram endpoint; the text that left."""

    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        wall_clock_ms=lambda: 1_788_600_420_000,  # 17:27 on the reader's clock
    )
    sender.prepare()
    sender.send_card(card, channel_payload=feishu_card(card), presentation=presentation)
    return str(observed["text"])
