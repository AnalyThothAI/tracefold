"""Telegram manual-trading transport stays bound to one channel and operator allowlist."""

from __future__ import annotations

import json

import httpx

from tracefold.integrations.telegram import TelegramNewsPushSender, TelegramTradingClient

CHANNEL_ID = -1001234567890
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"
OPERATOR_ID = 123456789


def _response(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def test_news_sender_adds_stable_detail_and_trade_actions_when_manual_trading_is_enabled() -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        observed.append((method, payload))
        if method == "getChat":
            return _response({"id": CHANNEL_ID, "type": "channel"})
        if method == "getMe":
            return _response({"id": 123456, "is_bot": True})
        if method == "getChatMember":
            return _response(
                {
                    "status": "administrator",
                    "user": {"id": 123456, "is_bot": True},
                    "can_post_messages": True,
                }
            )
        return _response({"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}})

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        trading_actions_enabled=True,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    receipt = sender.send_card(
        {
            "header": {"title": {"content": "BTC 新闻"}, "template": "green"},
            "elements": [{"tag": "markdown", "content": "新闻事实\n利多 · 新事实 · 影响明显 · BTC"}],
        }
    )
    sender.edit_card(
        receipt,
        {
            "header": {"title": {"content": "BTC 新闻"}, "template": "green"},
            "elements": [{"tag": "markdown", "content": "补充事实\n利多 · 新事实 · 影响明显 · BTC"}],
        },
    )

    expected = {
        "inline_keyboard": [
            [
                {"text": "详细数据", "callback_data": "tf:detail:v1"},
                {"text": "交易", "callback_data": "tf:trade:v1"},
            ]
        ]
    }
    sends = [payload for method, payload in observed if method in {"sendMessage", "editMessageText"}]
    assert [payload["reply_markup"] for payload in sends] == [expected, expected]


def test_trading_client_validates_updates_and_marks_only_bound_operator_as_authorized() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        requests.append((method, payload))
        return _response(
            [
                {
                    "update_id": 101,
                    "callback_query": {
                        "id": "callback-authorized",
                        "from": {"id": OPERATOR_ID, "is_bot": False},
                        "message": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}},
                        "data": "tf:trade:v1",
                    },
                },
                {
                    "update_id": 102,
                    "callback_query": {
                        "id": "callback-stranger",
                        "from": {"id": 777, "is_bot": False},
                        "message": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}},
                        "data": "tf:trade:v1",
                    },
                },
            ]
        )

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )

    updates = client.poll_updates(next_update_id=101)

    assert len(client.target_sha256) == 64
    assert requests == [
        (
            "getUpdates",
            {
                "offset": 101,
                "limit": 20,
                "timeout": 0,
                "allowed_updates": ["callback_query"],
            },
        )
    ]
    assert [(update.update_id, update.actor_user_id, update.authorized) for update in updates] == [
        (101, OPERATOR_ID, True),
        (102, 777, False),
    ]
    assert all(update.chat_id == CHANNEL_ID and update.message_id == 42 for update in updates)


def test_trading_client_answers_callback_and_edits_one_bound_interaction_message() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        requests.append((method, payload))
        if method == "answerCallbackQuery":
            return _response(True)
        return _response({"message_id": 99, "chat": {"id": CHANNEL_ID, "type": "channel"}})

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )
    client.answer_callback("callback-1", text="已打开交易预览")
    message_id = client.send_interaction_reply(
        source_message_id=42,
        text="选择交易策略",
        keyboard=(("大仓位 / 小止损", "tf:preset:tight:v1"), ("取消", "tf:cancel:v1")),
    )
    client.edit_interaction(
        message_id=message_id,
        text="BTC LONG Preview",
        keyboard=(("确认交易", "tf:confirm:v1:session-1"),),
    )

    assert requests[0] == (
        "answerCallbackQuery",
        {"callback_query_id": "callback-1", "text": "已打开交易预览", "show_alert": False},
    )
    assert requests[1][0] == "sendMessage"
    assert requests[1][1]["reply_parameters"] == {"message_id": 42, "allow_sending_without_reply": False}
    assert requests[2][0] == "editMessageText"
    assert requests[2][1]["message_id"] == 99
    assert requests[2][1]["reply_markup"] == {
        "inline_keyboard": [[{"text": "确认交易", "callback_data": "tf:confirm:v1:session-1"}]]
    }
