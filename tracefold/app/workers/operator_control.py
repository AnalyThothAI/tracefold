"""Workers-owned ingress transaction for authenticated Trading commands."""

from __future__ import annotations

import time

from fastapi import Request
from fastapi.responses import JSONResponse

from tracefold.app.operator_control import (
    OperatorIntentReceipt,
)
from tracefold.app.operator_control import (
    persist_operator_intent as persist_operator_intent_in_transaction,
)
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.integrations.telegram_control import (
    TELEGRAM_CONTROL_MAX_BODY_BYTES,
    TelegramControlError,
    TelegramControlWebhook,
    telegram_webhook_reply,
)
from tracefold.trading import PreparedOperatorIntent

_DB_TIMEOUT_SECONDS = 5.0


class WorkersTelegramControl:
    """HTTP adapter whose successful command reply proves only durable intent recording."""

    def __init__(self, *, webhook: TelegramControlWebhook, db: WorkerTradingDatabase) -> None:
        self._webhook = webhook
        self._db = db

    async def handle(self, request: Request) -> JSONResponse:
        try:
            body = await _bounded_request_body(request)
            parsed = self._webhook.parse(
                headers=request.headers,
                body=body,
                received_at_ns=time.time_ns(),
            )
            if parsed.intent is None:
                active = await self._db.read(
                    "trading_operator_status",
                    lambda repos: repos.trading.execution_profile_activation(parsed.target_profile_id),
                    timeout_seconds=_DB_TIMEOUT_SECONDS,
                )
                text = (
                    "状态已读取：执行 profile 已激活。"
                    if active is not None
                    else "状态已读取：当前无活动执行 profile。"
                )
            else:
                await persist_operator_intent(self._db, parsed.intent)
                text = "意图已记录。"
            return JSONResponse(telegram_webhook_reply(chat_id=parsed.chat_id, text=text))
        except TelegramControlError as exc:
            return JSONResponse({"ok": False, "error": exc.code}, status_code=exc.status_code)
        except ValueError:
            return JSONResponse({"ok": False, "error": "telegram_control_body_invalid"}, status_code=400)


async def persist_operator_intent(
    db: WorkerTradingDatabase,
    prepared: PreparedOperatorIntent,
) -> OperatorIntentReceipt:
    return await db.tx(
        "trading_operator_intent_append",
        lambda repos: persist_operator_intent_in_transaction(repos.trading, prepared),
        timeout_seconds=_DB_TIMEOUT_SECONDS,
    )


async def _bounded_request_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            raise TelegramControlError("telegram_control_body_invalid", status_code=400) from None
        if declared_bytes < 0 or declared_bytes > TELEGRAM_CONTROL_MAX_BODY_BYTES:
            raise TelegramControlError("telegram_control_body_invalid", status_code=400)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > TELEGRAM_CONTROL_MAX_BODY_BYTES:
            raise TelegramControlError("telegram_control_body_invalid", status_code=400)
        body.extend(chunk)
    return bytes(body)


__all__ = ["OperatorIntentReceipt", "WorkersTelegramControl", "persist_operator_intent"]
