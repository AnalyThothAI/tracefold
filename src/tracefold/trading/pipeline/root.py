"""Composition root for the Tracefold decision-to-Intent worker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..decision.program import TradingDecisionProgram
from ..telemetry import TradingExternalDataTelemetryPort
from .candidate import CandidateRunner
from .runtime import (
    BarFetcherFactory,
    CandidateProjectionReader,
    InstrumentProjectionReader,
    TradingConfig,
    TradingDatabasePort,
)


@dataclass(slots=True)
class TradingPipeline:
    """The one Trading capability hosted by Workers."""

    candidate: CandidateRunner

    def runners(self) -> list[tuple[str, Callable[[asyncio.Event], Awaitable[None]]]]:
        candidate = self.candidate
        return [("trading-candidate", lambda stop: candidate.run(stop_event=stop))]

    async def close(self) -> None:
        return None


def build_pipeline(
    *,
    db: TradingDatabasePort,
    config: TradingConfig,
    bars: BarFetcherFactory,
    candidate_projection: CandidateProjectionReader,
    instrument_projection: InstrumentProjectionReader,
    news_generation: str,
    program: TradingDecisionProgram | None = None,
    telemetry: TradingExternalDataTelemetryPort | None = None,
) -> TradingPipeline:
    """Compose the Evidence -> Case -> Intent path. Execution belongs only to Nautilus."""

    return TradingPipeline(
        candidate=CandidateRunner(
            db=db,
            config=config,
            bars=bars,
            candidate_projection=candidate_projection,
            instrument_projection=instrument_projection,
            news_generation=news_generation,
            program=program,
            telemetry=telemetry,
        ),
    )


__all__ = ["TradingConfig", "TradingDatabasePort", "TradingPipeline", "build_pipeline"]
