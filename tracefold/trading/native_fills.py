"""Native execution economics, known cost and verified Plan association are separate facts.

A venue order belongs to the immutable fact, not its economic identity. Neither
fees, business purpose, strategy versions nor observation time can create another
trade. Unknown costs and associations have no row; later evidence appends them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .execution_contracts import ExecutionObservationV1
from .trade_plan import PlanOrderBinding

NATIVE_EXECUTION_KINDS = frozenset({"native_fill", "native_fill_cost", "native_fill_binding", "native_order_result"})


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("native_fill_decimal_invalid")
    text = format(value, "f")
    return "0" if value == 0 else text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True, slots=True)
class NativeFill:
    account_slot: str
    environment: Literal["LIVE", "DEMO", "TESTNET"]
    instrument: str
    trade_id: str
    order_id: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    price: Decimal
    occurred_at_ns: int

    def __post_init__(self) -> None:
        if (
            not self.account_slot
            or self.environment not in ("LIVE", "DEMO", "TESTNET")
            or not self.instrument.isalnum()
            or self.instrument != self.instrument.upper()
            or not self.trade_id.isdecimal()
            or str(int(self.trade_id)) != self.trade_id
            or not self.order_id.isdecimal()
            or int(self.order_id) <= 0
            or str(int(self.order_id)) != self.order_id
            or self.side not in ("BUY", "SELL")
            or not self.quantity.is_finite()
            or self.quantity <= 0
            or not self.price.is_finite()
            or self.price <= 0
            or self.occurred_at_ns <= 0
        ):
            raise ValueError("native_fill_identity_or_economics_invalid")

    def observation(
        self,
        *,
        execution_strategy: str,
        observed_at_ns: int,
        commission: Decimal | None = None,
        commission_currency: str | None = None,
        binding: PlanOrderBinding | None = None,
    ) -> tuple[ExecutionObservationV1, ...]:
        """Build one economic row and optional cost and exact-order association.

        Callers must already have verified the binding against the native order
        chain. A matching symbol/price/quantity alone is never such proof.
        """
        identity = {
            "venue_environment": self.environment,
            "native_instrument": self.instrument,
            "native_trade_id": self.trade_id,
        }

        def row(
            kind: str, summary: dict[str, str], *, correlation: dict[str, str] | None = None
        ) -> ExecutionObservationV1:
            key = {"account_slot": self.account_slot, "kind": kind, **identity}
            event_id = hashlib.sha256(json.dumps(key, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return ExecutionObservationV1.model_validate(
                {
                    "event_id": event_id,
                    "account_slot": self.account_slot,
                    "execution_strategy": execution_strategy,
                    "normalized_kind": kind,
                    "occurred_at_ns": self.occurred_at_ns,
                    "observed_at_ns": max(observed_at_ns, self.occurred_at_ns),
                    "native_identity_references": (self.order_id, self.trade_id),
                    "summary": {**identity, "venue_order_id": self.order_id, **summary},
                    **(correlation or {}),
                }
            )

        rows = [
            row(
                "native_fill",
                {
                    "side": self.side,
                    "last_quantity": decimal_text(self.quantity),
                    "last_price": decimal_text(self.price),
                },
            )
        ]
        if (commission is None) != (commission_currency is None):
            raise ValueError("native_fill_cost_incomplete")
        if commission is not None:
            if not commission_currency or not commission_currency.isalnum():
                raise ValueError("native_fill_cost_currency_invalid")
            rows.append(
                row(
                    "native_fill_cost",
                    {
                        "commission": decimal_text(commission),
                        "commission_currency": commission_currency,
                    },
                )
            )
        if binding is not None:
            if binding.account_slot != self.account_slot or binding.instrument_id != f"{self.instrument}-PERP.BINANCE":
                raise ValueError("native_fill_binding_scope_conflict")
            summary = {"client_order_id": binding.client_order_id, "leg": binding.leg}
            if binding.exit_reason is not None:
                summary["exit_reason"] = binding.exit_reason
            rows.append(
                row(
                    "native_fill_binding",
                    summary,
                    correlation={"signal_id" if binding.source == "signal" else "command_id": binding.entry_id},
                )
            )
        return tuple(rows)
