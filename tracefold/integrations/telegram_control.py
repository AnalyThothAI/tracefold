"""Authenticated Telegram webhook adapter for Trading operator commands."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tracefold.trading import (
    OperatorCommandError,
    ParsedOperatorCommand,
    PreparedOperatorIntent,
    parse_operator_command,
    prepare_parsed_operator_intent,
)

TELEGRAM_WEBHOOK_SECRET_HEADER = "x-telegram-bot-api-secret-token"
TELEGRAM_CONTROL_MAX_BODY_BYTES = 16 * 1_024
_MAX_FUTURE_SKEW_NS = 30 * 1_000_000_000
_WEBHOOK_SECRET = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


class TelegramControlError(ValueError):
    """A stable webhook refusal with no credential or payload echo."""

    def __init__(self, code: str, *, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TelegramControlRequest:
    update_id: int
    chat_id: int
    user_id: int
    message_id: int
    target_profile_id: str
    command: ParsedOperatorCommand
    intent: PreparedOperatorIntent | None


class TelegramControlWebhook:
    """Authenticate one update, apply the closed grammar, and bind a stable intent identity."""

    def __init__(
        self,
        *,
        webhook_secret: str,
        bot_id: int,
        allowed_chat_ids: frozenset[int],
        allowed_user_ids: frozenset[int],
        target_profile_id: str,
    ) -> None:
        if _WEBHOOK_SECRET.fullmatch(webhook_secret) is None:
            raise ValueError("telegram_control_webhook_secret_invalid")
        if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0:
            raise ValueError("telegram_control_bot_identity_invalid")
        if not allowed_chat_ids or not allowed_user_ids:
            raise ValueError("telegram_control_allowlist_empty")
        self._webhook_secret = webhook_secret
        self._bot_id = bot_id
        self._allowed_chat_ids = allowed_chat_ids
        self._allowed_user_ids = allowed_user_ids
        self._target_profile_id = target_profile_id

    def parse(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        received_at_ns: int,
    ) -> TelegramControlRequest:
        supplied_secret = next(
            (value for key, value in headers.items() if key.lower() == TELEGRAM_WEBHOOK_SECRET_HEADER),
            "",
        )
        if not hmac.compare_digest(supplied_secret, self._webhook_secret):
            raise TelegramControlError("telegram_control_unauthenticated", status_code=401)
        if not body or len(body) > TELEGRAM_CONTROL_MAX_BODY_BYTES:
            raise TelegramControlError("telegram_control_body_invalid", status_code=400)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramControlError("telegram_control_body_invalid", status_code=400) from None
        update_id, message_id, chat_id, user_id, sent_at_ns, text = _message(payload)
        if chat_id not in self._allowed_chat_ids or user_id not in self._allowed_user_ids:
            raise TelegramControlError("telegram_control_forbidden", status_code=403)
        if sent_at_ns > received_at_ns + _MAX_FUTURE_SKEW_NS:
            raise TelegramControlError("telegram_control_clock_invalid", status_code=400)
        try:
            command = parse_operator_command(text)
            intent = (
                None
                if command.kind == "status"
                else prepare_parsed_operator_intent(
                    command,
                    source=f"telegram:bot:{self._bot_id}",
                    source_command_id=str(update_id),
                    target_profile_id=self._target_profile_id,
                    operator_identity=f"telegram:user:{user_id}",
                    authentication_identity="telegram:webhook+allowlist:v1",
                    requested_at_ns=sent_at_ns,
                )
            )
        except OperatorCommandError as exc:
            raise TelegramControlError(exc.code, status_code=400) from None
        if intent is not None and intent.value.expires_at_ns <= received_at_ns:
            raise TelegramControlError("operator_command_expired", status_code=400)
        return TelegramControlRequest(
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            target_profile_id=self._target_profile_id,
            command=command,
            intent=intent,
        )


def telegram_webhook_reply(*, chat_id: int, text: str) -> dict[str, Any]:
    """Use Telegram's webhook response path; this is an acknowledgement, not order evidence."""

    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }


def _message(payload: object) -> tuple[int, int, int, int, int, str]:
    if not isinstance(payload, dict):
        raise TelegramControlError("telegram_control_body_invalid", status_code=400)
    update_id = _integer(payload.get("update_id"))
    message = payload.get("message")
    if not isinstance(message, dict):
        raise TelegramControlError("telegram_control_message_invalid", status_code=400)
    chat = message.get("chat")
    sender = message.get("from")
    text = message.get("text")
    if not isinstance(chat, dict) or not isinstance(sender, dict) or not isinstance(text, str):
        raise TelegramControlError("telegram_control_message_invalid", status_code=400)
    message_id = _integer(message.get("message_id"))
    chat_id = _integer(chat.get("id"), signed=True)
    user_id = _integer(sender.get("id"))
    sent_at_seconds = _integer(message.get("date"))
    if message_id <= 0 or chat_id == 0 or user_id <= 0 or not text or sent_at_seconds <= 0:
        raise TelegramControlError("telegram_control_message_invalid", status_code=400)
    return update_id, message_id, chat_id, user_id, sent_at_seconds * 1_000_000_000, text


def _integer(value: object, *, signed: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelegramControlError("telegram_control_message_invalid", status_code=400)
    if (not signed and value < 0) or value < -(2**63) or value >= 2**63:
        raise TelegramControlError("telegram_control_message_invalid", status_code=400)
    return value


__all__ = [
    "TELEGRAM_CONTROL_MAX_BODY_BYTES",
    "TELEGRAM_WEBHOOK_SECRET_HEADER",
    "TelegramControlError",
    "TelegramControlRequest",
    "TelegramControlWebhook",
    "telegram_webhook_reply",
]
