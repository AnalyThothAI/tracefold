"""Trading-owned observability contract for external-data worker boundaries."""

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Literal, Protocol

TradingWorkSemantics = Literal["derived_work", "durable_event", "latest_state", "signal_truth"]
TradingExternalDataName = Literal["trading_signal_lane", "trading_reconcile"]
# One live provider. Hyperliquid stays in the vocabulary because the research replay measures it.
TradingExternalDataSource = Literal["binance", "hyperliquid", "other"]
TradingExternalDataOutcome = Literal["error", "partial", "success"]
TradingExternalDataProviderOutcome = Literal["error", "success"]


class TradingExternalDataTelemetryPort(Protocol):
    """Only the measurements Trading runners need from their process host."""

    def record_external_data_turn(
        self,
        name: TradingExternalDataName,
        outcome: TradingExternalDataOutcome,
        seconds: float,
        *,
        target_count: int | None = None,
        source_count: int | None = None,
        timestamp: float | None = None,
    ) -> None: ...

    def record_external_data_provider_call(
        self,
        name: TradingExternalDataName,
        source: TradingExternalDataSource,
        outcome: TradingExternalDataProviderOutcome,
        seconds: float,
        *,
        byte_count: int | None = None,
    ) -> None: ...


async def observe_provider_call[T](
    telemetry: TradingExternalDataTelemetryPort | None,
    *,
    name: TradingExternalDataName,
    source: TradingExternalDataSource,
    call: Awaitable[T],
) -> T:
    """Measure one already-selected Trading provider/model boundary."""

    started = time.perf_counter()
    try:
        result = await call
    except BaseException:
        if telemetry is not None:
            telemetry.record_external_data_provider_call(
                name,
                source,
                "error",
                time.perf_counter() - started,
            )
        raise
    if telemetry is not None:
        telemetry.record_external_data_provider_call(
            name,
            source,
            "success",
            time.perf_counter() - started,
        )
    return result


def external_data_source(exchange_id: str) -> TradingExternalDataSource:
    if exchange_id == "binance":
        return "binance"
    if exchange_id == "hyperliquid":
        return "hyperliquid"
    return "other"


__all__ = [
    "TradingExternalDataName",
    "TradingExternalDataOutcome",
    "TradingExternalDataSource",
    "TradingExternalDataTelemetryPort",
    "TradingWorkSemantics",
    "external_data_source",
    "observe_provider_call",
]
