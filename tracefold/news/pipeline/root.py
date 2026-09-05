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
    # Editorial is one capability among several: a Program that cannot be assembled or registered
    # leaves no Triage consumer, and reception, admission and retention run on without it (#553 PR-3).
    triage: TriageConsumer | None
    deliverer: DelivererConsumer
    janitor: JanitorLoop
    instruments: InstrumentSnapshotLoop | None = None
    # #88/#304: two bounded Price Review loops. They are not consumers — no queue or delivery —
    # and every one of them may be absent without the pipeline changing shape.
    quotes: QuoteSnapshotLoop | None = None
    reactions: EventReactionLoop | None = None

    @property
    def runtime_manifest_sha(self) -> str | None:
        return None if self.triage is None else self.triage.runtime_manifest_sha

    async def register_runtime_manifest(self) -> None:
        if self.triage is not None:
            await self.triage.register_runtime_manifest()

    def disable_editorial(self) -> None:
        """Drop the Triage consumer after its Program failed to assemble or register."""

        self.triage = None

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
        out.append(("news-deduper", lambda stop: deduper.run(stop_event=stop)))
        if triage is not None:
            out.append(("news-triage", lambda stop: triage.run(stop_event=stop)))
        out.extend(
            [
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
