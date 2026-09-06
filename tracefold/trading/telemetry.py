"""Trading-owned observability contract for external-data worker boundaries."""

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Literal, Protocol

TradingWorkSemantics = Literal["derived_work", "durable_event", "latest_state", "signal_truth"]
# One runner measures a provider boundary: the Signal lane's source-native bar read.
TradingExternalDataName = Literal["trading_signal_lane"]
# The two provider families `sources.SOURCE_VENUES` resolves a source venue to, and nothing else: a
# third value would be a source venue no rule in this package can name.
TradingExternalDataSource = Literal["binance", "hyperliquid"]
# One runner, one turn, two answers: the lane's `advance()` either completed or raised. There is no
# path that reports a fraction of a turn, so `partial` was a third member nothing could ever emit and
# every reader of this metric had to allow for (#589 PR-2).
TradingExternalDataOutcome = Literal["error", "success"]
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


__all__ = [
    "TradingExternalDataName",
    "TradingExternalDataOutcome",
    "TradingExternalDataSource",
    "TradingExternalDataTelemetryPort",
    "TradingWorkSemantics",
    "observe_provider_call",
]
