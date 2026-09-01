"""Best-effort asynchronous delivery of durable execution observations."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from loguru import logger

from tracefold.app.workers.capabilities import FiniteOperations
from tracefold.app.workers.wiring.database import WorkerTradingDatabase
from tracefold.integrations.telegram import TelegramDeliveryError, TelegramTradingNotifier
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

TRADING_NOTIFICATION_TASK_NAME = "trading-observation-notifier"
_POLL_SECONDS = 2.0
_RETRY_SECONDS = 5.0
_DB_TIMEOUT_SECONDS = 5.0
_SEND_TIMEOUT_SECONDS = 8.0


class TradingNotificationWorker:
    """Append a durable receipt only after a non-blocking notification turn succeeds."""

    def __init__(
        self,
        *,
        db: WorkerTradingDatabase,
        finite: FiniteOperations,
        sender: TelegramTradingNotifier,
    ) -> None:
        self._db = db
        self._finite = finite
        self._sender = sender
        self._prepared = False

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            outcome = await self.advance_once()
            delay = _RETRY_SECONDS if outcome == "delivery_unavailable" else _POLL_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)

    async def advance_once(self) -> str:
        row = await self._db.read(
            "trading_notification_observation",
            lambda repos: repos.trading.next_execution_notification(self._sender.target_sha256),
            timeout_seconds=_DB_TIMEOUT_SECONDS,
        )
        if row is None:
            return "idle"
        text = trading_notification_text(row)
        if text is None:
            raise RuntimeError("trading_notification_projection_drift")
        try:
            if not self._prepared:
                await self._finite.run(
                    "trading_notification_prepare",
                    self._sender.prepare,
                    timeout_seconds=_SEND_TIMEOUT_SECONDS,
                )
                self._prepared = True
            message_id = await self._finite.run(
                "trading_notification_send",
                self._sender.send,
                text,
                timeout_seconds=_SEND_TIMEOUT_SECONDS,
            )
        except (TelegramDeliveryError, ResourceAdmissionTimeout, ResourceOperationOverrun) as exc:
            self._prepared = False
            logger.bind(error=type(exc).__name__).warning("Trading Telegram notification unavailable")
            return "delivery_unavailable"
        await self._db.tx(
            "trading_notification_delivery_append",
            lambda repos: repos.trading.append_execution_notification_delivery(
                target_sha256=self._sender.target_sha256,
                observation_seq=int(row["seq"]),
                message_id=int(message_id),
                delivered_at_ns=time.time_ns(),
            ),
            timeout_seconds=_DB_TIMEOUT_SECONDS,
        )
        return "sent"

    def close(self) -> None:
        self._sender.close()


def trading_notification_text(row: dict[str, Any]) -> str | None:
    """Project only operator-relevant stages; an HTTP or Signal fact is never called a fill."""

    kind = str(row.get("normalized_kind") or "")
    summary = row.get("summary")
    values: dict[str, Any] = summary if isinstance(summary, dict) else {}
    stage: str | None = None
    if kind == "signal_disposition":
        stage = f"Signal {values.get('disposition', 'disposed')}"
    elif kind == "readiness" and values.get("control_stage") == "runtime_accepted":
        stage = "Runtime accepted"
    elif kind == "control_disposition":
        stage = f"Command {values.get('disposition', 'disposed')}"
    elif kind == "order" and values.get("status") in {
        "accepted",
        "rejected",
        "denied",
        "expired",
        "submitted",
        "submitted_or_unknown",
    }:
        stage = f"Order {values['status']}"
    elif kind == "fill":
        stage = "Fill observed"
    elif kind == "reconciliation" and values.get("state") == "flat":
        stage = "Account flat observed"
    elif kind == "audit_gap":
        stage = "Audit gap"
    if stage is None:
        return None
    event_id = str(row.get("event_id") or "")
    profile = str(row.get("runtime_profile_id") or "")
    command_id = str(row.get("command_id") or "")
    signal_id = str(row.get("signal_id") or "")
    correlation = command_id or signal_id
    return "\n".join(
        (
            f"Tracefold execution: {stage}",
            f"profile: {profile}",
            f"correlation: {correlation[:16] or '-'}",
            f"event: {event_id[:16] or '-'}",
        )
    )


__all__ = ["TRADING_NOTIFICATION_TASK_NAME", "TradingNotificationWorker", "trading_notification_text"]
