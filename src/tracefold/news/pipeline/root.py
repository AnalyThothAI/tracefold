"""Workers-process composition of the News pipeline stages."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..price_loops import EventReactionLoop, QuoteSnapshotLoop
from .admission import DeduperConsumer
from .delivery import DelivererConsumer
from .maintenance import InstrumentSnapshotLoop, JanitorLoop
from .receiver import OpenNewsReceiver
from .recovery import RecoveryRunner
from .triage import TriageConsumer


@dataclass
class NewsPipeline:
    """All consumers wired for one Workers process."""

    receiver: OpenNewsReceiver | None
    recovery: RecoveryRunner | None
    deduper: DeduperConsumer
    triage: TriageConsumer
    deliverer: DelivererConsumer
    janitor: JanitorLoop
    instruments: InstrumentSnapshotLoop | None = None
    # #88: two cold Price Review loops. They are not consumers — no queue, no delivery, no hot-path lane —
    # and every one of them may be absent without the pipeline changing shape.
    quotes: QuoteSnapshotLoop | None = None
    reactions: EventReactionLoop | None = None
    tasks: list[tuple[str, Callable[..., Any]]] = field(default_factory=list)

    async def register_runtime_manifest(self) -> None:
        await self.triage.register_runtime_manifest()

    def runners(self) -> list[tuple[str, Callable[[asyncio.Event], Any]]]:
        out: list[tuple[str, Callable[[asyncio.Event], Any]]] = []
        if self.receiver is not None:
            out.append(("news-receiver", lambda stop: self.receiver.run(stop_event=stop)))  # type: ignore[union-attr]
        if self.recovery is not None:
            out.append(("news-recovery", lambda stop: self.recovery.run(stop_event=stop)))  # type: ignore[union-attr]
        out.extend(
            [
                ("news-deduper", lambda stop: self.deduper.run(stop_event=stop)),
                ("news-triage", lambda stop: self.triage.run(stop_event=stop)),
                ("news-deliverer", lambda stop: self.deliverer.run(stop_event=stop)),
                ("news-janitor", lambda stop: self.janitor.run(stop_event=stop)),
            ]
        )
        if self.instruments is not None:
            out.append(("news-instruments", lambda stop: self.instruments.run(stop_event=stop)))  # type: ignore[union-attr]
        if self.quotes is not None:
            out.append(("news-quotes", lambda stop: self.quotes.run(stop_event=stop)))  # type: ignore[union-attr]
        if self.reactions is not None:
            out.append(("news-reactions", lambda stop: self.reactions.run(stop_event=stop)))  # type: ignore[union-attr]
        return out

    async def close(self) -> None:
        await self.deliverer.close()
