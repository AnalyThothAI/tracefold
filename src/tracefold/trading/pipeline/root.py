"""Composition root for Trading candidate and reconciliation capabilities in Workers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..contracts import ExecutionAdapter, LiveExecutionAdapter
from ..decision.program import TradingDecisionProgram
from ..execution.paper import PaperAdapter
from ..telemetry import TradingExternalDataTelemetryPort
from .candidate import CandidateRunner
from .reconcile import ReconcileRunner
from .runtime import (
    BarFetcherFactory,
    CandidateProjectionReader,
    InstrumentProjectionReader,
    TradingConfig,
    TradingDatabasePort,
)


@dataclass(slots=True)
class TradingPipeline:
    """Trading capabilities hosted by the existing Workers root."""

    candidate: CandidateRunner
    reconcile: ReconcileRunner
    adapter: ExecutionAdapter

    def runners(self) -> list[tuple[str, Callable[[asyncio.Event], Awaitable[None]]]]:
        candidate, reconcile = self.candidate, self.reconcile
        return [
            ("trading-candidate", lambda stop: candidate.run(stop_event=stop)),
            ("trading-reconcile", lambda stop: reconcile.run(stop_event=stop)),
        ]

    async def close(self) -> None:
        await self.adapter.aclose()


def build_pipeline(
    *,
    db: TradingDatabasePort,
    config: TradingConfig,
    bars: BarFetcherFactory,
    candidate_projection: CandidateProjectionReader,
    instrument_projection: InstrumentProjectionReader,
    program: TradingDecisionProgram | None = None,
    adapter: ExecutionAdapter | None = None,
    telemetry: TradingExternalDataTelemetryPort | None = None,
) -> TradingPipeline:
    """Compose Trading capabilities. A live mode without a real adapter refuses to start."""

    resolved_adapter: ExecutionAdapter
    if config.mode == "live_bounded":
        raise ValueError("trading_live_bounded_disabled")
    if config.mode == "live_reviewed" and config.order.take_profit_bps != 0:
        raise ValueError("trading_live_reviewed_take_profit_disabled")
    if config.mode != "paper":
        if adapter is None or not isinstance(adapter, LiveExecutionAdapter):
            raise ValueError("trading_live_mode_requires_execution_adapter")
        resolved_adapter = adapter
    elif adapter is None:
        resolved_adapter = PaperAdapter()
    else:
        resolved_adapter = adapter
    return TradingPipeline(
        candidate=CandidateRunner(
            db=db,
            config=config,
            bars=bars,
            adapter=resolved_adapter,
            candidate_projection=candidate_projection,
            instrument_projection=instrument_projection,
            program=program,
            telemetry=telemetry,
        ),
        reconcile=ReconcileRunner(db=db, config=config, bars=bars, adapter=resolved_adapter, telemetry=telemetry),
        adapter=resolved_adapter,
    )


__all__ = ["TradingConfig", "TradingDatabasePort", "TradingPipeline", "build_pipeline"]
