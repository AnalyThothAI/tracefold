"""News V3 consumers: Receiver, Recovery, Deduper, Triage, Deliverer, Janitor.

Each consumer is one asyncio task; the broker is the only coordination plane; PostgreSQL holds
facts/decisions/audit; every write is idempotent by key. Consumers coordinate only through the
broker and database keys, so any of them can be scaled out without code changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tracefold.news import (
    TOLD_MAX,
    TOLD_WINDOW_MS,
    ProgramTrace,
    ProgramUsage,
    SemanticJudge,
    SemanticJudgeError,
    TriageContext,
)
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from .bus import (
    Q_DELIVER,
    Q_RAW,
    Q_TRIAGE,
    RK_EVENT,
    RK_RAW_LIVE,
    RK_RAW_RECOVERY,
    RK_VERDICT_PUSH,
    BusMessage,
    DeferError,
    PermanentError,
    TransientError,
    new_trace_id,
    now_ms,
)
from .canary import CanaryRuntimeArm
from .delivery import card_assets, render_first_card
from .events import admit_frame
from .models import (
    ADMITTED_ADMISSIONS,
    GATE_POLICY_VERSION,
    OUTBOX_MAX_AGE_MS,
    TRIAGE_POLICY_VERSION,
    TriageVerdict,
    json_ready,
)
from .oi_signals import DEFAULT_OI_POLICY, OiPolicy, evaluate_oi, parse_oi_signal, program_sha256
from .oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from .oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from .opennews import (
    OpenNewsExpectedError,
    OpenNewsHistoryError,
    enabled_strategy_ids,
    parse_opennews_message,
    parse_opennews_strategy_hits,
)
from .price_loops import EventReactionLoop, QuoteSnapshotLoop
from .storyline import final_storyline_key
from .triage_rules import (
    DEFAULT_POLICY,
    DecidePolicy,
    DecisionResult,
    GateFacts,
    decide,
    fallback_verdict,
    grounded_restatement,
    storyline_status,
)

log = logging.getLogger("tracefold.news")

_HISTORY_PAGE_SIZE = 100
_HISTORY_PAGE_CAP = 60
_RECOVERY_OVERLAP_MS = 30_000
_WS_RECONNECT_SECONDS = 3.0
_OUTBOX_MIN_AGE_MS = 15_000
_JANITOR_PERIOD_SECONDS = 60.0
_DAY_MS = 24 * 3600_000
# The Program ledger shows the reader's last TOLD_MAX cards; decide() measures a duplicate candidate against
# a wider, bounded sent ledger. Replaying the stored corpus, widening the comparison set from the 12-entry status
# bar caught 14 more duplicate pairs and 11 more facts the reader never received (#81). This is a memory bound,
# not a reader quota: a ledger filled with distinct facts never blocks the next distinct fact.
_SEEN_LEDGER_MAX = 128
# Instrument universe (#75): the Gate's cached copy, and how often the snapshot loop asks the venues.
_INSTRUMENT_CACHE_TTL_MS = 10 * 60_000
_INSTRUMENT_SNAPSHOT_PERIOD_SECONDS = 6 * 3600.0
_INSTRUMENT_RETRY_SECONDS = 15 * 60.0

_WS_CAUSE = {
    "opennews_network_connect": "network_connect",
    "opennews_authentication": "authentication",
    "opennews_provider_close": "provider_close",
    "opennews_protocol_error": "protocol_error",
    "opennews_idle_timeout": "idle_timeout",
}


def _cause_for(code: str | None) -> str:
    if not code:
        return "unknown"
    for prefix, cause in _WS_CAUSE.items():
        if code.startswith(prefix):
            return cause
    return "unknown"


class _Db:
    """Thin adapter over WorkerDatabase's News lane: run a sync repository function inside one session."""

    def __init__(self, db: Any, *, cold: bool = False) -> None:
        self._db = db
        self._lane = db.heavy_business() if cold else None

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos, repos.transaction():
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def read(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float = 3.0) -> Any:
        def _run() -> Any:
            with self._db.worker_session(name, timeout_seconds) as repos:
                return fn(repos)

        return await self._run(name, _run, timeout_seconds)

    async def _run(self, name: str, fn: Callable[[], Any], timeout_seconds: float) -> Any:
        try:
            if self._lane is not None:
                return await self._lane.run_business(name, fn, operation_timeout_seconds=timeout_seconds)
            return await self._db.run_news(name, fn, operation_timeout_seconds=timeout_seconds)
        except ResourceAdmissionTimeout as exc:
            raise DeferError(f"db_admission_timeout:{name}") from exc
        except ResourceOperationOverrun as exc:
            raise TransientError(f"db_overrun:{name}") from exc


# ---------------------------------------------------------------------------- Receiver
class OpenNewsReceiver:
    """WSS -> broker. Publishes each accepted frame with confirms; overflow/unavailability become incidents."""

    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        ws_client: Any | None,
        history_client: Any | None,
        recovery: RecoveryRunner | None,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.ws_client = ws_client
        self.history_client = history_client
        self.recovery = recovery
        self._backpressure_open = False

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if self.ws_client is None:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            try:
                await self.ws_client.connect()
                await self._connected()
                while not stop_event.is_set():
                    message = await _receive_or_stop(self.ws_client, stop_event=stop_event)
                    if message is None:
                        break
                    event = parse_opennews_message(message)
                    if event is None:
                        continue
                    await self._publish_frame(message, strategy_id=str(event.provider_metadata["strategies"][0]["id"]))
            except asyncio.CancelledError:
                raise
            except OpenNewsExpectedError as exc:
                await self._disconnected(cause=_cause_for(exc.code), close_code=exc.status_code, error_code=exc.code)
            except Exception as exc:
                log.exception("news receiver failed")
                await self._disconnected(cause="unknown", close_code=None, error_code=type(exc).__name__)
            finally:
                with contextlib.suppress(Exception):
                    await self.ws_client.close()
            if not stop_event.is_set():
                await _sleep_or_stop(stop_event, _WS_RECONNECT_SECONDS)
        await self._disconnected(cause="planned_shutdown", close_code=None, error_code=None, planned=True)

    async def _publish_frame(self, message: Mapping[str, Any], *, strategy_id: str) -> None:
        params = message.get("params") if isinstance(message, Mapping) else None
        if not isinstance(params, Mapping):
            return
        stamp = now_ms()
        payload = {"params": dict(params), "strategy_id": strategy_id, "ingest_mode": "live", "observed_at_ms": stamp}
        msg = BusMessage(
            kind="raw",
            message_id=f"raw:{params.get('id')}",
            routing_key=RK_RAW_LIVE.format(strategy_id=strategy_id),
            payload=payload,
            trace_id=new_trace_id(),
            occurred_at_ms=stamp,
        )
        try:
            await self.bus.publish(msg)
        except Exception as exc:  # BrokerBackpressure / BrokerUnavailable
            cause = "broker_backpressure" if type(exc).__name__ == "BrokerBackpressure" else "broker_unavailable"
            if not self._backpressure_open:
                self._backpressure_open = True
                with contextlib.suppress(TransientError, DeferError):
                    await self.db.tx(
                        "news_ingest_backpressure",
                        lambda repos: (
                            repos.news.open_incident(cause_class=cause, now_ms=stamp),
                            repos.news.update_ingest_state(now_ms=stamp, last_error_code=cause),
                        ),
                    )
            return
        if self._backpressure_open:
            self._backpressure_open = False
            with contextlib.suppress(TransientError, DeferError):
                await self.db.tx(
                    "news_ingest_backpressure_close",
                    lambda repos: repos.news.close_open_incidents(
                        cause_classes=["broker_backpressure", "broker_unavailable"], now_ms=stamp
                    ),
                )
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_ingest_frame",
                lambda repos: repos.news.update_ingest_state(
                    now_ms=stamp, last_frame_at_ms=stamp, last_publish_at_ms=stamp, clear_error=True
                ),
                timeout_seconds=1.0,
            )

    async def _connected(self) -> None:
        stamp = now_ms()

        def _fn(repos: Any) -> None:
            repos.news.close_open_incidents(
                cause_classes=[*_WS_CAUSE.values(), "unknown", "process_outage", "planned_shutdown"],
                now_ms=stamp,
            )
            repos.news.update_ingest_state(now_ms=stamp, connected=True, clear_error=True)

        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx("news_ingest_connected", _fn)
        if self.recovery is not None:
            self.recovery.request()

    async def _disconnected(
        self, *, cause: str, close_code: int | None, error_code: str | None, planned: bool = False
    ) -> None:
        stamp = now_ms()

        def _fn(repos: Any) -> None:
            repos.news.open_incident(cause_class=cause, now_ms=stamp, planned=planned, close_code=close_code)
            repos.news.update_ingest_state(now_ms=stamp, connected=False, last_error_code=error_code)

        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx("news_ingest_disconnected", _fn)


# ---------------------------------------------------------------------------- Recovery
class RecoveryRunner:
    """Closed incidents -> official Strategy hits -> raw.recovery.* messages (never delivered)."""

    def __init__(self, *, bus: Any, db: Any, history_client: Any | None) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.history_client = history_client
        self._requested = asyncio.Event()

    def request(self) -> None:
        self._requested.set()

    async def run(self, *, stop_event: asyncio.Event) -> None:
        self._requested.set()  # startup pass
        while not stop_event.is_set():
            waiter = asyncio.create_task(self._requested.wait())
            stopper = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (waiter, stopper):
                    task.cancel()
                await asyncio.gather(waiter, stopper, return_exceptions=True)
            if stop_event.is_set():
                return
            self._requested.clear()
            try:
                await self._recover_pending()
            except (TransientError, DeferError):
                await _sleep_or_stop(stop_event, 5.0)
                self._requested.set()

    async def _provider_strategy_ids(self) -> tuple[str, ...]:
        """Which Strategies to pull history for: the provider's own enabled list, read fresh.

        Live ingestion needs no list at all — the socket pushes what the account enabled. Recovery does, only
        because the provider's hits endpoint is per-strategy. Reading it per pass rather than caching it at
        startup means a Strategy enabled mid-run is recovered too.

        A failed read raises rather than returning nothing, and that distinction is the whole point:
        `complete_recovery` is terminal, `pending_recovery_incidents` only ever selects `pending`, so settling
        an incident `unavailable` throws its outage window away for good. One 429 on this call would otherwise
        discard the entire backlog. `TransientError` leaves every incident pending for `run()` to retry; an
        empty tuple means the account really has no enabled Strategies and there is nothing to recover.
        """

        if self.history_client is None:
            return ()
        try:
            payload = await self.history_client.get_strategy_list(limit=100, page=1)
        except Exception as exc:
            raise TransientError(f"opennews_strategy_list_unavailable:{type(exc).__name__}") from exc
        return tuple(sorted(enabled_strategy_ids(payload)))

    async def _recover_pending(self) -> None:
        incidents = await self.db.read("news_recovery_pending", lambda repos: repos.news.pending_recovery_incidents())
        if not incidents:
            return
        strategy_ids = await self._provider_strategy_ids()
        for incident in incidents:
            incident_id = int(incident["incident_id"])
            stamp = now_ms()
            if not strategy_ids:

                def _unavailable(repos: Any, i: int = incident_id, s: int = stamp) -> None:
                    repos.news.complete_recovery(
                        incident_id=i,
                        status="unavailable",
                        recovered_count=0,
                        error_code="opennews_history_unavailable",
                        recovery_from_at_ms=None,
                        recovery_to_at_ms=None,
                        now_ms=s,
                    )

                await self.db.tx("news_recovery_unavailable", _unavailable)
                continue
            from_ms = max(
                0, int(incident.get("recovery_from_at_ms") or incident["opened_at_ms"]) - _RECOVERY_OVERLAP_MS
            )
            to_ms = int(incident.get("recovery_to_at_ms") or incident.get("closed_at_ms") or stamp)
            complete, count, error = True, 0, None
            try:
                for strategy_id in strategy_ids:
                    strategy_complete, strategy_count = await self._recover_strategy(
                        strategy_id, from_ms=from_ms, to_ms=to_ms
                    )
                    complete = complete and strategy_complete
                    count += strategy_count
            except OpenNewsHistoryError as exc:
                error = exc.code
            except Exception as exc:
                error = f"opennews_history_failed:{type(exc).__name__}"
            status = "unavailable" if error else ("recovered" if complete else "partial")

            def _complete(
                repos: Any,
                i: int = incident_id,
                s: str = status,
                c: int = count,
                e: str | None = error,
                f: int = from_ms,
                u: int = to_ms,
            ) -> None:
                repos.news.complete_recovery(
                    incident_id=i,
                    status=s,
                    recovered_count=c,
                    error_code=e or (None if s == "recovered" else "opennews_history_retention_partial"),
                    recovery_from_at_ms=f,
                    recovery_to_at_ms=u,
                    now_ms=now_ms(),
                )

            await self.db.tx("news_recovery_complete", _complete)

    async def _recover_strategy(self, strategy_id: str, *, from_ms: int, to_ms: int) -> tuple[bool, int]:
        client = self.history_client
        if client is None:
            raise OpenNewsHistoryError("opennews_history_unavailable")
        recovered = 0
        for page_number in range(1, _HISTORY_PAGE_CAP + 1):
            payload = await client.get_strategy_hits(
                strategy_id=strategy_id, limit=_HISTORY_PAGE_SIZE, page=page_number
            )
            page = parse_opennews_strategy_hits(payload)
            for event in page.events:
                published = event.entry.published_at_ms
                if published is None or not (from_ms <= int(published) < to_ms):
                    continue
                raw_params = _raw_params_from_history(payload, event.provider_record_id)
                if raw_params is None:
                    continue
                stamp = now_ms()
                await self.bus.publish(
                    BusMessage(
                        kind="raw",
                        message_id=f"raw:{event.provider_record_id}",
                        routing_key=RK_RAW_RECOVERY.format(strategy_id=strategy_id),
                        payload={
                            "params": raw_params,
                            "strategy_id": strategy_id,
                            "ingest_mode": "recovery",
                            "observed_at_ms": stamp,
                        },
                        trace_id=new_trace_id(),
                        occurred_at_ms=stamp,
                    )
                )
                recovered += 1
            oldest = min(
                (int(e.entry.published_at_ms) for e in page.events if e.entry.published_at_ms is not None), default=None
            )
            if oldest is not None and oldest <= from_ms:
                return True, recovered
            if not page.has_more:
                return page.total == 0 or (oldest is not None and oldest <= from_ms), recovered
        return False, recovered


def _raw_params_from_history(payload: Mapping[str, Any], provider_record_id: str) -> dict[str, Any] | None:
    for value in payload.get("data") or []:
        if isinstance(value, Mapping) and str(value.get("id")) == provider_record_id:
            return dict(value)
    return None


# ---------------------------------------------------------------------------- Deduper
async def publish_event(bus: Any, db: _Db, *, event_id: str, family: str, priority: str, trace_id: str) -> None:
    """Publish one candidate Event to Triage and mark it published (commit-then-publish outbox step)."""

    stamp = now_ms()
    await bus.publish(
        BusMessage(
            kind="event",
            message_id=f"event:{event_id}",
            routing_key=RK_EVENT.format(family=family, priority=priority),
            payload={"event_id": event_id},
            trace_id=trace_id,
            occurred_at_ms=stamp,
            priority=5 if priority == "high" else 0,
        )
    )
    with contextlib.suppress(TransientError, DeferError):
        await db.tx(
            "news_event_mark_published",
            lambda repos: repos.news.mark_event_published(event_id=event_id, now_ms=stamp),
            timeout_seconds=1.0,
        )


class DeduperConsumer:
    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        watchlist_symbols: frozenset[str],
        suppress_low_signal: bool = False,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.watchlist_symbols = watchlist_symbols
        self.suppress_low_signal = bool(suppress_low_signal)
        # #89: symbol -> instrument_class, which is how the Gate tells a stock headline from a coin headline. The
        # universe changes about once a day, so it is cached per consumer.
        self._classes: Mapping[str, str] | None = None
        self._classes_at_ms = 0

    def _current_instrument_classes(self, repos: Any, *, now: int) -> Mapping[str, str] | None:
        """Cached instrument classes for the Gate, refreshed at most once per `_INSTRUMENT_CACHE_TTL_MS`."""

        if self._classes is not None and now - self._classes_at_ms < _INSTRUMENT_CACHE_TTL_MS:
            return self._classes
        classes = repos.instruments.instrument_classes()
        self._classes_at_ms = now
        # An empty universe means no snapshot has landed: fall back to the prefix heuristic, not to "no assets".
        self._classes = classes or None
        return self._classes

    async def run(self, *, stop_event: asyncio.Event) -> None:
        await self.bus.consume(Q_RAW, self.handle, prefetch=1, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        params = message.payload.get("params")
        if not isinstance(params, Mapping):
            raise PermanentError("news_raw_params_missing")
        event = parse_opennews_message({"method": "strategy.triggered", "params": dict(params)})
        if event is None:
            return  # malformed frame: settle silently
        ingest_mode = "recovery" if str(message.payload.get("ingest_mode")) == "recovery" else "live"
        observed = int(message.payload.get("observed_at_ms") or message.occurred_at_ms or now_ms())
        stamp = now_ms()
        batch = await self.db.tx(
            "news_deduper_admit",
            lambda repos: admit_frame(
                repos,
                event=event,
                ingest_mode=ingest_mode,
                observed_at_ms=observed,
                trace_id=message.trace_id,
                watchlist_symbols=self.watchlist_symbols,
                now_ms=stamp,
                suppress_low_signal=self.suppress_low_signal,
                instrument_classes=self._current_instrument_classes(repos, now=stamp),
            ),
            timeout_seconds=5.0,
        )
        for result in batch.results:
            if result.event_created and result.admission in ADMITTED_ADMISSIONS:
                await publish_event(
                    self.bus,
                    self.db,
                    event_id=result.event_id,
                    family=result.family,
                    priority=result.gate.priority if result.gate else "normal",
                    trace_id=message.trace_id,
                )


# ---------------------------------------------------------------------------- Triage
@dataclass
class _Circuit:
    failures: int = 0
    open_until_ms: int = 0
    threshold: int = 3
    open_seconds: float = 60.0

    def is_open(self, at_ms: int) -> bool:
        return at_ms < self.open_until_ms

    def record_failure(self, at_ms: int) -> bool:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until_ms = at_ms + int(self.open_seconds * 1000)
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0


@dataclass(frozen=True, slots=True)
class _TriageSettle:
    """Everything the in-transaction decide-and-persist step needs (built after the model call)."""

    event_id: str
    evidence_version: int
    evidence_sha256: str
    focus_fact_id: str
    verdict: TriageVerdict
    facts: GateFacts
    final_key: str
    told: Sequence[Mapping[str, Any]]
    seen: Sequence[Mapping[str, Any]]
    told_seen: frozenset[str]
    degraded: bool
    error_code: str | None
    model_name: str | None
    program_version: str
    program_sha256: str
    policy: DecidePolicy
    trace: dict[str, Any]
    stamp: int
    allow_stale: bool


@dataclass(frozen=True, slots=True)
class _TriageOutcome:
    stale: bool
    final: str
    decision: DecisionResult | None
    stale_reason: str | None = None


def _open_circuit_incident(repos: Any, *, now_ms: int) -> Any:
    return repos.news.open_incident(cause_class="triage_circuit_open", now_ms=now_ms)


def _close_circuit_incidents(repos: Any, *, now_ms: int) -> Any:
    return repos.news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms)


def _told_trace(told: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The ledger exactly as the model saw it (plus event ids), so ``news why`` can name the restated card and
    CandidateEvaluator recordings can reproduce ``StorylineStatus.told_directions``."""

    return [
        {
            "i": int(t.get("i", i)),
            "event_id": str(t.get("event_id") or ""),
            "at_ms": int(t.get("at_ms") or 0),
            "m": int(t.get("m") or 0),
            "dir": str(t.get("dir") or ""),
            "headline_zh": str(t.get("headline_zh") or ""),
        }
        for i, t in enumerate(told)
    ]


def _told_from_context(context: TriageContext) -> list[dict[str, Any]]:
    """Return the exact ledger order/index visible to the Program in the shape used by policy and audit."""

    return [
        {
            "i": entry.i,
            "event_id": entry.event_id,
            "at_ms": entry.at_ms,
            "m": entry.magnitude,
            "dir": entry.direction,
            "headline_zh": entry.headline_zh,
        }
        for entry in context.told.entries
    ]


def _usage_from_partial_trace(program_trace: ProgramTrace | None, *, attempts: int) -> dict[str, Any]:
    """Recover the observable usage of a failed Program execution.

    ``SemanticJudgeError`` deliberately carries a partial trace rather than a
    second usage object.  Calls already made before the failure are nevertheless
    billable facts and must survive a stale-ledger re-ask.  Synthetic trace
    entries remain in ``call_count`` for audit, while only entries explicitly
    marked as physical provider calls contribute usage or cost.  With no such
    entry the observed physical cost is exactly zero.
    """

    calls = tuple(program_trace.calls) if program_trace is not None else ()
    physical_calls = tuple(call for call in calls if call.physical_provider_call)
    costs = [call.provider_cost_microusd for call in physical_calls]
    return {
        "wall_latency_ms": sum(call.latency_ms for call in physical_calls),
        "call_count": len(calls) if calls else max(0, int(attempts)),
        "physical_call_count": len(physical_calls),
        "input_tokens": sum(call.input_tokens for call in physical_calls),
        "output_tokens": sum(call.output_tokens for call in physical_calls),
        "cached_tokens": sum(call.cached_tokens for call in physical_calls),
        "total_tokens": sum(call.total_tokens for call in physical_calls),
        "provider_cost_microusd": (
            sum(int(cost) for cost in costs if cost is not None) if all(cost is not None for cost in costs) else None
        ),
    }


def _program_execution(
    *,
    execution_index: int,
    phase: str,
    status: str,
    context: TriageContext,
    program_trace: ProgramTrace | None,
    usage: ProgramUsage | Mapping[str, Any],
    answering_model: str | None = None,
    fallback_from: str | None = None,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one immutable-in-meaning online Program execution audit entry."""

    usage_payload = usage.model_dump(mode="json") if isinstance(usage, ProgramUsage) else dict(usage)
    trace_payload = program_trace.model_dump(mode="json") if program_trace is not None else None
    result: dict[str, Any] = {
        "execution_index": execution_index,
        "phase": phase,
        "status": status,
        "context_sha256": program_trace.context_sha256 if program_trace is not None else None,
        "context": context.model_dump(mode="json"),
        "trace": trace_payload,
        "usage": usage_payload,
        # ``_sync_program_audit`` assigns global call indices after appending
        # the execution.  That disambiguates two calls named
        # (event_semantics, attempt=1) in initial/re-ask runs.
        "recording_call_indices": list(range(len(program_trace.calls))) if program_trace is not None else [],
    }
    if answering_model is not None:
        result["answering_model"] = answering_model
    if fallback_from is not None:
        result["fallback_from"] = fallback_from
    if error is not None:
        result["error"] = dict(error)
    return result


def _sync_program_audit(
    trace: dict[str, Any],
    *,
    executions: Sequence[dict[str, Any]],
    selected_execution_index: int | None,
) -> None:
    """Project all executions plus the verdict-owning trace into verdict audit.

    The selected ``program_trace`` is always the trace whose ``verdict_sha256``
    belongs to the persisted verdict.  Initial and failed re-ask calls live in
    ``program_executions`` and contribute to the aggregate telemetry without
    being spliced into a trace with a different context/verdict identity.
    """

    trace["program_executions"] = list(executions)
    if not executions:
        for field_name in (
            "latency_ms",
            "model_attempts",
            "physical_model_attempts",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
            "provider_cost_microusd",
            "program_execution_index",
            "program_trace",
            "input_sha256",
            "novelty_defaulted",
            "model_fallback_from",
        ):
            trace.pop(field_name, None)
        return
    next_call_index = 0
    for execution in executions:
        execution_trace = execution.get("trace")
        calls = list(execution_trace.get("calls") or []) if isinstance(execution_trace, Mapping) else []
        execution["recording_call_indices"] = list(range(next_call_index, next_call_index + len(calls)))
        next_call_index += len(calls)
    call_count = sum(int(dict(execution.get("usage") or {}).get("call_count") or 0) for execution in executions)
    trace["latency_ms"] = sum(
        int(dict(execution.get("usage") or {}).get("wall_latency_ms") or 0) for execution in executions
    )
    trace["model_attempts"] = call_count
    trace["physical_model_attempts"] = sum(
        int(dict(execution.get("usage") or {}).get("physical_call_count") or 0) for execution in executions
    )
    for field_name in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens"):
        trace[field_name] = sum(
            int(dict(execution.get("usage") or {}).get(field_name) or 0) for execution in executions
        )
    physical_call_bearing = [
        dict(execution.get("usage") or {})
        for execution in executions
        if int(dict(execution.get("usage") or {}).get("physical_call_count") or 0) > 0
    ]
    trace["provider_cost_microusd"] = (
        sum(int(usage["provider_cost_microusd"]) for usage in physical_call_bearing)
        if all(usage.get("provider_cost_microusd") is not None for usage in physical_call_bearing)
        else None
    )

    trace.pop("program_execution_index", None)
    trace.pop("program_trace", None)
    trace.pop("input_sha256", None)
    trace.pop("novelty_defaulted", None)
    trace.pop("model_fallback_from", None)
    if selected_execution_index is None:
        return
    selected = executions[selected_execution_index]
    selected_trace = selected.get("trace")
    if not isinstance(selected_trace, Mapping):
        raise ValueError("news_selected_program_trace_missing")
    trace["program_execution_index"] = selected_execution_index
    trace["program_trace"] = dict(selected_trace)
    trace["input_sha256"] = str(selected_trace["context_sha256"])
    if bool(selected_trace.get("novelty_defaulted")):
        trace["novelty_defaulted"] = True
    if selected.get("fallback_from"):
        trace["model_fallback_from"] = str(selected["fallback_from"])


def _evaluate_canary_rolling_slo(repos: Any, *, activation_id: str, now_ms: int) -> dict[str, Any]:
    return dict(repos.news.evaluate_canary_rolling_slo(activation_id=activation_id, now_ms=now_ms))


class TriageConsumer:
    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        judge: SemanticJudge | None,
        program_version: str,
        program_sha256: str,
        watchlist_symbols: frozenset[str],
        watchlist: Sequence[str],
        concurrency: int,
        circuit_failures: int,
        circuit_open_seconds: float,
        policy: DecidePolicy = DEFAULT_POLICY,
        oi_policy: OiPolicy = DEFAULT_OI_POLICY,
        stable_bundle_sha: str | None = None,
        canary_arms: Mapping[str, CanaryRuntimeArm] | None = None,
        runtime_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.judge = judge
        self.program_version = str(program_version)
        self.program_sha256 = str(program_sha256)
        self.watchlist_symbols = watchlist_symbols
        self.watchlist = list(watchlist)
        self.concurrency = int(concurrency)
        self.circuit = _Circuit(threshold=circuit_failures, open_seconds=circuit_open_seconds)
        self._circuit_failures = int(circuit_failures)
        self._circuit_open_seconds = float(circuit_open_seconds)
        self._candidate_circuits: dict[str, _Circuit] = {}
        self._circuit_incident_open = False
        self.policy = policy
        self.oi_policy = oi_policy
        self._canary_enabled = stable_bundle_sha is not None
        self.stable_bundle_sha = (
            stable_bundle_sha
            or hashlib.sha256(
                json.dumps(
                    {"program": self.program_sha256, "policy": policy.as_dict()},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        self.canary_arms = dict(canary_arms or {})
        self.runtime_manifest = dict(runtime_manifest or {})
        # #75: symbol aliases collapse one issuer's several contracts into one storyline bucket. Loaded lazily
        # from the universe snapshot and refreshed on the same TTL as the Gate's copy; `None` uses the seeds.
        self._aliases: dict[str, str] | None = None
        self._aliases_at_ms = 0

    def _refresh_aliases(self, repos: Any, *, now: int) -> None:
        if self._aliases is not None and now - self._aliases_at_ms < _INSTRUMENT_CACHE_TTL_MS:
            return
        self._aliases_at_ms = now
        table = repos.instruments.alias_map()
        self._aliases = table or None

    async def register_runtime_manifest(self) -> None:
        if self.runtime_manifest:
            await self.db.tx(
                "news_agent_runtime_manifest",
                lambda repos: repos.news.register_agent_runtime_manifest(**self.runtime_manifest),
            )

    async def run(self, *, stop_event: asyncio.Event) -> None:
        # A fresh process starts with a closed circuit: an incident left open by a previous process is over.
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_triage_circuit_reconcile",
                lambda repos: repos.news.close_open_incidents(cause_classes=["triage_circuit_open"], now_ms=now_ms()),
            )
        await self.bus.consume(Q_TRIAGE, self.handle, prefetch=self.concurrency, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        existing = await self.db.read(
            "news_triage_existing",
            lambda repos: repos.news.get_verdict(
                event_id=event_id, stage="triage", policy_version=TRIAGE_POLICY_VERSION
            ),
        )
        if existing is not None:
            if existing.get("published_at_ms") is None and existing["final_decision"] in {"push", "escalate"}:
                await self._publish_decision(
                    event_id, existing["final_decision"], trace_id=message.trace_id, amqp_priority=message.priority
                )
            return
        bundle = await self.db.read("news_triage_load", lambda repos: self._load_with_aliases(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_event_missing")
        card, ledger_rows = bundle
        facts = GateFacts(
            grounded_assets=tuple(card.get("grounded_assets") or []),
            watchlist_symbols=self.watchlist_symbols,
            provider_score=card.get("provider_score_max"),
            priority=str(card.get("priority") or "normal"),
            admission=str(card.get("admission") or ""),
        )
        if str(card.get("admission") or "") == "telemetry_deterministic":
            # #137. Fixed-format open-interest telemetry: judged here by arithmetic instead of by two
            # structured model calls that would re-read four numbers a regex already has. Everything
            # after the judgment — decide(), the storyline lock, the verdict row, delivery, the receipt,
            # the outcome, the feed — is the ordinary path, because nothing after the judgment differs.
            await self._judge_telemetry(
                event_id=event_id,
                card=card,
                facts=facts,
                ledger_rows=ledger_rows,
                stamp=stamp,
                message=message,
            )
            return
        assignment = (
            await self.db.tx(
                "news_canary_assign",
                lambda repos: repos.news.assign_agent_arm(
                    event_id=event_id,
                    stable_bundle_sha=self.stable_bundle_sha,
                    admission=str(card.get("admission") or ""),
                    priority=str(card.get("priority") or "normal"),
                    ingest_mode=str(card.get("ingest_mode") or "live"),
                    now_ms=stamp,
                ),
            )
            if self._canary_enabled
            else {
                "activation_id": None,
                "arm": "stable",
                "bundle_sha": self.stable_bundle_sha,
                "selector_version": "test_stable_only_v1",
                "eligibility_reason": "canary_not_composed",
            }
        )
        selected_bundle_sha = str(assignment["bundle_sha"])
        selected_arm = str(assignment["arm"])
        runtime_arm = self.canary_arms.get(selected_bundle_sha) if selected_arm == "candidate" else None
        candidate_artifact_missing = selected_arm == "candidate" and runtime_arm is None
        stable_assignment_mismatch = selected_arm == "stable" and selected_bundle_sha != self.stable_bundle_sha
        assignment_shape_mismatch = selected_arm not in {"stable", "candidate"}
        if candidate_artifact_missing and assignment.get("activation_id"):
            await self._trip_canary(str(assignment["activation_id"]), "candidate_artifact_missing", stamp)
        assigned_runtime_missing = candidate_artifact_missing or stable_assignment_mismatch or assignment_shape_mismatch
        active_judge = (
            runtime_arm.program if runtime_arm is not None else (None if assigned_runtime_missing else self.judge)
        )
        active_policy = runtime_arm.policy if runtime_arm is not None else self.policy
        active_program_version = runtime_arm.program_version if runtime_arm is not None else self.program_version
        active_program_sha = runtime_arm.program_sha256 if runtime_arm is not None else self.program_sha256
        active_circuit = (
            self._candidate_circuits.setdefault(
                selected_bundle_sha,
                _Circuit(threshold=self._circuit_failures, open_seconds=self._circuit_open_seconds),
            )
            if selected_arm == "candidate"
            else self.circuit
        )
        prelim_key = str(card.get("storyline_key") or "")
        # The told ledger: what the reader already received (newest first, same preliminary storyline first). The
        # model judges novelty against it; ``told_seen`` (event ids) is the snapshot the persist step compares against
        # — ids, not clocks, because verdict rows carry their handler's start stamp, not commit time.
        context = TriageContext.from_card(
            card,
            watchlist=tuple(self.watchlist),
            told_rows=ledger_rows,
            now_ms=stamp,
            queue_lag_ms=max(0, stamp - int(message.occurred_at_ms or stamp)),
        )
        told = _told_from_context(context)
        told_seen = frozenset(str(r.get("event_id") or "") for r in ledger_rows)
        queue_lag_ms = max(0, stamp - int(message.occurred_at_ms or stamp))
        trace: dict[str, Any] = {
            "queue_lag_ms": queue_lag_ms,
            "attempt": message.attempt,
            "program_version": active_program_version,
            "program_sha256": active_program_sha,
            # The policy numbers and the Gate version that produced this decision, not just the rule name: a
            # verdict has to be replayable against the thresholds it actually ran under (#81).
            "policy": active_policy.as_dict(),
            "gate_policy_version": GATE_POLICY_VERSION,
            "evidence_version": int(card.get("evidence_version") or 0),
            "evidence_sha256": str(card.get("evidence_sha256") or ""),
            "focus_fact_id": str(card.get("focus_fact_id") or ""),
            "storyline_key_preliminary": prelim_key,
            "status": {
                "storyline_key": prelim_key,
                "preliminary": True,
                "queue_lag_ms": queue_lag_ms,
            },
            "told": _told_trace(told),
            "told_count": len(told),
            "agent_assignment": {
                "activation_id": assignment.get("activation_id"),
                "arm": selected_arm,
                "bundle_sha": selected_bundle_sha,
                "selector_version": assignment.get("selector_version"),
                "eligibility_reason": assignment.get("eligibility_reason"),
            },
        }
        model_name: str | None = None
        wire_title = str(card.get("leader_title") or "")
        reasked = False
        reask_reason: str | None = None
        first_verdict: TriageVerdict | None = None
        program_executions: list[dict[str, Any]] = []
        selected_execution_index: int | None = None
        while True:
            degraded = False
            error_code = None
            if active_judge is None:
                code = (
                    "news_canary_artifact_missing"
                    if candidate_artifact_missing
                    else "news_semantic_program_identity_mismatch"
                    if stable_assignment_mismatch or assignment_shape_mismatch
                    else "news_semantic_program_unconfigured"
                )
                verdict, _ = fallback_verdict(facts, error_code=code, title=wire_title)
                degraded, error_code = True, code
            elif active_circuit.is_open(stamp) and first_verdict is None:
                code = "news_canary_circuit_open" if selected_arm == "candidate" else "news_triage_circuit_open"
                verdict, _ = fallback_verdict(facts, error_code=code, title=wire_title)
                degraded, error_code = True, code
            elif active_circuit.is_open(stamp) and first_verdict is not None:
                code = "news_canary_circuit_open" if selected_arm == "candidate" else "news_triage_circuit_open"
                trace["reask_failed"] = code
                if reask_reason == "evidence":
                    verdict, _ = fallback_verdict(facts, error_code=code, title=wire_title)
                    degraded, error_code = True, code
                    model_name = None
                    selected_execution_index = None
                else:
                    verdict = first_verdict  # a told-only re-ask may safely keep the first semantic judgment
                    if selected_execution_index is not None:
                        program_executions[selected_execution_index]["status"] = "accepted_after_reask_failure"
            else:
                trace["watchlist"] = list(self.watchlist)
                execution_phase = "stale_reask" if first_verdict is not None else "initial"
                try:
                    call = await active_judge.judge(context)
                except SemanticJudgeError as exc:
                    program_executions.append(
                        _program_execution(
                            execution_index=len(program_executions),
                            phase=execution_phase,
                            status="failed",
                            context=context,
                            program_trace=exc.partial_trace,
                            usage=_usage_from_partial_trace(exc.partial_trace, attempts=exc.attempts),
                            error={
                                "code": exc.code,
                                "retryable": exc.retryable,
                                "output_failure": exc.output_failure,
                                "finish_reason": exc.finish_reason,
                                "failing_predictor": exc.failing_predictor,
                                "primary_code": exc.primary_code,
                            },
                        )
                    )
                    trace.update(
                        {
                            "model_failure_retryable": exc.retryable,
                            "program_error": exc.code,
                        }
                    )
                    if exc.finish_reason:
                        trace["finish_reason"] = exc.finish_reason
                    if exc.failing_predictor:
                        trace["failing_predictor"] = exc.failing_predictor
                    if exc.primary_code:
                        trace["primary_error"] = exc.primary_code
                    if exc.output_failure:
                        log.warning(
                            "news semantic program output unusable event=%s code=%s",
                            event_id,
                            exc.code,
                        )
                        if selected_arm == "candidate" and assignment.get("activation_id"):
                            await self._trip_canary(
                                str(assignment["activation_id"]), "candidate_schema_contract_breach", stamp
                            )
                    elif exc.retryable and active_circuit.record_failure(stamp):
                        if selected_arm != "candidate":
                            self._circuit_incident_open = True
                            with contextlib.suppress(TransientError, DeferError):
                                await self.db.tx(
                                    "news_triage_circuit",
                                    functools.partial(_open_circuit_incident, now_ms=stamp),
                                )
                    if first_verdict is not None:
                        trace["reask_failed"] = exc.code
                        if reask_reason == "evidence":
                            verdict, _ = fallback_verdict(facts, error_code=exc.code, title=wire_title)
                            degraded, error_code = True, exc.code
                            model_name = None
                            selected_execution_index = None
                        else:
                            # A told-only re-ask failure may keep the first valid judgment. Its evidence is unchanged;
                            # only its novelty view predates the newest delivered card.
                            verdict = first_verdict
                            if selected_execution_index is not None:
                                program_executions[selected_execution_index]["status"] = "accepted_after_reask_failure"
                    else:
                        verdict, _ = fallback_verdict(facts, error_code=exc.code, title=wire_title)
                        degraded, error_code = True, exc.code
                else:
                    execution_index = len(program_executions)
                    program_executions.append(
                        _program_execution(
                            execution_index=execution_index,
                            phase=execution_phase,
                            status="completed",
                            context=context,
                            program_trace=call.trace,
                            usage=call.usage,
                            answering_model=call.answering_model,
                            fallback_from=call.fallback_from,
                        )
                    )
                    if call.program_version != active_program_version or call.program_sha256 != active_program_sha:
                        code = "news_semantic_program_identity_mismatch"
                        program_executions[execution_index]["status"] = "identity_mismatch"
                        program_executions[execution_index]["error"] = {"code": code}
                        if selected_arm == "candidate" and assignment.get("activation_id"):
                            await self._trip_canary(str(assignment["activation_id"]), code, stamp)
                        if first_verdict is not None:
                            trace["reask_failed"] = code
                            if reask_reason == "evidence":
                                verdict, _ = fallback_verdict(facts, error_code=code, title=wire_title)
                                degraded, error_code = True, code
                                model_name = None
                                selected_execution_index = None
                            else:
                                verdict = first_verdict
                                if selected_execution_index is not None:
                                    program_executions[selected_execution_index]["status"] = (
                                        "accepted_after_reask_failure"
                                    )
                        else:
                            verdict, _ = fallback_verdict(facts, error_code=code, title=wire_title)
                            degraded, error_code = True, code
                    else:
                        active_circuit.record_success()
                        if selected_arm == "stable" and self._circuit_incident_open:
                            self._circuit_incident_open = False
                            with contextlib.suppress(TransientError, DeferError):
                                await self.db.tx(
                                    "news_triage_circuit_close",
                                    functools.partial(_close_circuit_incidents, now_ms=stamp),
                                )
                        verdict = call.verdict
                        model_name = call.answering_model
                        selected_execution_index = execution_index
                        program_executions[execution_index]["status"] = "accepted"
                        if call.fallback_from:
                            log.warning(
                                "news semantic program fallback answered event=%s model=%s primary_error=%s",
                                event_id,
                                call.answering_model,
                                call.fallback_from,
                            )
            _sync_program_audit(
                trace,
                executions=program_executions,
                selected_execution_index=selected_execution_index,
            )
            # The final storyline key comes from the verdict (primaries/scope); duplicate evidence is traced on it.
            final_key = final_storyline_key(
                title=str(card.get("leader_title") or ""),
                headline_zh=verdict.headline_zh,
                scope=verdict.scope,
                verdict_primaries=[a.symbol for a in verdict.assets if a.role == "primary"],
                grounded_assets=facts.grounded_assets,
                family=str(card.get("family") or "general"),
                aliases=self._aliases,
                degraded=degraded,
            )
            settle = _TriageSettle(
                event_id=event_id,
                evidence_version=int(card.get("evidence_version") or 0),
                evidence_sha256=str(card.get("evidence_sha256") or ""),
                focus_fact_id=str(card.get("focus_fact_id") or ""),
                verdict=verdict,
                facts=facts,
                final_key=final_key,
                told=told,
                seen=ledger_rows,
                told_seen=told_seen,
                degraded=degraded,
                error_code=error_code,
                model_name=model_name,
                program_version=active_program_version,
                program_sha256=active_program_sha,
                policy=active_policy,
                trace=trace,
                stamp=stamp,
                allow_stale=not reasked and not degraded,
            )
            outcome = await self.db.tx("news_triage_persist", functools.partial(self._decide_and_persist, s=settle))
            if outcome.stale:
                # A card landed while the model was thinking: ask once more with the ledger it did not see (rare,
                # ~0.6% of calls at 8 pushes/h) instead of pushing a restatement the reader just received. Everything
                # the model and decide() look at is re-read under a fresh stamp so the second input is consistent.
                reasked = True
                reask_reason = outcome.stale_reason
                first_verdict = verdict
                if selected_execution_index is None:
                    raise ValueError("news_stale_program_execution_missing")
                program_executions[selected_execution_index]["status"] = (
                    "superseded_evidence_change" if reask_reason == "evidence" else "superseded_stale_ledger"
                )
                trace["reask_reason"] = reask_reason
                trace[
                    "reasked_after_evidence_change" if reask_reason == "evidence" else "reasked_after_told_change"
                ] = True
                trace["first_input_sha256"] = trace.get("input_sha256")
                trace["first_verdict"] = {
                    "novelty": verdict.novelty,
                    "restates": verdict.restates,
                    "decision": verdict.decision,
                    "magnitude": verdict.magnitude,
                    "direction": verdict.direction,
                    "headline_zh": verdict.headline_zh,
                }
                stamp = now_ms()
                bundle = await self.db.read(
                    "news_triage_reload", functools.partial(self._load, event_id=event_id, stamp=stamp)
                )
                if bundle is None:
                    raise PermanentError("news_event_missing")
                card, ledger_rows = bundle
                wire_title = str(card.get("leader_title") or "")
                facts = GateFacts(
                    grounded_assets=tuple(card.get("grounded_assets") or []),
                    watchlist_symbols=self.watchlist_symbols,
                    provider_score=card.get("provider_score_max"),
                    priority=str(card.get("priority") or "normal"),
                    admission=str(card.get("admission") or ""),
                )
                refreshed_prelim_key = str(card.get("storyline_key") or "")
                if refreshed_prelim_key != prelim_key:
                    trace["first_storyline_key_preliminary"] = prelim_key
                    prelim_key = refreshed_prelim_key
                    trace["storyline_key_preliminary"] = prelim_key
                trace["evidence_version"] = int(card.get("evidence_version") or 0)
                trace["evidence_sha256"] = str(card.get("evidence_sha256") or "")
                trace["focus_fact_id"] = str(card.get("focus_fact_id") or "")
                context = TriageContext.from_card(
                    card,
                    watchlist=tuple(self.watchlist),
                    told_rows=ledger_rows,
                    now_ms=stamp,
                    queue_lag_ms=queue_lag_ms,
                )
                told = _told_from_context(context)
                told_seen = frozenset(str(r.get("event_id") or "") for r in ledger_rows)
                trace["status"] = {
                    "storyline_key": prelim_key,
                    "preliminary": True,
                    "queue_lag_ms": queue_lag_ms,
                }
                trace["told"] = _told_trace(told)
                trace["told_count"] = len(told)
                continue
            if selected_arm == "candidate" and assignment.get("activation_id"):
                with contextlib.suppress(ValueError, TransientError, DeferError):
                    await self.db.tx(
                        "news_canary_rolling_slo",
                        functools.partial(
                            _evaluate_canary_rolling_slo,
                            activation_id=str(assignment["activation_id"]),
                            now_ms=stamp,
                        ),
                    )
            break
        if outcome.final in {"push", "escalate"}:
            await self._publish_decision(
                event_id, outcome.final, trace_id=message.trace_id, amqp_priority=message.priority
            )

    async def _judge_telemetry(
        self,
        *,
        event_id: str,
        card: Mapping[str, Any],
        facts: GateFacts,
        ledger_rows: Sequence[Mapping[str, Any]],
        stamp: int,
        message: BusMessage,
    ) -> None:
        """Deterministic judgment for one telemetry frame, then the ordinary settle path.

        No model call, so no arm assignment, no circuit breaker and no Program identity: the verdict
        carries `OI_PROGRAM_VERSION` instead, which is what the trace, `news why` and the release
        cohorts read. The rank counts this symbol's other frames inside the window — counting lives
        here because `decide()` deliberately cannot count, and what `decide()` receives is an ordinary
        verdict it already knows how to rule on.
        """

        title = str(card.get("leader_title") or "")
        signal = parse_oi_signal(title)
        observed = int(card.get("opened_at_ms") or card.get("leader_published_at_ms") or stamp)
        trace: dict[str, Any] = {
            "queue_lag_ms": max(0, stamp - int(message.occurred_at_ms or stamp)),
            "attempt": message.attempt,
            "program_version": OI_PROGRAM_VERSION,
            "policy": self.policy.as_dict(),
            "gate_policy_version": GATE_POLICY_VERSION,
            "evidence_version": int(card.get("evidence_version") or 0),
            "evidence_sha256": str(card.get("evidence_sha256") or ""),
            "focus_fact_id": str(card.get("focus_fact_id") or ""),
            "storyline_key_preliminary": str(card.get("storyline_key") or ""),
            "told": [],
            "told_count": 0,
        }
        if signal is None:
            # `1019` is provider provenance, not a parser guarantee. A frame that is not the template
            # carries no numbers this rule can act on; it is dropped deterministically rather than
            # falling through to a model call the Gate admitted it specifically to avoid.
            log.info("news telemetry frame not parseable event=%s title=%r", event_id, title[:120])
            verdict = TriageVerdict(
                novelty="new_fact",
                event_type="noise",
                assets=[],
                direction="neutral",
                scope="single_name",
                magnitude=0,
                actionable=False,
                confidence=1.0,
                decision="drop",
                headline_zh=title[:60] or "持仓异动帧无法解析",
                why_zh="",
            )
            trace["oi_signal"] = {"parsed": False}
        else:
            earlier = await self.db.read(
                "news_signal_history",
                lambda repos: repos.news.recent_oi_signal_times(
                    symbol=signal.symbol,
                    metric_version=OI_METRIC_VERSION,
                    since_ms=observed - self.oi_policy.window_ms,
                    before_ms=observed,
                    exclude_event_id=event_id,
                ),
            )
            judgment = evaluate_oi(signal, earlier_at_ms=earlier, now_ms=observed, policy=self.oi_policy)
            verdict = judgment.verdict
            trace["oi_signal"] = {
                "parsed": True,
                "symbol": signal.symbol,
                "direction": signal.direction,
                "oi_change_bps": signal.oi_change_bps,
                "oi_value_usd": signal.oi_value_usd,
                "whale_long_profit_bps": signal.whale_long_profit_bps,
                "whale_oi_ratio_bps": signal.whale_oi_ratio_bps,
                "rank_in_window": judgment.rank_in_window,
                "rule": judgment.rule,
                "policy": self.oi_policy.as_dict(),
            }
            # The rank ledger, written before the verdict. The failure is not swallowed: losing the
            # row would let the *next* frame rank itself lower and push when it should not, which is
            # the unsafe direction. Letting it propagate retries the message, and the insert is
            # idempotent (`ON CONFLICT DO NOTHING`, and the rank read excludes this Event).
            await self.db.tx(
                "news_signal_record",
                lambda repos: repos.news.insert_oi_signal(
                    event_id=event_id,
                    metric_version=OI_METRIC_VERSION,
                    symbol=signal.symbol,
                    direction=signal.direction,
                    oi_change_bps=signal.oi_change_bps,
                    oi_value_usd=signal.oi_value_usd,
                    whale_long_profit_bps=signal.whale_long_profit_bps,
                    whale_oi_ratio_bps=signal.whale_oi_ratio_bps,
                    observed_at_ms=observed,
                    rank_in_window=judgment.rank_in_window,
                    now_ms=stamp,
                ),
            )
        final_key = final_storyline_key(
            title=title,
            headline_zh=verdict.headline_zh,
            scope=verdict.scope,
            verdict_primaries=[a.symbol for a in verdict.assets if a.role == "primary"],
            grounded_assets=facts.grounded_assets,
            family=str(card.get("family") or "general"),
            aliases=self._aliases,
            degraded=False,
        )
        settle = _TriageSettle(
            event_id=event_id,
            evidence_version=int(card.get("evidence_version") or 0),
            evidence_sha256=str(card.get("evidence_sha256") or ""),
            focus_fact_id=str(card.get("focus_fact_id") or ""),
            verdict=verdict,
            facts=facts,
            final_key=final_key,
            told=[],
            seen=ledger_rows,
            told_seen=frozenset(str(r.get("event_id") or "") for r in ledger_rows),
            degraded=False,
            error_code=None,
            model_name=None,
            program_version=OI_PROGRAM_VERSION,
            program_sha256=program_sha256(self.oi_policy),
            policy=self.policy,
            trace=trace,
            stamp=stamp,
            # No model was thinking, so no card can have landed while it was: nothing to re-ask.
            allow_stale=False,
        )
        outcome = await self.db.tx("news_triage_persist", functools.partial(self._decide_and_persist, s=settle))
        if outcome.final in {"push", "escalate"}:
            await self._publish_decision(
                event_id, outcome.final, trace_id=message.trace_id, amqp_priority=message.priority
            )

    def _decide_and_persist(self, repos: Any, s: _TriageSettle) -> _TriageOutcome:
        """Inside one transaction, under the storyline's advisory lock: re-read the newest reader evidence and
        told entry, decide, and insert the verdict. Reports ``stale`` (no write) when a card landed after the model
        saw the ledger and the caller may still re-ask."""

        repos.news.lock_storyline(s.final_key)
        latest_evidence = repos.news.latest_evidence_snapshot(s.event_id)
        if latest_evidence is None:
            raise PermanentError("news_event_evidence_missing")
        evidence_changed = (
            int(latest_evidence["evidence_version"]) != s.evidence_version
            or str(latest_evidence["evidence_sha256"]) != s.evidence_sha256
        )
        if evidence_changed:
            if s.allow_stale:
                return _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="evidence")
            # A second concurrent evidence change is not safe to bind to the
            # already-produced verdict.  Reconsume after the durable retry lane
            # rather than publishing a judgment over evidence it did not read.
            raise TransientError("news_event_evidence_changed")
        if s.allow_stale:
            fresh = repos.news.told_ledger(now_ms=s.stamp, window_ms=TOLD_WINDOW_MS, limit=TOLD_MAX * 2)
            if any(str(r.get("event_id") or "") not in s.told_seen for r in fresh):
                return _TriageOutcome(stale=True, final="drop", decision=None, stale_reason="told")
        status = storyline_status(s.final_key, told=s.told, seen=s.seen)
        trace = s.trace
        decision = decide(
            s.verdict,
            s.facts,
            status,
            degraded=s.degraded,
            policy=s.policy,
        )
        if s.degraded and decision.final in {"push", "escalate"} and decision.rule_baseline == "drop":
            decision = DecisionResult(
                "drop", "fail_closed_fallback", None, decision.rule_baseline, decision.watchlist_hits
            )
        trace["status_final"] = {"storyline_key": s.final_key}
        trace["storyline_key"] = s.final_key
        trace["seen_count"] = len(status.seen_headlines)
        if decision.seen_similarity is not None:
            # What the duplicate check actually measured, so `news why` can name the card this one resembled instead of
            # reporting a bare rule (#81). ``seen_scope=all`` means the normal
            # push path was compared with the received-card ledger.
            trace["seen_similarity"] = round(float(decision.seen_similarity), 4)
            trace["seen_scope"] = decision.seen_scope
            if 0 <= decision.seen_against < len(s.seen):
                # `seen_headlines` was built from `s.seen` in order, so the index names that ledger row.
                row = s.seen[decision.seen_against]
                trace["seen_against"] = {
                    "event_id": str(row.get("event_id") or ""),
                    "headline_zh": str(row.get("headline_zh") or ""),
                    "at_ms": int(row.get("at_ms") or 0),
                }
        if grounded_restatement(s.verdict, status):
            trace["restates_event_id"] = s.told[s.verdict.restates]["event_id"]
        final = decision.final
        reason = decision.throttled_by or decision.override_rule or ""
        context_line = (
            f"[{s.verdict.audience}/{s.verdict.event_type}/{s.verdict.direction} m{s.verdict.magnitude}"
            f" → {final}·{reason}] {s.verdict.headline_zh}"
        )
        repos.news.insert_verdict(
            event_id=s.event_id,
            stage="triage",
            policy_version=TRIAGE_POLICY_VERSION,
            model_decision=None
            if s.degraded and s.error_code == "news_semantic_program_unconfigured"
            else s.verdict.decision,
            rule_baseline_decision=decision.rule_baseline,
            final_decision=final,
            override_rule=decision.override_rule,
            throttled_by=decision.throttled_by,
            verdict=json_ready(s.verdict),
            model=s.model_name,
            program_version=s.program_version,
            program_sha256=s.program_sha256,
            degraded=s.degraded,
            error_code=s.error_code,
            trace=trace,
            evidence_version=s.evidence_version,
            evidence_sha256=s.evidence_sha256,
            focus_fact_id=s.focus_fact_id,
            now_ms=s.stamp,
        )
        repos.news.set_storyline_key(event_id=s.event_id, storyline_key=s.final_key, now_ms=s.stamp)
        repos.news.set_context_line(event_id=s.event_id, context_line=context_line, followup_of=None, now_ms=s.stamp)
        return _TriageOutcome(stale=False, final=final, decision=decision)

    async def _trip_canary(self, activation_id: str, reason: str, stamp: int) -> None:
        with contextlib.suppress(ValueError, TransientError, DeferError):
            await self.db.tx(
                "news_canary_trip",
                lambda repos: repos.news.transition_canary(
                    activation_id=activation_id,
                    target_state="tripped",
                    reason=reason,
                    now_ms=stamp,
                ),
            )

    def _load_with_aliases(
        self, repos: Any, event_id: str, stamp: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """The Triage bundle plus a refreshed alias table, both from the one session (#75)."""

        self._refresh_aliases(repos, now=stamp)
        return self._load(repos, event_id, stamp)

    @staticmethod
    def _load(repos: Any, event_id: str, stamp: int) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        card = repos.news.event_card(event_id)
        if card is None:
            return None
        ledger = repos.news.told_ledger(
            now_ms=stamp,
            window_ms=TOLD_WINDOW_MS,
            limit=_SEEN_LEDGER_MAX,
            prefer_key=str(card.get("storyline_key") or "") or None,
        )
        return card, ledger

    async def _publish_decision(self, event_id: str, final: str, *, trace_id: str, amqp_priority: int) -> None:
        stamp = now_ms()
        await self.bus.publish(
            BusMessage(
                kind="verdict",
                message_id=f"push:{event_id}",
                routing_key=RK_VERDICT_PUSH,
                payload={"event_id": event_id, "kind": "first"},
                trace_id=trace_id,
                occurred_at_ms=stamp,
                priority=amqp_priority,
            )
        )
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_triage_mark_published",
                lambda repos: repos.news.mark_verdict_published(
                    event_id=event_id, stage="triage", policy_version=TRIAGE_POLICY_VERSION, now_ms=stamp
                ),
                timeout_seconds=1.0,
            )


# ---------------------------------------------------------------------------- Deliverer
# The quote read gets its own short session. A price is display-only and must
# never delay, retry, or suppress a delivery; every failure degrades to no
# market line while the card proceeds normally (#113).
_QUOTE_READ_TIMEOUT_SECONDS = 1.5


class DelivererConsumer:
    """SAC consumer: one Feishu attempt per (event, kind); crash between send and ack never resends."""

    def __init__(
        self,
        *,
        bus: Any,
        db: Any,
        sender: Any | None,
        finite_operations: Any,
        min_interval_seconds: float,
    ) -> None:
        self.bus = bus
        self.db = _Db(db)
        self.sender = sender
        self.finite = finite_operations
        self.min_interval = float(min_interval_seconds)
        self._last_send_at = 0.0

    async def run(self, *, stop_event: asyncio.Event) -> None:
        with contextlib.suppress(TransientError, DeferError):
            await self.db.tx(
                "news_delivery_reconcile", lambda repos: repos.news.terminalize_interrupted_deliveries(now_ms=now_ms())
            )
        await self.bus.consume(Q_DELIVER, self.handle, prefetch=1, stop_event=stop_event)

    async def handle(self, message: BusMessage) -> None:
        event_id = str(message.payload.get("event_id") or "")
        kind = "first"  # one Event, one card; there is no follow-up lane
        if not event_id:
            raise PermanentError("news_event_id_missing")
        stamp = now_ms()
        bundle = await self.db.read("news_delivery_load", lambda repos: self._load(repos, event_id, stamp))
        if bundle is None:
            raise PermanentError("news_delivery_inputs_missing")
        card, triage_row = bundle
        tv = dict(triage_row.get("verdict") or {})
        if triage_row["final_decision"] not in {"push", "escalate"}:
            return
        if self.sender is None:
            await self._settle_direct(event_id, kind, "delivery_unavailable", stamp)
            return
        # Only query a quote after every policy return above. A quote failure
        # never changes the delivery decision.
        quotes = await self._quotes(card, tv, stamp)
        card_payload = render_first_card(
            event=card,
            verdict=tv,
            decision=str(triage_row["final_decision"]),
            grounded_assets=list(card.get("grounded_assets") or []),
            degraded=bool(triage_row.get("degraded")),
            quotes=quotes,
        )
        state = await self.db.tx(
            "news_delivery_begin",
            lambda repos: repos.news.begin_delivery(event_id=event_id, kind=kind, card=card_payload, now_ms=stamp),
        )
        if state != "new":
            if state == "sending":
                await self.db.tx(
                    "news_delivery_ambiguous",
                    lambda repos: repos.news.settle_delivery(
                        event_id=event_id,
                        kind=kind,
                        state="terminal",
                        receipt=None,
                        error_code="ambiguous_after_crash",
                        now_ms=now_ms(),
                    ),
                )
            return
        wait = self.min_interval - (time.monotonic() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        error_code: str | None = None
        receipt: dict[str, Any] | None = None
        try:
            result = await self.finite.run(
                "news_delivery_feishu_send", self.sender.send_card, card_payload, timeout_seconds=8.0
            )
            receipt = dict(result)
        except Exception as exc:
            error_code = getattr(exc, "code", None) or f"news_delivery_failed:{type(exc).__name__}"
        finally:
            self._last_send_at = time.monotonic()
        settled_state = "sent" if error_code is None else "terminal"
        try:
            await self.db.tx(
                "news_delivery_settle",
                lambda repos: repos.news.settle_delivery(
                    event_id=event_id,
                    kind=kind,
                    state=settled_state,
                    receipt=receipt,
                    error_code=error_code,
                    now_ms=now_ms(),
                ),
            )
        except (TransientError, DeferError) as exc:
            raise RuntimeError("news_delivery_settlement_unavailable") from exc

    async def _settle_direct(self, event_id: str, kind: str, error_code: str, stamp: int) -> None:
        def _fn(repos: Any) -> None:
            state = repos.news.begin_delivery(event_id=event_id, kind=kind, card={}, now_ms=stamp)
            if state == "new":
                repos.news.settle_delivery(
                    event_id=event_id, kind=kind, state="terminal", receipt=None, error_code=error_code, now_ms=stamp
                )

        await self.db.tx("news_delivery_settle_direct", _fn)

    async def _quotes(self, card: Mapping[str, Any], verdict: Mapping[str, Any], stamp: int) -> list[Any]:
        """Fresh prices for exactly the assets rendered on the card.

        `card_assets()` is shared with the renderer, so the facts and quote
        lines cannot describe different symbols. Resolution remains owned by
        PriceRepository. Every price-plane failure returns an empty display
        value and leaves the already-made send decision untouched.
        """

        shown = card_assets(dict(verdict), list(card.get("grounded_assets") or []))
        if not shown:
            return []
        try:
            rows = await self.db.read(
                "news_delivery_quotes",
                lambda repos: repos.price.quotes_for_symbols(shown, now_ms=stamp),
                timeout_seconds=_QUOTE_READ_TIMEOUT_SECONDS,
            )
        except Exception:  # price is display-only; all failures degrade to no line
            return []
        return list(rows or [])

    @staticmethod
    def _load(repos: Any, event_id: str, stamp: int) -> tuple[Any, ...] | None:
        card = repos.news.event_card(event_id)
        triage = repos.news.latest_verdict(event_id=event_id, stage="triage")
        if card is None or triage is None:
            return None
        return card, triage

    async def close(self) -> None:
        if self.sender is not None:
            with contextlib.suppress(Exception):
                await self.finite.run(
                    "news_delivery_sender_close", self.sender.close, timeout_seconds=5.0, allow_shutdown=True
                )


# ---------------------------------------------------------------------------- Janitor
# ---------------------------------------------------------------------------- Instrument universe
class InstrumentSnapshotLoop:
    """Venue listing catalogues -> `news_market_instruments`, one bounded snapshot per period (#75).

    The universe is a provider fact, so the snapshot is idempotent and rebuildable: re-running it on an unchanged
    catalogue only moves `last_seen_ms`. It feeds symbol normalization and the Gate's asset class — not listing
    cards, which arrive as provider frames (#89).

    A venue that fails is skipped, never fatal: `apply_snapshot` only reconciles venues that actually answered, so
    an unreachable Binance cannot read as a mass delisting.
    """

    def __init__(
        self,
        *,
        db: Any,
        fetchers: Sequence[tuple[str, Callable[[], Any]]],
        period_seconds: float = _INSTRUMENT_SNAPSHOT_PERIOD_SECONDS,
        enabled: bool = True,
    ) -> None:
        self.db = _Db(db)
        self.fetchers = tuple(fetchers)
        self.period = float(period_seconds)
        self.enabled = bool(enabled)
        self.last_result: Any | None = None
        self.last_error: str | None = None

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if not self.enabled or not self.fetchers:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            ok = await self.turn()
            await _sleep_or_stop(stop_event, self.period if ok else _INSTRUMENT_RETRY_SECONDS)

    async def turn(self) -> bool:
        """One snapshot. Returns False when no venue answered, so the caller retries sooner."""

        instruments: list[Any] = []
        errors: list[str] = []
        for venue, fetch in self.fetchers:
            try:
                instruments.extend(await fetch())
            except Exception as exc:  # adapters raise VenueExpectedError; anything else is equally non-fatal here
                code = getattr(exc, "code", None) or type(exc).__name__
                errors.append(f"{venue}:{code}")
                log.warning("news instrument snapshot venue failed venue=%s code=%s", venue, code)
        self.last_error = ",".join(errors) or None
        if not instruments:
            return False
        stamp = now_ms()

        def _apply(repos: Any, items: list[Any] = instruments, s: int = stamp) -> Any:
            repos.instruments.reconcile_seed_aliases(now_ms=s)
            result = repos.instruments.apply_snapshot(items, now_ms=s)
            repos.instruments.learn_aliases_from_universe(now_ms=s)
            return result, repos.instruments.dangling_seed_aliases()

        try:
            result, dangling = await self.db.tx("news_instrument_snapshot", _apply, timeout_seconds=30.0)
        except (TransientError, DeferError) as exc:
            self.last_error = f"db:{type(exc).__name__}"
            return False
        self.last_result = result
        log.info(
            "news instrument snapshot venues=%s total=%d delisted=%d",
            ",".join(result.venues),
            result.total,
            result.delisted,
        )
        # A seed alias pointing at a symbol no venue lists resolves to nothing, silently, forever (#89).
        for row in dangling:
            log.warning(
                "news instrument seed alias resolves to nothing alias=%s base=%s", row["alias"], row["base_symbol"]
            )
        return True


class JanitorLoop:
    """Outbox catch-up, band expiry, retention, broker snapshot — one bounded turn per period."""

    def __init__(
        self,
        *,
        db: Any,
        bus: Any | None = None,
        period_seconds: float = _JANITOR_PERIOD_SECONDS,
        retention_raw_days: int = 30,
        retention_judged_days: int = 365,
    ) -> None:
        self.db = _Db(db)
        self.cold_db = _Db(db, cold=True)
        self.bus = bus
        self.period = float(period_seconds)
        # Two tiers (#81): a raw Item nobody judged is storage, an Item behind a judged or labelled Event is the
        # corpus every later comparison replays against.
        self.retention_raw_ms = int(retention_raw_days) * _DAY_MS
        self.retention_judged_ms = int(retention_judged_days) * _DAY_MS

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.turn()
            await _sleep_or_stop(stop_event, self.period)

    async def turn(self) -> None:
        stamp = now_ms()
        if self.bus is not None:
            with contextlib.suppress(TransientError, DeferError, Exception):
                await self.republish_unpublished()
        try:

            def _janitor(repos: Any, s: int = stamp) -> dict[str, Any]:
                repos.news.expire_bands(now_ms=s)
                repos.news.purge_before(
                    cutoff_ms=s - self.retention_raw_ms, judged_cutoff_ms=s - self.retention_judged_ms
                )
                return dict(repos.news.purge_learning_retention(batch_size=500))

            retention = await self.cold_db.tx("news_janitor", _janitor, timeout_seconds=10.0)
            deleted = sum(
                int(retention.get(field) or 0) for field in ("deleted_recordings", "deleted_cases", "deleted_artifacts")
            )
            if deleted:
                log.info("news learning retention deleted=%d detail=%s", deleted, retention)
        except Exception as exc:
            error_code = f"learning_retention_failed:{type(exc).__name__}"
            log.warning("news learning retention failed code=%s", error_code)
            with contextlib.suppress(Exception):

                def _retention_error(repos: Any, s: int = stamp, code: str = error_code) -> None:
                    repos.news.record_learning_retention_error(error_code=code, now_ms=s)

                await self.cold_db.tx("news_learning_retention_error", _retention_error, timeout_seconds=2.0)
        if self.bus is not None:
            snapshot: dict[str, Any] = {"configured": True, "connected": False, "queues": {}, "error_code": None}
            try:
                depths = await asyncio.wait_for(self.bus.queue_depths(), timeout=5.0)
                prefix = f"{self.bus.prefix}." if getattr(self.bus, "prefix", "") else ""
                snapshot.update(
                    connected=True,
                    queues={name.removeprefix(prefix): value for name, value in depths.items()},
                )
            except Exception as exc:
                snapshot["error_code"] = f"broker_snapshot_failed:{type(exc).__name__}"
            with contextlib.suppress(TransientError, DeferError):

                def _snapshot(repos: Any, s: int = stamp, snap: dict[str, Any] = snapshot) -> None:
                    repos.news.update_broker_snapshot(snapshot=snap, now_ms=s)

                await self.db.tx("news_broker_snapshot", _snapshot, timeout_seconds=3.0)

    async def republish_unpublished(self) -> int:
        """Commit-then-crash (or publish failure) before publish: re-publish candidate Events that never left."""

        stamp = now_ms()
        floor_ms, ceiling_ms = stamp - _OUTBOX_MIN_AGE_MS, stamp - OUTBOX_MAX_AGE_MS

        def _scan(repos: Any) -> Any:
            return repos.news.outbox_scan(older_than_ms=floor_ms, newer_than_ms=ceiling_ms)

        rows, expired = await self.db.read("news_outbox_unpublished", _scan)
        if expired:
            # Never silent: the ceiling gave up on these, and that is a fact an operator should see.
            log.warning(
                "news outbox gave up on %d stranded event(s) older than %d min (#76)",
                expired,
                OUTBOX_MAX_AGE_MS // 60_000,
            )
        republished = 0
        for row in rows:
            event_id = str(row["event_id"])

            def _card(repos: Any, e: str = event_id) -> Any:
                return repos.news.event_card(e)

            card = await self.db.read("news_outbox_card", _card)
            if card is None:
                continue
            await publish_event(
                self.bus,
                self.db,
                event_id=str(card["event_id"]),
                family=str(card["family"]),
                priority=str(card["priority"]),
                trace_id=str(card.get("trace_id") or new_trace_id()),
            )
            republished += 1
        return republished


# ---------------------------------------------------------------------------- helpers
async def _receive_or_stop(client: Any, *, stop_event: asyncio.Event) -> Any | None:
    receive_task = asyncio.create_task(client.receive())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait({receive_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and stop_task.result():
            return None
        return await receive_task
    finally:
        for task in (receive_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive_task, stop_task, return_exceptions=True)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))


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


__all__ = [
    "DeduperConsumer",
    "DelivererConsumer",
    "EventReactionLoop",
    "InstrumentSnapshotLoop",
    "JanitorLoop",
    "NewsPipeline",
    "OpenNewsReceiver",
    "QuoteSnapshotLoop",
    "RecoveryRunner",
    "TriageConsumer",
    "publish_event",
]
