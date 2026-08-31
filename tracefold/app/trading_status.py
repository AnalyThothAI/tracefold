"""One App projection for the CLI and HTTP Trading runtime status surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tracefold.trading import (
    CapitalRuntimeV1,
    DecisionRuntimeV1,
    NautilusRuntimePlanV1,
    VenueBindingRuntimeV1,
    nautilus_runtime_plan,
)
from tracefold.trading.storage.root import TradingRepository

TRADING_STATUS_WINDOW_MS = 24 * 3_600_000


@dataclass(frozen=True, slots=True)
class TradingRuntimeStatusSnapshot:
    decision: DecisionRuntimeV1
    capital: CapitalRuntimeV1
    bindings: tuple[VenueBindingRuntimeV1, ...]
    summary: dict[str, Any]
    nautilus: NautilusRuntimePlanV1


def read_trading_runtime_status(
    trading: TradingRepository,
    *,
    now_ms: int,
) -> TradingRuntimeStatusSnapshot:
    """Read the durable inputs once and derive the one public Nautilus decision."""

    decision = trading.decision_runtime() or DecisionRuntimeV1(
        state="FAULTED",
        heartbeat_at_ms=None,
        reason="decision_runtime_missing",
        updated_at_ms=now_ms,
    )
    capital = trading.capital_runtime() or CapitalRuntimeV1(
        control="PAUSED",
        blacklist_revision=0,
        arm_epoch=1,
        updated_at_ms=now_ms,
    )
    bindings = tuple(trading.binding_runtime_rows(now_ms=now_ms))
    summary = trading.runtime_summary(
        since_ms=now_ms - TRADING_STATUS_WINDOW_MS,
        now_ms=now_ms,
    )
    active_values = trading.active_intent_snapshot_values()
    active_bindings = () if active_values is None else (active_values[0]["binding"],)
    return TradingRuntimeStatusSnapshot(
        decision=decision,
        capital=capital,
        bindings=bindings,
        summary=summary,
        nautilus=nautilus_runtime_plan(
            capital=capital,
            bindings=bindings,
            active_intents=int(summary["active_intents"]),
            active_intent_bindings=active_bindings,
        ),
    )


__all__ = [
    "TRADING_STATUS_WINDOW_MS",
    "TradingRuntimeStatusSnapshot",
    "read_trading_runtime_status",
]
