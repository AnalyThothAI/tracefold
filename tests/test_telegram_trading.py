"""Telegram trading transport keeps every private user's interaction isolated."""

from __future__ import annotations

import json

import httpx
import pytest

from tracefold.integrations.telegram import TelegramDeliveryError, TelegramNewsPushSender, TelegramTradingClient

CHANNEL_ID = -1001234567890
PRIVATE_CHAT_ID = 8385255219
BOT_TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345"
OPERATOR_ID = 123456789


def _response(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def test_news_sender_adds_stable_detail_and_trade_actions_only_for_a_private_profile() -> None:
    observed: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        observed.append((method, payload))
        if method == "getChat":
            return _response({"id": PRIVATE_CHAT_ID, "type": "private"})
        if method == "getMe":
            return _response({"id": 123456, "is_bot": True})
        return _response({"message_id": 42, "chat": {"id": PRIVATE_CHAT_ID, "type": "private"}})

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=PRIVATE_CHAT_ID,
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
                {"text": "合约交易", "callback_data": "tf:trade:v1"},
            ]
        ]
    }
    sends = [payload for method, payload in observed if method in {"sendMessage", "editMessageText"}]
    assert [payload["reply_markup"] for payload in sends] == [expected, expected]


def test_trading_client_rejects_channel_callbacks_and_authorizes_only_a_matching_private_profile() -> None:
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
                        "message": {"message_id": 42, "chat": {"id": OPERATOR_ID, "type": "private"}},
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
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )

    updates = client.poll_updates(next_update_id=101)

    assert len(client.target_sha256_for(OPERATOR_ID)) == 64
    assert requests == [
        (
            "getUpdates",
            {
                "offset": 101,
                "limit": 20,
                "timeout": 0,
                "allowed_updates": ["callback_query", "message"],
            },
        )
    ]
    assert [(update.update_id, update.actor_user_id, update.authorized) for update in updates] == [
        (101, OPERATOR_ID, True),
        (102, 777, False),
    ]
    assert [update.chat_id for update in updates] == [OPERATOR_ID, CHANNEL_ID]
    assert all(update.message_id == 42 for update in updates)


def test_trading_client_accepts_callbacks_from_one_bound_private_user_chat() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.rsplit("/", maxsplit=1)[-1] == "getUpdates"
        return _response(
            [
                {
                    "update_id": 101,
                    "callback_query": {
                        "id": "callback-private",
                        "from": {"id": PRIVATE_CHAT_ID, "is_bot": False},
                        "message": {
                            "message_id": 42,
                            "chat": {"id": PRIVATE_CHAT_ID, "type": "private"},
                        },
                        "data": "tf:trade:v1",
                    },
                }
            ]
        )

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(PRIVATE_CHAT_ID,),
        transport=httpx.MockTransport(handle),
    )

    update = client.poll_updates(next_update_id=101)[0]

    assert update.chat_id == PRIVATE_CHAT_ID
    assert update.actor_user_id == PRIVATE_CHAT_ID
    assert update.authorized is True


def test_trading_client_allows_the_socket_four_seconds_within_the_total_call_budget() -> None:
    observed_timeouts: list[dict[str, float]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        observed_timeouts.append(dict(request.extensions["timeout"]))
        return _response([])

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )

    assert client.poll_updates(next_update_id=0) == ()
    assert observed_timeouts == [{"connect": 4.0, "read": 4.0, "write": 4.0, "pool": 4.0}]


def test_trading_client_answers_callback_and_edits_one_bound_interaction_message() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        requests.append((method, payload))
        if method == "answerCallbackQuery":
            return _response(True)
        return _response({"message_id": 99, "chat": {"id": OPERATOR_ID, "type": "private"}})

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )
    client.answer_callback("callback-1", text="已打开交易预览")
    message_id = client.send_interaction_reply(
        chat_id=OPERATOR_ID,
        source_message_id=42,
        text="选择交易策略",
        keyboard=(("大仓位 / 小止损", "tf:preset:tight:v1"), ("取消", "tf:cancel:v1")),
    )
    client.edit_interaction(
        chat_id=OPERATOR_ID,
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
    assert requests[1][1]["reply_markup"] == {
        "inline_keyboard": [
            [
                {"text": "大仓位 / 小止损", "callback_data": "tf:preset:tight:v1"},
                {"text": "取消", "callback_data": "tf:cancel:v1"},
            ]
        ]
    }
    assert requests[2][0] == "editMessageText"
    assert requests[2][1]["message_id"] == 99
    assert requests[2][1]["reply_markup"] == {
        "inline_keyboard": [[{"text": "确认交易", "callback_data": "tf:confirm:v1:session-1"}]]
    }


def test_trading_client_registers_a_chat_scoped_command_menu() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path.rsplit("/", maxsplit=1)[-1], json.loads(request.content)))
        return _response(True)

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )

    client.set_commands(
        chat_id=OPERATOR_ID,
        commands=(
            ("start", "查看可用指令"),
            ("test_futures", "发送合约测试新闻"),
        ),
    )

    assert requests == [
        (
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "查看可用指令"},
                    {"command": "test_futures", "description": "发送合约测试新闻"},
                ],
                "scope": {"type": "chat", "chat_id": OPERATOR_ID},
            },
        )
    ]


def test_trading_client_can_reply_with_help_without_an_inline_keyboard() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path.rsplit("/", maxsplit=1)[-1], json.loads(request.content)))
        return _response({"message_id": 100, "chat": {"id": OPERATOR_ID, "type": "private"}})

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )

    message_id = client.send_plain_reply(
        chat_id=OPERATOR_ID,
        source_message_id=42,
        text="<b>Tracefold 交易测试</b>\n\n/test_futures — 发送合约测试新闻",
    )

    assert message_id == 100
    assert requests[0][0] == "sendMessage"
    assert "reply_markup" not in requests[0][1]
    assert requests[0][1]["reply_parameters"] == {
        "message_id": 42,
        "allow_sending_without_reply": False,
    }


def test_trading_client_renders_onchain_contract_candidates_one_per_row() -> None:
    requests: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response({"message_id": 99, "chat": {"id": OPERATOR_ID, "type": "private"}})

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )
    client.send_interaction_reply(
        chat_id=OPERATOR_ID,
        source_message_id=42,
        text="选择链上合约",
        keyboard=(
            ("Robinhood · COPPERINU · 0x5317…b63b", "tf:o:c:0:session-1"),
            ("Robinhood · COPPERINU · 0xf46e…9045", "tf:o:c:1:session-1"),
            ("BNB · COPPERINU · 0xfc86…4444", "tf:o:c:2:session-1"),
            ("取消", "tf:o:x:session-1"),
        ),
    )

    assert requests[0]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "Robinhood · COPPERINU · 0x5317…b63b", "callback_data": "tf:o:c:0:session-1"}],
            [{"text": "Robinhood · COPPERINU · 0xf46e…9045", "callback_data": "tf:o:c:1:session-1"}],
            [{"text": "BNB · COPPERINU · 0xfc86…4444", "callback_data": "tf:o:c:2:session-1"}],
            [{"text": "取消", "callback_data": "tf:o:x:session-1"}],
        ]
    }


def test_trading_client_can_remove_keyboard_when_interaction_reaches_terminal_state() -> None:
    requests: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.read() and json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 99, "chat": {"id": OPERATOR_ID}}},
        )

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )
    client.edit_interaction(chat_id=OPERATOR_ID, message_id=99, text="已取消本次链上路由分析。", keyboard=())

    assert requests[0]["reply_markup"] == {"inline_keyboard": []}


def test_trading_client_treats_replayed_identical_edit_as_observed_success() -> None:
    descriptions = iter(
        (
            "Bad Request: message is not modified: specified new message content and reply markup are exactly the same",
            "Bad Request: message to edit not found",
        )
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": next(descriptions)})

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )
    client.edit_interaction(
        chat_id=OPERATOR_ID,
        message_id=99,
        text="HYPE LONG",
        keyboard=(("确认交易", "tf:confirm:v1:session-1"),),
    )

    with pytest.raises(TelegramDeliveryError, match="manual_trading_telegram_edit_http_rejected"):
        client.edit_interaction(
            chat_id=OPERATOR_ID,
            message_id=99,
            text="HYPE LONG",
            keyboard=(("确认交易", "tf:confirm:v1:session-1"),),
        )


def test_trading_client_treats_expired_callback_answer_as_terminal_effect() -> None:
    descriptions = iter(
        (
            "Bad Request: query is too old and response timeout expired or query ID is invalid",
            "Bad Request: chat not found",
        )
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": next(descriptions)})

    client = TelegramTradingClient(
        bot_token=BOT_TOKEN,
        authorized_user_ids=(OPERATOR_ID,),
        transport=httpx.MockTransport(handle),
    )
    client.answer_callback("callback-expired", text="请选择交易策略。")

    with pytest.raises(TelegramDeliveryError, match="manual_trading_telegram_callback_answer_http_rejected"):
        client.answer_callback("callback-invalid", text="请选择交易策略。")
