"""The semantic stage: one News Agent turn per wanted evidence revision (#706).

Admission commits semantic work beside every new evidence snapshot of an admitted Event and wakes
this consumer on the existing `news.triage` queue. A turn claims the work lease, runs the News Agent
(frozen input, extraction, judgments, adoption) and settles the work: done, deferred with backoff,
or failed with a code. Nothing here decides news value; a failure is never "no news".
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Literal, Protocol

from ..bus import Q_TRIAGE, BusMessage, DeferError, PermanentError, TransientError, now_ms
from ..storage.event_updates import SEMANTIC_ATTEMPTS_MAX, EventUpdateConflict, SemanticLease
from ..telemetry import NewsWorkSemantics
from ..updates.judgment import ConfigurationFault, ContractFault, ProviderUnavailable
from .runtime import NewsDatabasePort

log = logging.getLogger("tracefold.news")

# Longer than one stage (20 s) plus adoption: an expired lease means the turn is gone.
SEMANTIC_LEASE_MS: Final = 60_000
# Revisions that arrive while a turn holds the lease are processed by the same consumer: their own
# wake was dropped on the held lease. Bounded so one hot Event cannot monopolize a consumer slot.
TURNS_PER_WAKE: Final = 3
# The durable incident a sustained provider outage opens; the Console already reads this cause.
PROVIDER_OUTAGE_CAUSE: Final = "triage_circuit_open"
_CODE = re.compile(r"^[a-z0-9_:.]{1,160}$")

TurnOutcome = Literal["adopted", "unchanged", "newer_head", "deferred", "failed"]


class SemanticAgent(Protocol):
    async def process(self, event_id: str, *, final_attempt: bool = True) -> str: ...


class SemanticWorkStore(Protocol):
    async def claim_semantic_work(self, event_id: str, *, lease_ms: int) -> SemanticLease | None: ...

    async def defer_semantic_event(self, lease: SemanticLease, *, reason: str, retry_after_ms: int = 0) -> None: ...

    async def fail_semantic_event(self, lease: SemanticLease, *, error_code: str) -> None: ...


@dataclass(slots=True)
class ProviderBreaker:
    """Consecutive provider failures pause claiming, so an outage does not spend every attempt.

    While open, pending work stays durable and unclaimed; the repair turn wakes it again once the
    breaker has closed. One answered turn closes it.
    """

    threshold: int
    open_seconds: float
    failures: int = 0
    open_until_ms: int = 0

    def is_open(self, at_ms: int) -> bool:
        return at_ms < self.open_until_ms

    def record_failure(self, at_ms: int) -> bool:
        """Count one failure; True when this failure opens the breaker."""

        self.failures += 1
        if self.failures < self.threshold:
            return False
        self.failures = 0
        self.open_until_ms = at_ms + int(self.open_seconds * 1000)
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.open_until_ms = 0


def error_code(exc: BaseException, *, default: str) -> str:
    """A bounded, code-shaped reason; free text from a provider or a library is never stored."""

    text = str(exc)
    return text if _CODE.fullmatch(text) else f"{default}:{type(exc).__name__}"


class SemanticWorker:
    """The `news.triage` consumer, reborn: it settles semantic work, never a verdict."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        store: SemanticWorkStore,
        agent: SemanticAgent,
        concurrency: int,
        circuit_failures: int,
        circuit_open_seconds: float,
        program_identity: str,
        lease_ms: int = SEMANTIC_LEASE_MS,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.bus = bus
        self.db = db
        self.store = store
        self.agent = agent
        self.concurrency = int(concurrency)
        self.breaker = ProviderBreaker(threshold=int(circuit_failures), open_seconds=float(circuit_open_seconds))
        self.program_identity = program_identity
        self.lease_ms = int(lease_ms)
        self.clock = clock
        self._incident_open = False

    async def run(self, *, stop_event: asyncio.Event) -> None:
        # A fresh process starts with a closed breaker, so an incident a previous process left open is
        # over. Consuming without knowing that would make the durable incident permanently wrong; the
        # failure fails this capability and the supervised restart tries again.
        await self.db.tx(
            "news_semantic_incident_reconcile",
            lambda repos: repos.news.close_open_incidents(cause_classes=[PROVIDER_OUTAGE_CAUSE], now_ms=self.clock()),
        )
        await self.bus.consume(Q_TRIAGE, self.handle, prefetch=self.concurrency, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        if not event_id:
            raise PermanentError("news_event_id_missing")
        for _ in range(TURNS_PER_WAKE):
            if self.breaker.is_open(self.clock()):
                return
            lease = await self.store.claim_semantic_work(event_id, lease_ms=self.lease_ms)
            if lease is None:
                # Nothing due: done, not yet due after a deferral, exhausted, or another turn holds it.
                return
            if await self.turn(lease) in {"deferred", "failed"}:
                return

    async def turn(self, lease: SemanticLease) -> TurnOutcome:
        """One claimed attempt. The last attempt of a revision adopts unresolved comparisons as such."""

        final_attempt = lease.attempts >= SEMANTIC_ATTEMPTS_MAX
        try:
            outcome = await self.agent.process(lease.event_id, final_attempt=final_attempt)
        except (asyncio.CancelledError, TransientError, DeferError):
            # Cancellation or a PostgreSQL lane failure says nothing about the provider or the content.
            # The lease expires on its own and the repair turn wakes the still-pending work.
            raise
        except (ProviderUnavailable, TimeoutError) as exc:
            await self.store.defer_semantic_event(lease, reason=error_code(exc, default="news_provider_unavailable"))
            await self._provider_failed()
            return "deferred"
        except (ContractFault, ConfigurationFault, EventUpdateConflict, LookupError, ValueError) as exc:
            # The response or the stored input cannot satisfy the contract: visible, not retried until
            # new evidence, and never recorded as a judgment that the Event has no news value.
            code = error_code(exc, default="news_semantic_contract_fault")
            log.warning("news semantic turn failed event_id=%s code=%s", lease.event_id, code)
            await self.store.fail_semantic_event(lease, error_code=code)
            return "failed"
        except Exception as exc:
            # An unclassified provider or program error: bounded retry under the same attempt budget.
            log.exception("news semantic turn raised event_id=%s", lease.event_id)
            await self.store.defer_semantic_event(lease, reason=f"news_semantic_unexpected:{type(exc).__name__}")
            return "deferred"
        await self._provider_answered()
        if outcome not in {"adopted", "unchanged", "newer_head", "deferred"}:
            raise RuntimeError(f"news_semantic_outcome_unknown:{outcome}")
        return outcome  # type: ignore[return-value]

    async def _provider_failed(self) -> None:
        stamp = self.clock()
        if not self.breaker.record_failure(stamp):
            return
        log.warning("news semantic provider breaker open for %.0f s", self.breaker.open_seconds)
        await self.db.tx(
            "news_semantic_incident_open",
            lambda repos: repos.news.open_incident(cause_class=PROVIDER_OUTAGE_CAUSE, now_ms=stamp),
        )
        self._incident_open = True

    async def _provider_answered(self) -> None:
        self.breaker.record_success()
        if not self._incident_open:
            return
        stamp = self.clock()
        await self.db.tx(
            "news_semantic_incident_close",
            lambda repos: repos.news.close_open_incidents(cause_classes=[PROVIDER_OUTAGE_CAUSE], now_ms=stamp),
        )
        self._incident_open = False


__all__ = [
    "PROVIDER_OUTAGE_CAUSE",
    "SEMANTIC_LEASE_MS",
    "ProviderBreaker",
    "SemanticWorker",
    "error_code",
]
