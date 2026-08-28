"""Workers-process composition of the News pipeline stages."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..market_review.loops import EventReactionLoop, QuoteSnapshotLoop
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

    async def register_runtime_manifest(self) -> None:
        await self.triage.register_runtime_manifest()

    def runners(self) -> list[tuple[str, Callable[[asyncio.Event], Awaitable[None]]]]:
        """Ordered task declarations. The optional stages are bound to a local so the absent ones are
        absent by construction rather than by a suppressed `union-attr`."""

        out: list[tuple[str, Callable[[asyncio.Event], Awaitable[None]]]] = []
        receiver, recovery = self.receiver, self.recovery
        if receiver is not None:
            out.append(("news-receiver", lambda stop: receiver.run(stop_event=stop)))
        if recovery is not None:
            out.append(("news-recovery", lambda stop: recovery.run(stop_event=stop)))
        deduper, triage, deliverer, janitor = self.deduper, self.triage, self.deliverer, self.janitor
        out.extend(
            [
                ("news-deduper", lambda stop: deduper.run(stop_event=stop)),
                ("news-triage", lambda stop: triage.run(stop_event=stop)),
                ("news-deliverer", lambda stop: deliverer.run(stop_event=stop)),
                ("news-janitor", lambda stop: janitor.run(stop_event=stop)),
            ]
        )
        instruments, quotes, reactions = self.instruments, self.quotes, self.reactions
        if instruments is not None:
            out.append(("news-instruments", lambda stop: instruments.run(stop_event=stop)))
        if quotes is not None:
            out.append(("news-quotes", lambda stop: quotes.run(stop_event=stop)))
        if reactions is not None:
            out.append(("news-reactions", lambda stop: reactions.run(stop_event=stop)))
        return out

    async def drain(self) -> None:
        """Finish receipt-bound enrichment before shared native capabilities close."""

        await self.deliverer.drain()

    async def close(self) -> None:
        """Close the provider after the Workers root has drained native operations."""

        await self.deliverer.close_sender()
