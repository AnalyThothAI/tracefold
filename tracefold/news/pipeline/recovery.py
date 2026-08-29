"""Official OpenNews history recovery stage."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from ..bus import (
    RK_RAW_RECOVERY,
    BrokerBackpressure,
    BrokerUnavailable,
    BusMessage,
    DeferError,
    TransientError,
    new_trace_id,
    now_ms,
)
from ..opennews import OpenNewsHistoryError, OpenNewsStrategyHistory, enabled_strategy_ids, parse_opennews_strategy_hits
from ..telemetry import NewsRecoveryBudget, NewsRecoveryOutcome, NewsTelemetryPort, NewsWorkSemantics
from .runtime import NewsDatabasePort, _sleep_or_stop

_HISTORY_PAGE_SIZE = 100
_RECOVERY_OVERLAP_MS = 30_000
_RECOVERY_SCAN_SECONDS = 300.0
_RECOVERY_BACKOFF_INITIAL_SECONDS = 5.0
_RECOVERY_BACKOFF_MAX_SECONDS = 60.0
_RECOVERY_CONTINUE_SECONDS = 1.0
_RECOVERY_MAX_WALL_SECONDS = 30.0
_RECOVERY_MAX_PROVIDER_CALLS = 60
_RECOVERY_MAX_PUBLISHED_MESSAGES = 1_000
_EMPTY_STRATEGY_ERROR = "opennews_history_strategy_list_empty"
_RETRYABLE_RECOVERY_ERRORS = (
    OpenNewsHistoryError,
    BrokerBackpressure,
    BrokerUnavailable,
    DeferError,
    TransientError,
)


class _RecoveryBudgetExhausted(RuntimeError):
    def __init__(self, budget: NewsRecoveryBudget) -> None:
        super().__init__(f"opennews_recovery_{budget}_budget")
        self.budget = budget


@dataclass(slots=True)
class _TurnBudget:
    max_wall_seconds: float
    max_provider_calls: int
    max_published_messages: int
    started_at: float
    provider_calls: int = 0
    published_messages: int = 0

    def _remaining_seconds(self) -> float:
        remaining = self.max_wall_seconds - (time.perf_counter() - self.started_at)
        if remaining <= 0:
            raise _RecoveryBudgetExhausted("wall_time")
        return remaining

    def checkpoint(self) -> float:
        return self._remaining_seconds()

    def provider_timeout(self) -> float:
        remaining = self._remaining_seconds()
        if self.provider_calls >= self.max_provider_calls:
            raise _RecoveryBudgetExhausted("provider_calls")
        self.provider_calls += 1
        return remaining

    def before_publish(self) -> None:
        self._remaining_seconds()
        if self.published_messages >= self.max_published_messages:
            raise _RecoveryBudgetExhausted("published_messages")

    def published(self) -> None:
        self.published_messages += 1


@dataclass(slots=True)
class _IncidentCursor:
    strategy_ids: tuple[str, ...]
    strategy_index: int = 0
    page: int = 1
    event_index: int = 0
    recovered_count: int = 0
    complete: bool = True


class RecoveryRunner:
    """Closed incidents -> official Strategy hits -> raw.recovery.* messages (never delivered)."""

    work_semantics: ClassVar[tuple[NewsWorkSemantics, ...]] = ("durable_event",)

    def __init__(
        self,
        *,
        bus: Any,
        db: NewsDatabasePort,
        history_client: OpenNewsStrategyHistory,
        telemetry: NewsTelemetryPort | None = None,
        scan_interval_seconds: float = _RECOVERY_SCAN_SECONDS,
        backoff_initial_seconds: float = _RECOVERY_BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = _RECOVERY_BACKOFF_MAX_SECONDS,
        max_wall_seconds: float = _RECOVERY_MAX_WALL_SECONDS,
        max_provider_calls: int = _RECOVERY_MAX_PROVIDER_CALLS,
        max_published_messages: int = _RECOVERY_MAX_PUBLISHED_MESSAGES,
    ) -> None:
        if history_client is None:
            raise ValueError("opennews_history_client_required")
        if (
            min(
                scan_interval_seconds,
                backoff_initial_seconds,
                backoff_max_seconds,
                max_wall_seconds,
                max_published_messages,
            )
            <= 0
            or max_provider_calls < 2
        ):
            raise ValueError("opennews_recovery_budget_invalid")
        self.bus = bus
        self.db = db
        self.history_client = history_client
        self.telemetry = telemetry
        self.scan_interval_seconds = float(scan_interval_seconds)
        self.backoff_initial_seconds = float(backoff_initial_seconds)
        self.backoff_max_seconds = max(float(backoff_max_seconds), self.backoff_initial_seconds)
        self.max_wall_seconds = float(max_wall_seconds)
        self.max_provider_calls = int(max_provider_calls)
        self.max_published_messages = int(max_published_messages)
        self._requested = asyncio.Event()
        # ponytail: this cursor only avoids replay inside one process; stable message IDs make restart replay safe.
        self._cursors: dict[int, _IncidentCursor] = {}

    def request(self) -> None:
        self._requested.set()

    async def run(self, *, stop_event: asyncio.Event) -> None:
        self._requested.set()  # startup pass
        retry_delay = self.backoff_initial_seconds
        while not stop_event.is_set():
            waiter = asyncio.create_task(self._requested.wait())
            stopper = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait(
                    {waiter, stopper}, timeout=self.scan_interval_seconds, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (waiter, stopper):
                    task.cancel()
                await asyncio.gather(waiter, stopper, return_exceptions=True)
            if stop_event.is_set():
                return
            self._requested.clear()
            started = time.perf_counter()
            budget = self._new_budget(started)
            try:
                outcome = await self._recover_pending(budget)
            except _RecoveryBudgetExhausted as exc:
                self._record_turn("budget", budget=budget, started=started, exhausted_budget=exc.budget)
                retry_delay = self.backoff_initial_seconds
                await _sleep_or_stop(stop_event, _RECOVERY_CONTINUE_SECONDS)
                self._requested.set()
            except _RETRYABLE_RECOVERY_ERRORS:
                self._record_turn("transient", budget=budget, started=started)
                await _sleep_or_stop(stop_event, retry_delay)
                retry_delay = min(self.backoff_max_seconds, retry_delay * 2)
                self._requested.set()
            else:
                self._record_turn(outcome, budget=budget, started=started)
                retry_delay = self.backoff_initial_seconds

    def _new_budget(self, started_at: float | None = None) -> _TurnBudget:
        return _TurnBudget(
            max_wall_seconds=self.max_wall_seconds,
            max_provider_calls=self.max_provider_calls,
            max_published_messages=self.max_published_messages,
            started_at=time.perf_counter() if started_at is None else started_at,
        )

    def _record_turn(
        self,
        outcome: NewsRecoveryOutcome,
        *,
        budget: _TurnBudget,
        started: float,
        exhausted_budget: NewsRecoveryBudget | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        self.telemetry.record_external_data_turn(
            "opennews_recovery",
            "error" if outcome == "transient" else ("partial" if outcome in {"budget", "partial"} else "success"),
            time.perf_counter() - started,
            source_count=1,
        )
        self.telemetry.record_news_opennews_recovery_turn(
            outcome,
            provider_calls=budget.provider_calls,
            published_messages=budget.published_messages,
            exhausted_budget=exhausted_budget,
        )

    async def _provider_strategy_ids(self, budget: _TurnBudget) -> tuple[str, ...]:
        """Which Strategies to pull history for: the provider's own enabled list, read fresh.

        Live ingestion needs no list at all — the socket pushes what the account enabled. Recovery does, only
        because the provider's hits endpoint is per-strategy. Reading it per pass rather than caching it at
        startup means a Strategy enabled mid-run is recovered too.

        A failed read raises rather than returning nothing. The caller terminalizes only the provider's
        explicit no-history response; every other typed provider fault leaves incidents pending.
        """

        payload = await self._provider_call(
            lambda: self.history_client.get_strategy_list(limit=100, page=1), budget=budget
        )
        return tuple(sorted(enabled_strategy_ids(payload)))

    async def _recover_pending(self, budget: _TurnBudget | None = None) -> NewsRecoveryOutcome:
        budget = budget or self._new_budget()
        incidents = await self.db.read(
            "news_recovery_pending",
            lambda repos: repos.news.pending_recovery_incidents(),
            timeout_seconds=min(3.0, budget.checkpoint()),
        )
        if not incidents:
            return "no_work"
        try:
            strategy_ids = await self._provider_strategy_ids(budget)
        except _RecoveryBudgetExhausted:
            raise
        except OpenNewsHistoryError as exc:
            if exc.code == "opennews_history_unavailable":
                for incident in incidents:
                    await self._complete_no_history(incident, error_code=exc.code, budget=budget)
                return "partial"
            for incident in incidents:
                await self._record_recovery_error(int(incident["incident_id"]), exc.code, budget=budget)
            raise

        if not strategy_ids:
            error = OpenNewsHistoryError(_EMPTY_STRATEGY_ERROR)
            for incident in incidents:
                await self._record_recovery_error(int(incident["incident_id"]), error.code, budget=budget)
            raise error

        partial = False
        for incident in incidents:
            budget.checkpoint()
            incident_id = int(incident["incident_id"])
            stamp = now_ms()
            from_ms = max(
                0, int(incident.get("recovery_from_at_ms") or incident["opened_at_ms"]) - _RECOVERY_OVERLAP_MS
            )
            to_ms = int(incident.get("recovery_to_at_ms") or incident.get("closed_at_ms") or stamp)
            try:
                complete, count = await self._recover_incident(
                    incident_id,
                    strategy_ids=strategy_ids,
                    from_ms=from_ms,
                    to_ms=to_ms,
                    budget=budget,
                )
            except OpenNewsHistoryError as exc:
                if exc.code == "opennews_history_unavailable":
                    await self._complete_no_history(incident, error_code=exc.code, budget=budget)
                    partial = True
                    continue
                await self._record_recovery_error(incident_id, exc.code, budget=budget)
                raise
            except (BrokerBackpressure, BrokerUnavailable) as exc:
                await self._record_recovery_error(
                    incident_id,
                    "news_broker_backpressure" if isinstance(exc, BrokerBackpressure) else "news_broker_unavailable",
                    budget=budget,
                )
                raise
            except _RecoveryBudgetExhausted as exc:
                if exc.budget != "wall_time":
                    await self._record_recovery_error(incident_id, str(exc), budget=budget)
                raise

            status: Literal["partial", "recovered"] = "recovered" if complete else "partial"
            await self._complete_incident(
                incident_id,
                status=status,
                recovered_count=count,
                error_code=None if complete else "opennews_history_retention_partial",
                from_ms=from_ms,
                to_ms=to_ms,
                budget=budget,
            )
            partial = partial or not complete
        return "partial" if partial else "success"

    async def _complete_no_history(
        self,
        incident: Mapping[str, Any],
        *,
        error_code: str,
        budget: _TurnBudget,
    ) -> None:
        budget.checkpoint()
        incident_id = int(incident["incident_id"])
        recovered_count = self._cursors.get(incident_id, _IncidentCursor(())).recovered_count
        stamp = now_ms()
        from_ms = max(0, int(incident.get("recovery_from_at_ms") or incident["opened_at_ms"]) - _RECOVERY_OVERLAP_MS)
        to_ms = int(incident.get("recovery_to_at_ms") or incident.get("closed_at_ms") or stamp)
        await self._complete_incident(
            incident_id,
            status="partial" if recovered_count else "unavailable",
            recovered_count=recovered_count,
            error_code=error_code,
            from_ms=from_ms,
            to_ms=to_ms,
            budget=budget,
        )

    async def _record_recovery_error(
        self,
        incident_id: int,
        error_code: str,
        *,
        budget: _TurnBudget,
    ) -> None:
        await self.db.tx(
            "news_recovery_error",
            lambda repos: repos.news.record_recovery_error(
                incident_id=incident_id, error_code=error_code, now_ms=now_ms()
            ),
            timeout_seconds=min(3.0, budget.checkpoint()),
        )

    async def _complete_incident(
        self,
        incident_id: int,
        *,
        status: Literal["partial", "recovered", "unavailable"],
        recovered_count: int,
        error_code: str | None,
        from_ms: int | None,
        to_ms: int | None,
        budget: _TurnBudget,
    ) -> None:
        await self.db.tx(
            "news_recovery_complete",
            lambda repos: repos.news.complete_recovery(
                incident_id=incident_id,
                status=status,
                recovered_count=recovered_count,
                error_code=error_code,
                recovery_from_at_ms=from_ms,
                recovery_to_at_ms=to_ms,
                now_ms=now_ms(),
            ),
            timeout_seconds=min(3.0, budget.checkpoint()),
        )
        self._cursors.pop(incident_id, None)

    async def _recover_incident(
        self,
        incident_id: int,
        *,
        strategy_ids: tuple[str, ...],
        from_ms: int,
        to_ms: int,
        budget: _TurnBudget,
    ) -> tuple[bool, int]:
        cursor = self._cursors.get(incident_id)
        if cursor is None or cursor.strategy_ids != strategy_ids:
            cursor = self._cursors[incident_id] = _IncidentCursor(strategy_ids)

        while cursor.strategy_index < len(strategy_ids):
            strategy_id = strategy_ids[cursor.strategy_index]
            page_number = cursor.page

            def _get_page(
                strategy_id: str = strategy_id,
                page_number: int = page_number,
            ) -> Awaitable[Mapping[str, Any]]:
                return self.history_client.get_strategy_hits(
                    strategy_id=strategy_id,
                    limit=_HISTORY_PAGE_SIZE,
                    page=page_number,
                )

            payload = await self._provider_call(
                _get_page,
                budget=budget,
            )
            page = parse_opennews_strategy_hits(payload)
            if page.page != page_number:
                raise OpenNewsHistoryError("opennews_history_payload_invalid")
            for index, event in enumerate(page.events[cursor.event_index :], start=cursor.event_index):
                published = event.entry.published_at_ms
                if published is not None and from_ms <= int(published) < to_ms:
                    raw_params = _raw_params_from_history(payload, event.provider_record_id)
                    if raw_params is not None:
                        budget.before_publish()
                        stamp = now_ms()
                        message = BusMessage(
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
                        try:
                            await asyncio.wait_for(self.bus.publish(message), timeout=budget.checkpoint())
                        except TimeoutError:
                            raise _RecoveryBudgetExhausted("wall_time") from None
                        budget.published()
                        cursor.recovered_count += 1
                cursor.event_index = index + 1

            oldest = min(
                (int(event.entry.published_at_ms) for event in page.events if event.entry.published_at_ms is not None),
                default=None,
            )
            strategy_complete = oldest is not None and oldest <= from_ms
            if not page.has_more:
                strategy_complete = strategy_complete or page.total == 0
                cursor.complete = cursor.complete and strategy_complete
            if strategy_complete or not page.has_more:
                cursor.strategy_index += 1
                cursor.page = 1
                cursor.event_index = 0
            else:
                cursor.page += 1
                cursor.event_index = 0
        return cursor.complete, cursor.recovered_count

    async def _provider_call[T](self, call: Callable[[], Awaitable[T]], *, budget: _TurnBudget) -> T:
        started = time.perf_counter()
        timeout = budget.provider_timeout()
        try:
            result = await asyncio.wait_for(call(), timeout=timeout)
        except TimeoutError:
            raise _RecoveryBudgetExhausted("wall_time") from None
        except Exception:
            if self.telemetry is not None:
                self.telemetry.record_external_data_provider_call(
                    "opennews_recovery",
                    "opennews",
                    "error",
                    time.perf_counter() - started,
                )
            raise
        if self.telemetry is not None:
            self.telemetry.record_external_data_provider_call(
                "opennews_recovery",
                "opennews",
                "success",
                time.perf_counter() - started,
            )
        return result


def _raw_params_from_history(payload: Mapping[str, Any], provider_record_id: str) -> dict[str, Any] | None:
    for value in payload.get("data") or []:
        if isinstance(value, Mapping) and str(value.get("id")) == provider_record_id:
            return dict(value)
    return None
