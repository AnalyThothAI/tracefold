"""Telegram News delivery: one configured channel and one outbound attempt."""

from __future__ import annotations

import hashlib
import json
import logging

import httpx
import pytest

from tracefold.integrations.telegram import TelegramDeliveryError, TelegramNewsPushSender

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


def _card(*, source_url: str = "https://example.test/news/1") -> dict[str, object]:
    return {
        "header": {"title": {"content": "BTC ETF 净流入"}},
        "elements": [
            {"tag": "markdown", "content": "连续第三日净流入\n利多 · 重要 · BTC"},
            {
                "tag": "action",
                "actions": [{"tag": "button", "text": {"content": "打开来源"}, "url": source_url}],
            },
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "Tracefold · abc12345"}]},
        ],
    }


def test_sender_posts_plain_text_to_only_the_configured_channel() -> None:
    observed: dict[str, object] = {}
    methods: list[str] = []
    timeout_totals: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        timeout_totals.append(sum(float(value) for value in request.extensions["timeout"].values()))
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}},
            },
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    assert methods == []

    sender.prepare()
    receipt = sender.send_card(_card())

    assert observed["chat_id"] == CHANNEL_ID
    assert observed["text"] == "BTC ETF 净流入\n\n连续第三日净流入\n利多 · 重要 · BTC\n\nTracefold · abc12345"
    assert "parse_mode" not in observed
    assert observed["link_preview_options"] == {"is_disabled": True}
    assert observed["reply_markup"] == {
        "inline_keyboard": [[{"text": "打开来源", "url": "https://example.test/news/1"}]]
    }
    assert methods == ["getChat", "getMe", "getChatMember", "sendMessage"]
    assert all(total <= 5.0 for total in timeout_totals)
    assert receipt["provider"] == "telegram"
    assert receipt["message_id"] == 42
    assert len(str(receipt["target_sha256"])) == 64
    assert str(CHANNEL_ID) not in json.dumps(receipt)


def test_http_client_info_logs_never_include_the_bot_token(caplog: pytest.LogCaptureFixture) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    caplog.set_level(logging.INFO, logger="httpx")
    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )

    sender.prepare()
    sender.send_card(_card())

    assert BOT_TOKEN not in caplog.text


def test_production_transport_injects_the_bot_token_only_in_the_wire_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire_paths: list[str] = []

    class FakeResponse:
        status = 200
        reason = "OK"

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        @staticmethod
        def getheaders() -> list[tuple[str, str]]:
            return [("content-type", "application/json")]

        def read(self, _limit: int) -> bytes:
            return json.dumps({"ok": True, "result": self.payload}).encode()

    class FakeHTTPSConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.response = FakeResponse({})

        def request(self, _verb: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            del body, headers
            wire_paths.append(path)
            method = path.rsplit("/", maxsplit=1)[-1]
            payloads: dict[str, dict[str, object]] = {
                "getChat": {"id": CHANNEL_ID, "type": "channel"},
                "getMe": {"id": BOT_ID, "is_bot": True},
                "getChatMember": {
                    "status": "administrator",
                    "user": {"id": BOT_ID, "is_bot": True},
                    "can_post_messages": True,
                },
                "sendMessage": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}},
            }
            self.response = FakeResponse(payloads[method])

        def getresponse(self) -> FakeResponse:
            return self.response

        def close(self) -> None:
            return None

    monkeypatch.setattr("tracefold.integrations.telegram.HTTPSConnection", FakeHTTPSConnection)
    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID)

    sender.prepare()
    sender.send_card(_card())

    assert wire_paths == [
        f"/bot{BOT_TOKEN}/getChat",
        f"/bot{BOT_TOKEN}/getMe",
        f"/bot{BOT_TOKEN}/getChatMember",
        f"/bot{BOT_TOKEN}/sendMessage",
    ]


def test_target_receipt_is_keyed_and_changes_when_the_bot_token_rotates() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    before_sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    before_sender.prepare()
    before = before_sender.send_card(_card())
    after_sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN_ROTATED,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    after_sender.prepare()
    after = after_sender.send_card(_card())

    unkeyed = hashlib.sha256(f"telegram-private-channel-v1:{CHANNEL_ID}".encode()).hexdigest()
    assert before["target_sha256"] != unkeyed
    assert before["target_sha256"] != after["target_sha256"]


def test_sender_rejects_a_response_from_any_other_chat() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(_request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": -1009999999999}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()

    with pytest.raises(TelegramDeliveryError, match="telegram_response_chat_mismatch"):
        sender.send_card(_card())


@pytest.mark.parametrize("chat_id", [0, 123456789, -123456789, -100, -1000000000, "@public_channel"])
def test_sender_accepts_only_a_private_channel_bot_api_id(chat_id: object) -> None:
    with pytest.raises(ValueError, match="news_push_telegram_chat_id_invalid"):
        TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=chat_id)  # type: ignore[arg-type]


def test_sender_drops_a_non_https_source_button() -> None:
    observed: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42, "chat": {"id": CHANNEL_ID}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )
    sender.prepare()
    sender.send_card(_card(source_url="http://example.test/not-allowed"))

    assert "reply_markup" not in observed


@pytest.mark.parametrize(
    ("chat", "error"),
    [
        ({"id": CHANNEL_ID, "type": "supergroup"}, "target_not_private_channel"),
        ({"id": CHANNEL_ID, "type": "channel", "username": "public_feed"}, "target_not_private_channel"),
        ({"id": -1009999999999, "type": "channel"}, "target_chat_mismatch"),
    ],
)
def test_sender_rejects_any_target_that_is_not_the_exact_private_channel(chat: dict[str, object], error: str) -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", maxsplit=1)[-1])
        return httpx.Response(200, json={"ok": True, "result": chat})

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID, transport=httpx.MockTransport(handle))
    assert methods == []

    with pytest.raises(TelegramDeliveryError, match=error):
        sender.prepare()

    assert methods == ["getChat"]
    assert "sendMessage" not in methods


def test_sender_requires_the_bot_to_have_channel_post_permission() -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method in {"getChat", "getMe"}:
            response = _preflight_response(request)
            assert response is not None
            return response
        assert method == "getChatMember"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "status": "administrator",
                    "user": {"id": BOT_ID, "is_bot": True},
                    "can_post_messages": False,
                },
            },
        )

    sender = TelegramNewsPushSender(bot_token=BOT_TOKEN, chat_id=CHANNEL_ID, transport=httpx.MockTransport(handle))
    assert methods == []

    with pytest.raises(TelegramDeliveryError, match="target_post_permission_missing"):
        sender.prepare()

    assert methods == ["getChat", "getMe", "getChatMember"]
    assert "sendMessage" not in methods


def test_sender_retries_preflight_on_a_later_event_after_transient_provider_failure() -> None:
    methods: list[str] = []
    get_chat_attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal get_chat_attempts
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getChat":
            get_chat_attempts += 1
            if get_chat_attempts == 1:
                return httpx.Response(503, json={"ok": False})
        preflight = _preflight_response(request)
        if preflight is not None:
            return preflight
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 43, "chat": {"id": CHANNEL_ID, "type": "channel"}}},
        )

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(TelegramDeliveryError, match="preflight_http_failed"):
        sender.prepare()
    sender.prepare()
    receipt = sender.send_card(_card())

    assert receipt["message_id"] == 43
    assert methods == ["getChat", "getChat", "getMe", "getChatMember", "sendMessage"]


def test_sender_never_starts_send_message_after_the_shared_call_budget_is_exhausted() -> None:
    clock = [100.0]
    methods: list[str] = []

    def monotonic() -> float:
        return clock[0]

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        assert method == "getChat"
        clock[0] += 7.1
        return httpx.Response(200, json={"ok": True, "result": {"id": CHANNEL_ID, "type": "channel"}})

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
        monotonic=monotonic,
    )

    with pytest.raises(TelegramDeliveryError, match="preflight_budget_exhausted"):
        sender.prepare()

    assert methods == ["getChat"]


def test_sender_refuses_to_send_before_target_preflight() -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", maxsplit=1)[-1])
        return httpx.Response(500)

    sender = TelegramNewsPushSender(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL_ID,
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(TelegramDeliveryError, match="target_not_prepared"):
        sender.send_card(_card())

    assert methods == []
