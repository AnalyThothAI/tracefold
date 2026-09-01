from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from tracefold.app.workers.operator_control import _bounded_request_body
from tracefold.integrations.telegram_control import TelegramControlError, TelegramControlWebhook
from tracefold.platform.config.models import Settings
from tracefold.trading.operator_control import OperatorCommandError, parse_operator_command

_SECRET = "webhook-secret-for-tests"
_CHAT_ID = -1001234567890
_USER_ID = 7001
_SENT_AT_SECONDS = 1_800_000_000


def _body(text: str, *, update_id: int = 91, chat_id: int = _CHAT_ID, user_id: int = _USER_ID) -> bytes:
    return json.dumps(
        {
            "update_id": update_id,
            "message": {
                "message_id": 52,
                "date": _SENT_AT_SECONDS,
                "chat": {"id": chat_id, "type": "supergroup"},
                "from": {"id": user_id, "is_bot": False},
                "text": text,
            },
        }
    ).encode()


def _webhook() -> TelegramControlWebhook:
    return TelegramControlWebhook(
        webhook_secret=_SECRET,
        allowed_chat_ids=frozenset({_CHAT_ID}),
        allowed_user_ids=frozenset({_USER_ID}),
        target_profile_id="binance-usdm-demo-v1",
    )


@pytest.mark.parametrize(
    ("text", "action", "scope", "ttl", "market", "direction", "confirmed"),
    [
        ("/pause investigate feed", "pause_entries", "entries", 300, None, None, False),
        ("/resume incident cleared CONFIRM", "resume_entries", "entries", 300, None, None, True),
        ("/halt unexpected exposure CONFIRM", "emergency_halt", "account", 300, None, None, True),
        ("/flatten account 30 CONFIRM", "flatten", "account", 30, None, None, True),
        ("/long crypto:perp:BTC:USDT 20", "manual_entry", "market", 20, "crypto:perp:BTC:USDT", "long", False),
        ("/short crypto:perp:ETH:USDT 15", "manual_entry", "market", 15, "crypto:perp:ETH:USDT", "short", False),
    ],
)
def test_closed_command_parser_carries_no_capital_parameters(
    text: str,
    action: str,
    scope: str,
    ttl: int,
    market: str | None,
    direction: str | None,
    confirmed: bool,
) -> None:
    parsed = parse_operator_command(text)

    assert parsed.action == action
    assert parsed.scope == scope
    assert parsed.ttl_seconds == ttl
    assert parsed.market_key == market
    assert parsed.direction == direction
    assert parsed.confirmed is confirmed
    assert not hasattr(parsed, "quantity")
    assert not hasattr(parsed, "leverage")


@pytest.mark.parametrize(
    "text",
    [
        "/resume incident cleared",
        "/halt CONFIRM",
        "/flatten account 121 CONFIRM",
        "/flatten account 30",
        "/flatten symbol 30 CONFIRM",
        "/long crypto:perp:BTC:USDT 20 1BTC",
        "/long crypto/perp/BTC/USDT 20",
        "/short crypto:perp:BTC:USDT 20 leverage=5",
        "/unknown reason",
        " /status",
    ],
)
def test_closed_command_parser_rejects_missing_confirmation_bad_ttl_and_extra_parameters(text: str) -> None:
    with pytest.raises(OperatorCommandError):
        parse_operator_command(text)


def test_status_is_read_only_and_creates_no_intent() -> None:
    parsed = _webhook().parse(
        headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        body=_body("/status"),
        received_at_ns=(_SENT_AT_SECONDS + 1) * 1_000_000_000,
    )

    assert parsed.command.kind == "status"
    assert parsed.intent is None


def test_authenticated_allowlisted_update_is_stable_and_secret_free() -> None:
    webhook = _webhook()
    kwargs = {
        "headers": {"X-Telegram-Bot-Api-Secret-Token": _SECRET},
        "body": _body("/flatten account 30 CONFIRM"),
        "received_at_ns": (_SENT_AT_SECONDS + 1) * 1_000_000_000,
    }

    first = webhook.parse(**kwargs)
    retried = webhook.parse(**kwargs)

    assert first.intent is not None
    assert retried.intent is not None
    assert first.intent.payload_json == retried.intent.payload_json
    assert first.intent.value.command_id == retried.intent.value.command_id
    assert first.intent.value.operator_identity == f"telegram:user:{_USER_ID}"
    assert _SECRET not in first.intent.payload_json
    assert first.intent.value.expires_at_ns - first.intent.value.requested_at_ns == 30_000_000_000


@pytest.mark.parametrize(
    ("headers", "body", "code", "status"),
    [
        ({}, _body("/pause investigate"), "telegram_control_unauthenticated", 401),
        (
            {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            _body("/pause investigate"),
            "telegram_control_unauthenticated",
            401,
        ),
        (
            {"X-Telegram-Bot-Api-Secret-Token": _SECRET},
            _body("/pause investigate", chat_id=-1009876543210),
            "telegram_control_forbidden",
            403,
        ),
        (
            {"X-Telegram-Bot-Api-Secret-Token": _SECRET},
            _body("/pause investigate", user_id=9999),
            "telegram_control_forbidden",
            403,
        ),
    ],
)
def test_webhook_authentication_and_both_allowlists_fail_closed(
    headers: dict[str, str], body: bytes, code: str, status: int
) -> None:
    with pytest.raises(TelegramControlError, match=code) as raised:
        _webhook().parse(
            headers=headers,
            body=body,
            received_at_ns=(_SENT_AT_SECONDS + 1) * 1_000_000_000,
        )

    assert raised.value.status_code == status


def test_expired_update_creates_no_intent() -> None:
    with pytest.raises(TelegramControlError, match="operator_command_expired"):
        _webhook().parse(
            headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
            body=_body("/long crypto:perp:BTC:USDT 5"),
            received_at_ns=(_SENT_AT_SECONDS + 6) * 1_000_000_000,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message_id", 0),
        ("chat_id", 0),
        ("user_id", 0),
    ],
)
def test_webhook_rejects_zero_telegram_identities(field: str, value: int) -> None:
    payload = json.loads(_body("/pause investigate"))
    message = payload["message"]
    if field == "chat_id":
        message["chat"]["id"] = value
    elif field == "user_id":
        message["from"]["id"] = value
    else:
        message[field] = value

    with pytest.raises(TelegramControlError, match="telegram_control_message_invalid"):
        _webhook().parse(
            headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
            body=json.dumps(payload).encode(),
            received_at_ns=(_SENT_AT_SECONDS + 1) * 1_000_000_000,
        )


def test_chunked_webhook_body_is_rejected_before_unbounded_accumulation() -> None:
    chunks = iter((b"a" * (16 * 1_024), b"b"))

    async def receive() -> dict[str, object]:
        chunk = next(chunks)
        return {"type": "http.request", "body": chunk, "more_body": chunk[-1:] != b"b"}

    request = Request(
        {"type": "http", "method": "POST", "path": "/telegram/control", "headers": []},
        receive,
    )

    async def read() -> None:
        with pytest.raises(TelegramControlError, match="telegram_control_body_invalid"):
            await _bounded_request_body(request)

    asyncio.run(read())


def test_enabled_control_requires_complete_sorted_allowlists_and_notification_target() -> None:
    valid = Settings.model_validate(
        {
            "trading": {
                "control": {
                    "enabled": True,
                    "allowed_chat_ids": [_CHAT_ID],
                    "allowed_user_ids": [_USER_ID],
                    "notification_chat_id": _CHAT_ID,
                }
            }
        }
    )
    assert valid.trading.control.enabled is True

    for invalid in (
        {"enabled": True},
        {
            "enabled": True,
            "allowed_chat_ids": [_CHAT_ID],
            "allowed_user_ids": [_USER_ID],
            "notification_chat_id": -1009999999999,
        },
        {
            "enabled": True,
            "allowed_chat_ids": [_CHAT_ID, _CHAT_ID],
            "allowed_user_ids": [_USER_ID],
            "notification_chat_id": _CHAT_ID,
        },
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"trading": {"control": invalid}})
