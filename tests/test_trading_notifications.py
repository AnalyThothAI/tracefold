from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.workers.trading_notifications import (
    TradingNotificationWorker,
    trading_notification_text,
)
from tracefold.integrations.telegram import TelegramDeliveryError


class _Trading:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.deliveries: dict[tuple[str, int], dict[str, Any]] = {}

    def next_execution_notification(self, target_sha256: str) -> dict[str, Any] | None:
        return None if (target_sha256, self.row["seq"]) in self.deliveries else self.row

    def append_execution_notification_delivery(
        self,
        *,
        target_sha256: str,
        observation_seq: int,
        message_id: int,
        delivered_at_ns: int,
    ) -> dict[str, Any]:
        assert target_sha256 == "a" * 64
        assert observation_seq == self.row["seq"]
        assert message_id == 91
        assert delivered_at_ns > 0
        receipt = {
            "target_sha256": target_sha256,
            "observation_seq": observation_seq,
            "message_id": message_id,
            "delivered_at_ns": delivered_at_ns,
        }
        self.deliveries.setdefault((target_sha256, observation_seq), receipt)
        return self.deliveries[(target_sha256, observation_seq)]


class _Database:
    def __init__(self, trading: _Trading) -> None:
        self.repos = SimpleNamespace(trading=trading)

    async def read(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
        assert timeout_seconds > 0
        return fn(self.repos)

    async def tx(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
        assert timeout_seconds > 0
        return fn(self.repos)


class _Finite:
    async def run(self, _name: str, fn: Any, *args: Any, timeout_seconds: float) -> Any:
        assert timeout_seconds > 0
        return fn(*args)


class _Sender:
    target_sha256 = "a" * 64

    def __init__(self) -> None:
        self.available = False
        self.messages: list[str] = []

    def prepare(self) -> None:
        if not self.available:
            raise TelegramDeliveryError("telegram-down")

    def send(self, text: str) -> int:
        self.messages.append(text)
        return 91

    def close(self) -> None:
        return None


def _row(**updates: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": 7,
        "event_id": "b" * 64,
        "runtime_profile_id": "demo-v1",
        "signal_id": None,
        "command_id": "c" * 64,
        "normalized_kind": "control_disposition",
        "summary": {"disposition": "accepted"},
    }
    row.update(updates)
    return row


def test_telegram_outage_never_appends_delivery_or_raises_into_workers_root() -> None:
    trading = _Trading(_row())
    sender = _Sender()
    worker = TradingNotificationWorker(
        db=_Database(trading),  # type: ignore[arg-type]
        finite=_Finite(),  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
    )

    assert asyncio.run(worker.advance_once()) == "delivery_unavailable"
    assert trading.deliveries == {}
    assert sender.messages == []

    sender.available = True
    assert asyncio.run(worker.advance_once()) == "sent"
    assert set(trading.deliveries) == {("a" * 64, 7)}
    assert asyncio.run(worker.advance_once()) == "idle"
    assert sender.messages == [
        "Tracefold execution: Command accepted\n"
        "profile: demo-v1\n"
        "correlation: cccccccccccccccc\n"
        "event: bbbbbbbbbbbbbbbb"
    ]


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"normalized_kind": "readiness", "summary": {"control_stage": "runtime_accepted"}}, "Runtime accepted"),
        ({"normalized_kind": "signal_disposition", "summary": {"disposition": "accepted"}}, "Signal accepted"),
        ({"normalized_kind": "order", "summary": {"status": "accepted"}}, "Order accepted"),
        ({"normalized_kind": "fill", "summary": {}}, "Fill observed"),
        ({"normalized_kind": "audit_gap", "summary": {"cause": "overflow"}}, "Audit gap"),
    ],
)
def test_notification_projection_keeps_runtime_order_fill_and_gap_stages_distinct(
    updates: dict[str, object], expected: str
) -> None:
    text = trading_notification_text(_row(**updates))
    assert text is not None and f"Tracefold execution: {expected}" in text


def test_non_operator_observation_is_skipped_without_inventing_a_notification_stage() -> None:
    assert trading_notification_text(_row(normalized_kind="position", summary={"status": "opened"})) is None
