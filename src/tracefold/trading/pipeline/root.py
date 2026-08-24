"""Composition root for exactly two Trading runners in the existing Workers process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..decision.program import TradingDecisionProgram
from ..execution.paper import PaperAdapter
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
    """Exactly two runners in the existing Workers root. No queue, no second process."""

    candidate: CandidateRunner
    reconcile: ReconcileRunner

    def runners(self) -> list[tuple[str, Callable[[asyncio.Event], Awaitable[None]]]]:
        candidate, reconcile = self.candidate, self.reconcile
        return [
            ("trading-candidate", lambda stop: candidate.run(stop_event=stop)),
            ("trading-reconcile", lambda stop: reconcile.run(stop_event=stop)),
        ]

    async def close(self) -> None:
        return None


def build_pipeline(
    *,
    db: TradingDatabasePort,
    config: TradingConfig,
    bars: BarFetcherFactory,
    candidate_projection: CandidateProjectionReader,
    instrument_projection: InstrumentProjectionReader,
    program: TradingDecisionProgram | None = None,
    adapter: Any | None = None,
) -> TradingPipeline:
    """Compose the two runners. A live mode without a real adapter refuses to start."""

    if adapter is None:
        if config.mode != "paper":
            raise ValueError("trading_live_mode_requires_execution_adapter")
        adapter = PaperAdapter()
    return TradingPipeline(
        candidate=CandidateRunner(
            db=db,
            config=config,
            bars=bars,
            adapter=adapter,
            candidate_projection=candidate_projection,
            instrument_projection=instrument_projection,
            program=program,
        ),
        reconcile=ReconcileRunner(db=db, config=config, bars=bars, adapter=adapter),
    )


__all__ = ["TradingConfig", "TradingDatabasePort", "TradingPipeline", "build_pipeline"]
