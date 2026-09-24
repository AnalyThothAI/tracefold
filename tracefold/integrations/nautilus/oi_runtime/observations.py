"""Translate Runtime verdicts and Nautilus events into durable observations.

One writer, so every observation of one entry carries the same correlation: a Signal's `signal_id` or
a manual Command's `command_id`, which is also the plan's `entry_id`. Exposure no plan claims is
recorded without one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final, Literal

from tracefold.trading.execution_contracts import ExecutionObservationV1, OperatorIntentV1

from .account_projection import OrderLeg
from .funding import FundingCashflow
from .journal import ExecutionJournal
from .risk import decimal_value
from .signal_client import ExecutionSignalClient

# The venue's own words for a refusal, bounded by what `ExecutionObservationV1` metadata accepts for
# one string.
_MAX_TEXT_BYTES: Final[int] = 256


def bounded_text(value: str) -> str:
    """Text cut to whole characters inside the metadata string bound."""

    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_TEXT_BYTES:
        return value
    return encoded[:_MAX_TEXT_BYTES].decode("utf-8", "ignore")


def _decimal_text(value: Any) -> str:
    return format(decimal_value(value).normalize(), "f")


class RuntimeObservations:
    """The sole native-event and verdict to `ExecutionObservationV1` translator."""

    def __init__(
        self,
        *,
        journal: ExecutionJournal,
        signals: ExecutionSignalClient,
        timestamp_ns: Callable[[], int],
    ) -> None:
        self._journal = journal
        self._factory = journal.factory
        self._signals = signals
        self._timestamp_ns = timestamp_ns

    @staticmethod
    def correlation(source: str | None, entry_id: str | None) -> dict[str, str]:
        if entry_id is None or source is None:
            return {}
        return {"command_id": entry_id} if source == "manual" else {"signal_id": entry_id}

    def _offer(self, value: ExecutionObservationV1) -> bool:
        return self._journal.offer(value)

    def funding(self, flow: FundingCashflow) -> bool:
        """One actual venue cashflow, keyed by the venue's transaction identity."""
        identity = f"{self._factory.account_slot}:FUNDING_FEE:{flow.transaction_id}"
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        return self._offer(
            self._factory.create(
                normalized_kind="funding",
                occurred_at_ns=flow.occurred_at_ms * 1_000_000,
                observed_at_ns=self._timestamp_ns(),
                native_identity_references=(flow.transaction_id,),
                summary={
                    "venue": "binance.usdm",
                    "source": "signed_income_v1",
                    "venue_transaction_id": flow.transaction_id,
                    "symbol": flow.symbol,
                    "asset": flow.asset,
                    "amount_decimal": format(flow.amount, "f"),
                },
                fixed_event_id=event_id,
            )
        )

    def funding_coverage(self, start_ms: int, end_ms: int) -> bool:
        """A fully paginated signed read proves cashflows absent within its window."""
        identity = f"{self._factory.account_slot}:FUNDING_FEE:coverage:{start_ms}:{end_ms}"
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        return self._offer(
            self._factory.create(
                normalized_kind="funding_coverage",
                occurred_at_ns=end_ms * 1_000_000,
                observed_at_ns=self._timestamp_ns(),
                summary={
                    "venue": "binance.usdm",
                    "source": "signed_income_v1",
                    "start_at_ns": start_ms * 1_000_000,
                    "end_at_ns": end_ms * 1_000_000,
                    "status": "complete",
                },
                fixed_event_id=event_id,
            )
        )

    # -- verdicts on the two inputs ----------------------------------------------------------------

    def dispose_signal(self, signal_id: str, reason: str, detail: dict[str, str] | None = None) -> None:
        """Write the one terminal verdict on a Signal. A full journal hands the Signal back instead."""

        now_ns = self._timestamp_ns()
        summary: dict[str, str | int | bool] = {"disposition": reason, **(detail or {})}
        offered = self._offer(
            self._factory.create(
                normalized_kind="signal_disposition",
                signal_id=signal_id,
                occurred_at_ns=now_ns,
                observed_at_ns=now_ns,
                summary=summary,
                event_identity="final",
            )
        )
        if not offered:
            self._signals.release(signal_id)

    def dispose_command(
        self,
        command_id: str,
        *,
        action: str,
        disposition: Literal["accepted", "rejected"],
        reason: str,
        detail: dict[str, str] | None = None,
    ) -> None:
        now_ns = self._timestamp_ns()
        summary: dict[str, str | int | bool] = {
            "action": action,
            "disposition": disposition,
            "reason": reason,
            **(detail or {}),
        }
        offered = self._offer(
            self._factory.create(
                normalized_kind="control_disposition",
                command_id=command_id,
                occurred_at_ns=now_ns,
                observed_at_ns=now_ns,
                summary=summary,
                event_identity="final",
            )
        )
        if not offered:
            self._signals.release_command(command_id)

    def dispose_entry(
        self,
        *,
        source: Literal["signal", "manual"],
        entry_id: str,
        reason: str,
        detail: dict[str, str] | None = None,
    ) -> None:
        """A Signal's verdict is its reason; a manual Command's is accepted or rejected, with the reason."""

        if source == "signal":
            self.dispose_signal(entry_id, reason, detail)
            return
        self.dispose_command(
            entry_id,
            action="manual_entry",
            disposition="accepted" if reason == "accepted" else "rejected",
            reason=reason,
            detail=detail,
        )

    def reject_command(self, command: OperatorIntentV1, reason: str) -> None:
        self.dispose_command(command.command_id, action=command.action, disposition="rejected", reason=reason)

    def accept_command(self, command: OperatorIntentV1, reason: str, detail: dict[str, str] | None = None) -> None:
        self.dispose_command(
            command.command_id, action=command.action, disposition="accepted", reason=reason, detail=detail
        )

    # -- Nautilus events ---------------------------------------------------------------------------

    def order(
        self,
        *,
        correlation: dict[str, str],
        client_order_id: str,
        leg: OrderLeg,
        status: str,
        occurred_at_ns: int,
        reason: str | None = None,
        trigger_price: Any = None,
        venue_order_id: str | None = None,
    ) -> None:
        """One order lifecycle step. A stop or take-profit is `protection`; entries and exits are `order`."""

        summary: dict[str, str | int | bool] = {"leg": leg, "status": status}
        if reason:
            summary["reason"] = bounded_text(reason)
        if trigger_price is not None:
            summary["trigger_price"] = _decimal_text(trigger_price)
        references = [client_order_id] if venue_order_id is None else [client_order_id, venue_order_id]
        self._offer(
            self._factory.create(
                normalized_kind="protection" if leg in {"stop", "take_profit"} else "order",
                **correlation,
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=self._timestamp_ns(),
                native_identity_references=references,
                summary=summary,
                event_identity=f"{status}:{client_order_id}",
            )
        )

    def fill(self, *, correlation: dict[str, str], leg: OrderLeg, event: Any) -> None:
        """One venue fill, with the commission the venue charged for it and the currency it charged in."""

        references = [event.client_order_id.value]
        for name in ("venue_order_id", "trade_id", "position_id"):
            value = getattr(event, name, None)
            if value is not None:
                references.append(value.value)
        summary: dict[str, str | int | bool] = {
            "leg": leg,
            "last_quantity": _decimal_text(event.last_qty),
            "last_price": _decimal_text(event.last_px),
        }
        commission = getattr(event, "commission", None)
        if commission is not None:
            summary["commission"] = _decimal_text(commission)
            summary["commission_currency"] = commission.currency.code
        occurred_at_ns = int(event.ts_event)
        self._offer(
            self._factory.create(
                normalized_kind="fill",
                **correlation,
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=self._timestamp_ns(),
                native_identity_references=references,
                summary=summary,
                event_identity=f"fill:{getattr(event, 'trade_id', event.client_order_id)}",
            )
        )

    def position(
        self,
        *,
        correlation: dict[str, str],
        position_id: str,
        status: Literal["opened", "closed"],
        occurred_at_ns: int,
        quantity: Any,
        average_entry_price: Any,
        exit_price: Any = None,
        exit_reason: str | None = None,
    ) -> None:
        summary: dict[str, str | int | bool] = {
            "status": status,
            "quantity": _decimal_text(abs(decimal_value(quantity))),
            "avg_entry_price": _decimal_text(average_entry_price),
        }
        if exit_price is not None:
            summary["exit_price"] = _decimal_text(exit_price)
        if exit_reason is not None:
            summary["exit_reason"] = exit_reason
        self._offer(
            self._factory.create(
                normalized_kind="position",
                **correlation,
                occurred_at_ns=occurred_at_ns,
                observed_at_ns=self._timestamp_ns(),
                native_identity_references=(position_id,),
                summary=summary,
                event_identity=f"{status}:{position_id}:{occurred_at_ns}",
            )
        )

    def exposure(self, *, unexpected: tuple[str, ...], observed_at_ns: int) -> None:
        """Exposure no plan claims appeared, changed or cleared; new entries are blocked while it lasts."""

        summary: dict[str, str | int | bool] = {
            "risk_fact": "unexpected_exposure",
            "count": len(unexpected),
            "exposure": bounded_text(",".join(unexpected)),
        }
        self._offer(
            self._factory.create(
                normalized_kind="risk",
                occurred_at_ns=observed_at_ns,
                observed_at_ns=observed_at_ns,
                summary=summary,
                event_identity=f"unexpected_exposure:{observed_at_ns}",
            )
        )


def spread_detail(spread: Decimal | None) -> dict[str, str]:
    return {} if spread is None else {"spread_bps": format(spread.quantize(Decimal("0.01")), "f")}


__all__ = ["RuntimeObservations", "bounded_text", "spread_detail"]
