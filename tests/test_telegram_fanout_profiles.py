from __future__ import annotations

import json

import httpx

from tracefold.integrations.telegram import TelegramNewsFanoutSender, TelegramNewsPushSender, TelegramTradingClient
from tracefold.news import TelegramDeliveryReceipt

BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"
CHANNEL = -1001234567890
GROUP = -987654321
USER_A = 8385255219
USER_B = 8385255220


def _card() -> dict[str, object]:
    return {
        "header": {"title": {"content": "测试新闻"}, "template": "green"},
        "elements": [{"tag": "markdown", "content": "利多 · 新事实 · 影响有限 · HYPE · 测试 · 09:01"}],
    }


def test_fanout_sends_every_target_but_adds_trading_buttons_only_to_a_profile_private_chat() -> None:
    sent: list[dict[str, object]] = []
    message_ids = {CHANNEL: 11, GROUP: 12, USER_A: 13}

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = request.url.path.strip("/")
        chat_id = payload.get("chat_id")
        if method == "getChat":
            chat_type = {CHANNEL: "channel", GROUP: "group", USER_A: "private"}[chat_id]
            return httpx.Response(200, json={"ok": True, "result": {"id": chat_id, "type": chat_type}})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 99, "is_bot": True}})
        if method == "getChatMember":
            status = "administrator" if chat_id == CHANNEL else "member"
            result = {"status": status, "user": {"id": 99, "is_bot": True}}
            if chat_id == CHANNEL:
                result["can_post_messages"] = True
            return httpx.Response(200, json={"ok": True, "result": result})
        if method == "sendMessage":
            sent.append(payload)
            return httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": message_ids[chat_id], "chat": {"id": chat_id}}},
            )
        raise AssertionError(method)

    transport = httpx.MockTransport(handle)
    fanout = TelegramNewsFanoutSender(
        (
            TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL, transport=transport),
            TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=GROUP, transport=transport),
            TelegramNewsPushSender(
                bot_token=BOT_TOKEN,
                chat_id=USER_A,
                transport=transport,
                trading_actions_enabled=True,
                onchain_actions_enabled=True,
            ),
        )
    )

    fanout.prepare()
    receipt = TelegramDeliveryReceipt.model_validate(fanout.send_card(_card()))
    fanout.close()

    by_target = {payload["chat_id"]: payload for payload in sent}
    assert set(by_target) == {CHANNEL, GROUP, USER_A}
    assert "reply_markup" not in by_target[CHANNEL]
    assert "reply_markup" not in by_target[GROUP]
    assert by_target[USER_A]["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "详细数据", "callback_data": "tf:detail:v1"},
                {"text": "合约交易", "callback_data": "tf:trade:v1"},
            ],
            [{"text": "链上路由", "callback_data": "tf:onchain:v1"}],
        ]
    }
    assert receipt.message_id == 11
    assert [copy.message_id for copy in receipt.copies] == [12, 13]


def test_test_commands_are_authorized_only_in_the_matching_private_chat() -> None:
    updates = [
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": USER_A, "is_bot": False},
                "chat": {"id": GROUP, "type": "group"},
                "text": "/test_futures",
            },
        },
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "from": {"id": USER_A, "is_bot": False},
                "chat": {"id": USER_A, "type": "private"},
                "text": "/test_futures",
            },
        },
        {
            "update_id": 3,
            "message": {
                "message_id": 12,
                "from": {"id": USER_B, "is_bot": False},
                "chat": {"id": USER_B, "type": "private"},
                "text": "/test_onchain",
            },
        },
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/getUpdates"
        return httpx.Response(200, json={"ok": True, "result": updates})

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(USER_A, USER_B),
        transport=httpx.MockTransport(handle),
    )
    parsed = client.poll_updates(next_update_id=0)
    client.close()

    assert [(item.data, item.authorized) for item in parsed] == [
        ("tf:test:futures", False),
        ("tf:test:futures", True),
        ("tf:test:onchain", True),
    ]
    assert client.target_sha256_for(USER_A) != client.target_sha256_for(USER_B)


def test_start_command_opens_private_help_instead_of_being_ignored() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/getUpdates"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 4,
                        "message": {
                            "message_id": 13,
                            "from": {"id": USER_A, "is_bot": False},
                            "chat": {"id": USER_A, "type": "private"},
                            "text": "/start",
                        },
                    }
                ],
            },
        )

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(USER_A,),
        transport=httpx.MockTransport(handle),
    )

    update = client.poll_updates(next_update_id=0)[0]
    client.close()

    assert update.data == "tf:help:v1"
    assert update.authorized is True
