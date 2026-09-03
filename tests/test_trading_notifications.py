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
from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceCapability,
    ResourceOperationOverrun,
)
from tracefold.trading.notification_policy import (
    NOTIFIABLE_KINDS,
    is_notifiable,
    notifiable_policy_rows,
)


class _Trading:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.deliveries: dict[tuple[str, int], dict[str, Any]] = {}

    def next_execution_notification(self, target_sha256: str, *, now_ns: int) -> dict[str, Any] | None:
        assert now_ns > 0
        return None if (target_sha256, self.row["seq"]) in self.deliveries else self.row

    def append_execution_notification_delivery(
        self,
        *,
        target_sha256: str,
        observation_seq: int,
        message_id: int,
        delivered_at_ns: int,
        selected_at_ns: int,
    ) -> dict[str, Any]:
        assert target_sha256 == "a" * 64
        assert observation_seq == self.row["seq"]
        assert message_id == 91
        assert delivered_at_ns > 0
        # The guard has to re-derive the candidate with the clock the caller chose it with, never a
        # later one, or a throttle window expiring mid-send would move the candidate underneath it.
        assert 0 < selected_at_ns <= delivered_at_ns
        receipt = {
            "target_sha256": target_sha256,
            "observation_seq": observation_seq,
            "message_id": message_id,
            "delivered_at_ns": delivered_at_ns,
        }
        self.deliveries.setdefault((target_sha256, observation_seq), receipt)
        return self.deliveries[(target_sha256, observation_seq)]


class _Database:
    def __init__(
        self,
        trading: _Trading,
        *,
        read_error: BaseException | None = None,
        tx_error: BaseException | None = None,
    ) -> None:
        self.repos = SimpleNamespace(trading=trading)
        self.read_error = read_error
        self.tx_error = tx_error

    async def read(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
        assert timeout_seconds > 0
        if self.read_error is not None:
            raise self.read_error
        return fn(self.repos)

    async def tx(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
        assert timeout_seconds > 0
        if self.tx_error is not None:
            raise self.tx_error
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
        "account_slot": "demo-v1",
        "signal_id": None,
        "command_id": "c" * 64,
        "normalized_kind": "control_disposition",
        "occurred_at_ns": 1_756_712_112_000_000_000,
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
        "at: 2025-09-01T07:35:12Z\n"
        "account: demo-v1\n"
        "correlation: cccccccccccccccc\n"
        "event: bbbbbbbbbbbbbbbb"
    ]


def test_notification_read_admission_timeout_retries_without_terminating_workers() -> None:
    trading = _Trading(_row())
    sender = _Sender()
    worker = TradingNotificationWorker(
        db=_Database(trading, read_error=ResourceAdmissionTimeout()),  # type: ignore[arg-type]
        finite=_Finite(),  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
    )

    assert asyncio.run(worker.advance_once()) == "delivery_unavailable"
    assert trading.deliveries == {}
    assert sender.messages == []


def test_notification_receipt_overrun_retries_the_unreceipted_delivery() -> None:
    trading = _Trading(_row())
    sender = _Sender()
    sender.available = True
    worker = TradingNotificationWorker(
        db=_Database(
            trading,
            tx_error=ResourceOperationOverrun(
                capability=ResourceCapability.DATABASE_BUSINESS,
                operation_name="trading_notification_delivery_append",
            ),
        ),  # type: ignore[arg-type]
        finite=_Finite(),  # type: ignore[arg-type]
        sender=sender,  # type: ignore[arg-type]
    )

    assert asyncio.run(worker.advance_once()) == "delivery_unavailable"
    assert trading.deliveries == {}
    assert len(sender.messages) == 1


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"normalized_kind": "readiness", "summary": {"action": "flatten", "control_stage": "runtime_accepted"}},
            "Runtime accepted flatten",
        ),
        (
            {"normalized_kind": "readiness", "summary": {"lifecycle": "started", "mode": "paper"}},
            "Runtime started in paper",
        ),
        ({"normalized_kind": "signal_disposition", "summary": {"disposition": "accepted"}}, "Signal accepted"),
        ({"normalized_kind": "order", "summary": {"status": "accepted"}}, "Order accepted"),
        # #472: the status enumeration is gone, so a recovery-path status is stated rather than dropped.
        ({"normalized_kind": "order", "summary": {"status": "canceled"}}, "Order canceled"),
        ({"normalized_kind": "fill", "summary": {}}, "Fill observed"),
        ({"normalized_kind": "audit_gap", "summary": {"cause": "overflow"}}, "Audit gap: overflow"),
        (
            {"normalized_kind": "reconciliation", "summary": {"account_flat": False, "positions": 1, "orders": 2}},
            "Account not flat",
        ),
    ],
)
def test_notification_projection_keeps_runtime_order_fill_and_gap_stages_distinct(
    updates: dict[str, object], expected: str
) -> None:
    text = trading_notification_text(_row(**updates))
    assert text is not None and f"Tracefold execution: {expected}" in text


def test_non_operator_observation_is_skipped_without_inventing_a_notification_stage() -> None:
    assert trading_notification_text(_row(normalized_kind="position", summary={"status": "opened"})) is None
    # The flat steady state is why `reconciliation` needs a summary test at all: it arrives on a timer
    # and says nothing an operator must act on.
    assert trading_notification_text(_row(normalized_kind="reconciliation", summary={"account_flat": True})) is None


def test_the_sql_policy_rows_and_the_python_predicate_state_the_same_vocabulary() -> None:
    """#472: one home, two readings — the arrays PostgreSQL matches on must agree with `is_notifiable`.

    The predicate used to exist three times over, in SQL, in a partial index, and in this renderer,
    and two of the three asked for summary keys no writer produced. Deriving both readings from one
    policy is only worth anything if a test refuses to let them diverge again.
    """

    kinds, keys, values = notifiable_policy_rows()
    assert len(kinds) == len(keys) == len(values)
    assert set(kinds) == set(NOTIFIABLE_KINDS)
    for kind, key, value in zip(kinds, keys, values, strict=True):
        summary: dict[str, object] = {}
        if key:
            summary[key] = {"true": True, "false": False}.get(value, value)
        assert is_notifiable(kind, summary) is True, f"{kind}/{key}={value} is SQL-notifiable but Python is not"
        assert trading_notification_text(_row(normalized_kind=kind, summary=summary)) is not None, (
            f"{kind}/{key}={value} is notifiable but the renderer states no stage for it"
        )


def test_a_card_dates_itself_from_the_observation_rather_than_the_send() -> None:
    """A coalesced kind can be minutes old when it leaves, so the instant has to be on the card."""

    text = trading_notification_text(_row(occurred_at_ns=1_756_712_112_000_000_000))
    assert text is not None and "at: 2025-09-01T07:35:12Z" in text
