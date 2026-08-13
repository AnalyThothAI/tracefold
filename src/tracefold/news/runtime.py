from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceCapability,
    ResourceOperationOverrun,
)

from .models import (
    NewsBriefPublisher,
    NewsBriefStory,
    NewsBriefSynthesisResult,
    NewsFeedExpectedError,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
)
from .opennews import (
    OpenNewsEvent,
    OpenNewsExpectedError,
    OpenNewsHistoryError,
    OpenNewsStrategyHistory,
    parse_opennews_message,
    parse_opennews_strategy_hits,
    parse_opennews_strategy_list,
)

_BRIEF_MODEL_TIMEOUT_SECONDS = 60.0
_BRIEF_PREPARE_TIMEOUT_SECONDS = 7.0
_OPENNEWS_BUFFER_CAPACITY = 256
_OPENNEWS_RECONNECT_SECONDS = 3.0
_OPENNEWS_PUBLISH_RETRY_SECONDS = 0.250
_OPENNEWS_HISTORY_PAGE_SIZE = 100
_OPENNEWS_HISTORY_PAGE_CAP = 100
_OPENNEWS_RECOVERY_OVERLAP_MS = 30_000
_RSS_CLAIM_LEASE_SECONDS = 60
_RSS_FETCH_TIMEOUT_SECONDS = 25.0


class _BriefClaimLost(RuntimeError):
    pass


class _BriefStartAdmissionTimeout(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _OpenNewsStatusObservation:
    connected: bool
    error_code: str | None
    coverage_gap: bool = False
    planned: bool = False
    close_code: int | None = None


class NewsAcquisition:
    """Allowlisted OpenNews Strategy ingest plus bounded public RSS corroboration."""

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        rss_sources: Sequence[NewsSourceDefinition],
        rss_feed_reader: NewsFeedReader,
        rss_feed_parser: Callable[..., NewsFeedFetch],
        opennews_source: NewsSourceDefinition,
        opennews_strategy_ids: Sequence[str],
        opennews_ws_client: Any | None = None,
        opennews_history_client: OpenNewsStrategyHistory | None = None,
        story_dirty: asyncio.Event | None = None,
    ) -> None:
        self.db = db
        self.finite_operations = finite_operations
        self.rss_sources = tuple(rss_sources)
        self.rss_feed_reader = rss_feed_reader
        self.rss_feed_parser = rss_feed_parser
        self.opennews_source = opennews_source
        normalized_strategy_ids = tuple(
            value.strip() if isinstance(value, str) else "" for value in opennews_strategy_ids
        )
        if (
            any(not isinstance(value, str) for value in opennews_strategy_ids)
            or any(not value for value in normalized_strategy_ids)
            or len(set(normalized_strategy_ids)) != len(normalized_strategy_ids)
        ):
            raise ValueError("opennews_strategy_ids_invalid")
        self.opennews_strategy_ids = frozenset(normalized_strategy_ids)
        self.opennews_ws_client = opennews_ws_client
        self.opennews_history_client = opennews_history_client
        self.story_dirty = story_dirty
        if any(source.source_kind != "rss" for source in self.rss_sources):
            raise ValueError("news_rss_source_kind_invalid")
        if len({source.source_id for source in self.rss_sources}) != len(self.rss_sources):
            raise ValueError("news_rss_source_ids_must_be_unique")
        if self.opennews_source.source_kind != "opennews":
            raise ValueError("opennews_source_required")
        if "" in self.opennews_strategy_ids or (self.opennews_ws_client is not None and not self.opennews_strategy_ids):
            raise ValueError("opennews_strategy_ids_required")
        self._rss_sources_by_id = {source.source_id: source for source in self.rss_sources}
        self._opennews_queue: asyncio.Queue[OpenNewsEvent] = asyncio.Queue(maxsize=_OPENNEWS_BUFFER_CAPACITY)
        self._opennews_pending_batch: tuple[OpenNewsEvent, ...] = ()
        self._opennews_pending_observed_at_ms: int | None = None
        self._opennews_buffer_overflow_active = False
        self._opennews_status_queue: asyncio.Queue[_OpenNewsStatusObservation] = asyncio.Queue()
        self._opennews_intake_done = asyncio.Event()
        self._opennews_recovery_requested = asyncio.Event()

    @property
    def opennews_enabled(self) -> bool:
        return self.opennews_ws_client is not None

    async def reconcile(self) -> None:
        await self.db.run_business(
            "news_source_reconcile",
            self._reconcile_sync,
            operation_timeout_seconds=3.0,
        )
        if not self.opennews_enabled:
            await self._update_opennews_status(
                connected=False,
                error_code="opennews_token_missing",
            )

    async def turn(self) -> bool | None:
        """Expire bounded Article facts, then process at most one due RSS source."""

        now_ms = _now_ms()
        claim_token = uuid4().hex
        try:
            claim = await self.db.run_business(
                "news_rss_claim",
                self._claim_rss_sync,
                now_ms,
                claim_token,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            return None
        if claim is None:
            return False

        source_id = str(claim["source_id"])
        source = self._rss_sources_by_id.get(source_id)
        if source is None:
            raise RuntimeError("news_rss_claim_source_not_in_catalog")
        try:
            fetch = await self.finite_operations.run(
                "news_rss_fetch",
                self._fetch_rss_sync,
                source,
                claim.get("etag"),
                claim.get("last_modified"),
                timeout_seconds=_RSS_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except ResourceAdmissionTimeout:
            recorded = await self._record_rss_failure(
                source_id=source_id,
                claim_token=claim_token,
                error_code="news_rss_admission_timeout",
                status_code=None,
            )
            return True if recorded else None
        except ResourceOperationOverrun:
            recorded = await self._record_rss_failure(
                source_id=source_id,
                claim_token=claim_token,
                error_code="news_rss_total_timeout",
                status_code=None,
            )
            return True if recorded else None
        except NewsFeedExpectedError as exc:
            recorded = await self._record_rss_failure(
                source_id=source_id,
                claim_token=claim_token,
                error_code=exc.code,
                status_code=cast(int | None, getattr(exc, "status_code", None)),
            )
            return True if recorded else None

        try:
            published = await self.db.run_business(
                "news_rss_publish",
                self._record_rss_fetch_sync,
                source,
                claim_token,
                fetch,
                _now_ms(),
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            return None
        if published is not None and self.story_dirty is not None:
            self.story_dirty.set()
        return True if published is not None else None

    async def run_opennews(self, *, stop_event: asyncio.Event) -> None:
        if self.opennews_ws_client is None:
            await stop_event.wait()
            return
        self._opennews_intake_done.clear()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._opennews_receive_loop(stop_event), name="opennews-receiver")
            group.create_task(self._opennews_publish_loop(stop_event), name="opennews-publisher")
            group.create_task(self._opennews_status_loop(), name="opennews-status")
            if self.opennews_history_client is not None:
                group.create_task(self._opennews_recovery_loop(stop_event), name="opennews-recovery")

    async def _opennews_receive_loop(self, stop_event: asyncio.Event) -> None:
        client = self.opennews_ws_client
        if client is None:
            raise RuntimeError("opennews_websocket_client_missing")
        while not stop_event.is_set():
            try:
                await client.connect()
                self._queue_opennews_status(
                    connected=True,
                    error_code=None,
                )
                self._opennews_buffer_overflow_active = False
                while not stop_event.is_set():
                    message = await _receive_or_stop(
                        client,
                        stop_event=stop_event,
                    )
                    if message is None:
                        break
                    event = parse_opennews_message(
                        message,
                        strategy_ids=self.opennews_strategy_ids,
                    )
                    if event is None:
                        continue
                    try:
                        self._opennews_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        if not self._opennews_buffer_overflow_active:
                            self._queue_opennews_status(
                                connected=True,
                                error_code="opennews_buffer_overflow",
                                coverage_gap=True,
                            )
                            self._opennews_buffer_overflow_active = True
                        continue
                    if self._opennews_buffer_overflow_active:
                        self._queue_opennews_status(
                            connected=True,
                            error_code=None,
                        )
                        self._opennews_buffer_overflow_active = False
            except asyncio.CancelledError:
                raise
            except OpenNewsExpectedError as exc:
                self._queue_opennews_status(
                    connected=False,
                    error_code=exc.code,
                    close_code=exc.status_code,
                )
            finally:
                await client.close()
            if not stop_event.is_set():
                await _wait_or_stop(stop_event, _OPENNEWS_RECONNECT_SECONDS)
        self._queue_opennews_status(
            connected=False,
            error_code=None,
            planned=True,
        )
        self._opennews_intake_done.set()

    async def _opennews_publish_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or not self._opennews_queue.empty() or self._opennews_pending_batch:
            if not self._opennews_pending_batch:
                first = await _queue_get_or_stop(
                    self._opennews_queue,
                    stop_event=stop_event,
                )
                if first is None:
                    continue
                events = [first]
                while len(events) < 100:
                    try:
                        events.append(self._opennews_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                self._opennews_pending_batch = tuple(events)
                self._opennews_pending_observed_at_ms = _now_ms()
            if self._opennews_pending_observed_at_ms is None:
                raise RuntimeError("opennews_pending_observation_clock_missing")
            try:
                outcome = await self.db.run_business(
                    "opennews_live_publish",
                    self._record_opennews_events_sync,
                    self._opennews_pending_batch,
                    self._opennews_pending_observed_at_ms,
                    operation_timeout_seconds=3.0,
                )
            except ResourceAdmissionTimeout:
                await _wait_or_stop(stop_event, _OPENNEWS_PUBLISH_RETRY_SECONDS)
                continue
            except ResourceOperationOverrun as exc:
                if exc.capability is not ResourceCapability.DATABASE_BUSINESS:
                    raise
                await _wait_or_stop(stop_event, _OPENNEWS_PUBLISH_RETRY_SECONDS)
                continue
            if (
                isinstance(outcome, dict)
                and int(outcome.get("items_inserted", 0)) + int(outcome.get("items_updated", 0)) > 0
                and self.story_dirty is not None
            ):
                self.story_dirty.set()
            self._opennews_pending_batch = ()
            self._opennews_pending_observed_at_ms = None

    def _queue_opennews_status(
        self,
        *,
        connected: bool,
        error_code: str | None,
        coverage_gap: bool = False,
        planned: bool = False,
        close_code: int | None = None,
    ) -> None:
        self._opennews_status_queue.put_nowait(
            _OpenNewsStatusObservation(
                connected=bool(connected),
                error_code=error_code,
                coverage_gap=bool(coverage_gap),
                planned=bool(planned),
                close_code=close_code,
            )
        )

    async def _opennews_status_loop(self) -> None:
        pending: _OpenNewsStatusObservation | None = None
        while not self._opennews_intake_done.is_set() or pending is not None or not self._opennews_status_queue.empty():
            if pending is None:
                pending = await _status_get_or_done(
                    self._opennews_status_queue,
                    done=self._opennews_intake_done,
                )
                if pending is None:
                    continue
            try:
                await self._update_opennews_status(
                    connected=pending.connected,
                    error_code=pending.error_code,
                    coverage_gap=pending.coverage_gap,
                    planned=pending.planned,
                    close_code=pending.close_code,
                )
            except ResourceAdmissionTimeout:
                if self._opennews_intake_done.is_set():
                    return
                await _wait_or_stop(self._opennews_intake_done, _OPENNEWS_PUBLISH_RETRY_SECONDS)
                continue
            except ResourceOperationOverrun as exc:
                if exc.capability is not ResourceCapability.DATABASE_BUSINESS:
                    raise
                if self._opennews_intake_done.is_set():
                    return
                await _wait_or_stop(self._opennews_intake_done, _OPENNEWS_PUBLISH_RETRY_SECONDS)
                continue
            except Exception:
                if self._opennews_intake_done.is_set():
                    return
                await _wait_or_stop(self._opennews_intake_done, _OPENNEWS_PUBLISH_RETRY_SECONDS)
                continue
            if pending.connected and not pending.coverage_gap:
                self._opennews_recovery_requested.set()
            pending = None

    async def _opennews_recovery_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            requested = asyncio.create_task(self._opennews_recovery_requested.wait())
            stopped = asyncio.create_task(stop_event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {requested, stopped},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stopped in done:
                    return
            finally:
                requested.cancel()
                stopped.cancel()
                await asyncio.gather(requested, stopped, return_exceptions=True)
            self._opennews_recovery_requested.clear()
            try:
                await self._recover_opennews_incidents()
            except ResourceAdmissionTimeout:
                await _wait_or_stop(stop_event, _OPENNEWS_PUBLISH_RETRY_SECONDS)
                self._opennews_recovery_requested.set()
            except ResourceOperationOverrun as exc:
                if exc.capability is not ResourceCapability.DATABASE_BUSINESS:
                    raise
                await _wait_or_stop(stop_event, _OPENNEWS_PUBLISH_RETRY_SECONDS)
                self._opennews_recovery_requested.set()

    async def _recover_opennews_incidents(self) -> None:
        client = self.opennews_history_client
        if client is None:
            await self._record_opennews_recovery_result(
                incident_id=None,
                status="unavailable",
                recovered_count=0,
                error_code="opennews_history_unavailable",
            )
            return
        try:
            strategy_payload = await client.get_strategy_list(
                limit=_OPENNEWS_HISTORY_PAGE_SIZE,
                page=1,
            )
            strategies = parse_opennews_strategy_list(
                strategy_payload,
                strategy_ids=self.opennews_strategy_ids,
            )
            enabled_ids = {str(strategy["id"]) for strategy in strategies if strategy["enabled"]}
            if enabled_ids != self.opennews_strategy_ids:
                raise OpenNewsHistoryError("opennews_history_strategy_mismatch")
        except OpenNewsHistoryError as exc:
            await self._record_opennews_recovery_result(
                incident_id=None,
                status="unavailable",
                recovered_count=0,
                error_code=exc.code,
            )
            return

        after_incident_id = 0
        while True:
            incident = await self._claim_opennews_recovery(after_incident_id=after_incident_id)
            if incident is None:
                await self._record_opennews_recovery_result(
                    incident_id=None,
                    status="recovered",
                    recovered_count=0,
                    error_code=None,
                )
                return
            incident_id = int(incident["incident_id"])
            after_incident_id = incident_id
            recovered_count = 0
            complete = True
            recovery_from_at_ms = max(
                0,
                int(incident.get("recovery_from_at_ms") or incident["opened_at_ms"]) - _OPENNEWS_RECOVERY_OVERLAP_MS,
            )
            recovery_to_at_ms = int(incident.get("recovery_to_at_ms") or incident.get("closed_at_ms") or _now_ms())
            try:
                for strategy_id in sorted(self.opennews_strategy_ids):
                    strategy_complete, strategy_count = await self._recover_opennews_strategy(
                        client=client,
                        strategy_id=strategy_id,
                        recovery_from_at_ms=recovery_from_at_ms,
                        recovery_to_at_ms=recovery_to_at_ms,
                    )
                    complete = complete and strategy_complete
                    recovered_count += strategy_count
            except OpenNewsHistoryError as exc:
                await self._record_opennews_recovery_result(
                    incident_id=incident_id,
                    status="unavailable",
                    recovered_count=recovered_count,
                    error_code=exc.code,
                )
                continue
            await self._record_opennews_recovery_result(
                incident_id=incident_id,
                status="recovered" if complete else "partial",
                recovered_count=recovered_count,
                error_code=None if complete else "opennews_history_retention_partial",
            )

    async def _recover_opennews_strategy(
        self,
        *,
        client: OpenNewsStrategyHistory,
        strategy_id: str,
        recovery_from_at_ms: int,
        recovery_to_at_ms: int,
    ) -> tuple[bool, int]:
        recovered_count = 0
        reached_boundary = False
        total = 0
        for page_number in range(1, _OPENNEWS_HISTORY_PAGE_CAP + 1):
            payload = await client.get_strategy_hits(
                strategy_id=strategy_id,
                limit=_OPENNEWS_HISTORY_PAGE_SIZE,
                page=page_number,
            )
            page = parse_opennews_strategy_hits(
                payload,
                strategy_ids=self.opennews_strategy_ids,
            )
            total = page.total
            eligible = tuple(
                event
                for event in page.events
                if event.entry.published_at_ms is not None
                and recovery_from_at_ms <= int(event.entry.published_at_ms) < recovery_to_at_ms
            )
            if eligible:
                outcome = await self.db.run_business(
                    "opennews_recovery_publish",
                    self._record_opennews_recovery_events_sync,
                    eligible,
                    _now_ms(),
                    operation_timeout_seconds=3.0,
                )
                recovered_count += int(outcome.get("items_inserted", 0)) + int(outcome.get("items_updated", 0))
                if recovered_count and self.story_dirty is not None:
                    self.story_dirty.set()
            oldest = min(
                (int(event.entry.published_at_ms) for event in page.events if event.entry.published_at_ms is not None),
                default=None,
            )
            if oldest is not None and oldest <= recovery_from_at_ms:
                reached_boundary = True
                break
            if not page.has_more:
                reached_boundary = total == 0
                break
        return reached_boundary, recovered_count

    async def close(self) -> None:
        await self.finite_operations.run(
            "news_rss_reader_close",
            self.rss_feed_reader.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )
        if self.opennews_ws_client is not None:
            await self.opennews_ws_client.close()
        if self.opennews_history_client is not None:
            await self.opennews_history_client.close()

    def _reconcile_sync(self) -> None:
        with self.db.worker_session("news_source_reconcile", 3.0) as repos, repos.transaction():
            repos.news.sync_sources(
                (*self.rss_sources, self.opennews_source),
                now_ms=_now_ms(),
            )

    def _claim_rss_sync(
        self,
        now_ms: int,
        claim_token: str,
    ) -> dict[str, Any] | None:
        with self.db.worker_session("news_rss_claim", 3.0) as repos, repos.transaction():
            repos.news.expire_items(now_ms=now_ms)
            return cast(
                dict[str, Any] | None,
                repos.news.claim_due_rss_source(
                    now_ms=now_ms,
                    claim_token=claim_token,
                    lease_expires_at_ms=now_ms + _RSS_CLAIM_LEASE_SECONDS * 1_000,
                ),
            )

    def _fetch_rss_sync(
        self,
        source: NewsSourceDefinition,
        etag: object,
        last_modified: object,
    ) -> NewsFeedFetch:
        wire = self.rss_feed_reader.fetch_wire(
            source=source,
            etag=str(etag) if etag is not None else None,
            last_modified=str(last_modified) if last_modified is not None else None,
        )
        return self.rss_feed_parser(wire, now_ms=_now_ms())

    def _record_rss_fetch_sync(
        self,
        source: NewsSourceDefinition,
        claim_token: str,
        fetch: NewsFeedFetch,
        finished_at_ms: int,
    ) -> dict[str, int] | None:
        with self.db.worker_session("news_rss_publish", 3.0) as repos, repos.transaction():
            return cast(
                dict[str, int] | None,
                repos.news.record_rss_fetch(
                    source=source,
                    claim_token=claim_token,
                    fetch=fetch,
                    finished_at_ms=finished_at_ms,
                ),
            )

    async def _record_rss_failure(
        self,
        *,
        source_id: str,
        claim_token: str,
        error_code: str,
        status_code: int | None,
    ) -> bool:
        try:
            return bool(
                await self.db.run_business(
                    "news_rss_failure",
                    self._record_rss_failure_sync,
                    source_id,
                    claim_token,
                    _now_ms(),
                    error_code,
                    status_code,
                    operation_timeout_seconds=3.0,
                )
            )
        except ResourceAdmissionTimeout:
            return False

    def _record_rss_failure_sync(
        self,
        source_id: str,
        claim_token: str,
        finished_at_ms: int,
        error_code: str,
        status_code: int | None,
    ) -> bool:
        with self.db.worker_session("news_rss_failure", 3.0) as repos, repos.transaction():
            return bool(
                repos.news.record_rss_failure(
                    source_id=source_id,
                    claim_token=claim_token,
                    finished_at_ms=finished_at_ms,
                    error_code=error_code,
                    status_code=status_code,
                )
            )

    async def _update_opennews_status(
        self,
        *,
        connected: bool,
        error_code: str | None,
        coverage_gap: bool = False,
        planned: bool = False,
        close_code: int | None = None,
    ) -> bool:
        return bool(
            await self.db.run_business(
                "opennews_status",
                self._update_opennews_status_sync,
                self.opennews_source.source_id,
                connected,
                _now_ms(),
                error_code,
                coverage_gap,
                planned,
                close_code,
                operation_timeout_seconds=3.0,
            )
        )

    def _update_opennews_status_sync(
        self,
        source_id: str,
        connected: bool,
        now_ms: int,
        error_code: str | None,
        coverage_gap: bool,
        planned: bool,
        close_code: int | None,
    ) -> bool:
        with self.db.worker_session("opennews_status", 3.0) as repos, repos.transaction():
            return bool(
                repos.news.update_opennews_live_status(
                    source_id=source_id,
                    connected=connected,
                    now_ms=now_ms,
                    error_code=error_code,
                    coverage_gap=coverage_gap,
                    planned=planned,
                    close_code=close_code,
                ),
            )

    async def _claim_opennews_recovery(self, *, after_incident_id: int) -> dict[str, Any] | None:
        return cast(
            dict[str, Any] | None,
            await self.db.run_business(
                "opennews_recovery_claim",
                self._claim_opennews_recovery_sync,
                after_incident_id,
                operation_timeout_seconds=1.0,
            ),
        )

    def _claim_opennews_recovery_sync(
        self,
        after_incident_id: int,
    ) -> dict[str, Any] | None:
        with self.db.worker_session("opennews_recovery_claim", 1.0) as repos, repos.transaction():
            return cast(
                dict[str, Any] | None,
                repos.news.claim_opennews_recovery(
                    source_id=self.opennews_source.source_id,
                    after_incident_id=after_incident_id,
                ),
            )

    async def _record_opennews_recovery_result(
        self,
        *,
        incident_id: int | None,
        status: str,
        recovered_count: int,
        error_code: str | None,
    ) -> None:
        await self.db.run_business(
            "opennews_recovery_result",
            self._record_opennews_recovery_result_sync,
            incident_id,
            status,
            recovered_count,
            error_code,
            _now_ms(),
            operation_timeout_seconds=1.0,
        )

    def _record_opennews_recovery_result_sync(
        self,
        incident_id: int | None,
        status: str,
        recovered_count: int,
        error_code: str | None,
        now_ms: int,
    ) -> None:
        with self.db.worker_session("opennews_recovery_result", 1.0) as repos, repos.transaction():
            repos.news.complete_opennews_recovery(
                source_id=self.opennews_source.source_id,
                incident_id=incident_id,
                status=status,
                recovered_count=recovered_count,
                error_code=error_code,
                now_ms=now_ms,
            )

    def _record_opennews_recovery_events_sync(
        self,
        events: Sequence[OpenNewsEvent],
        observed_at_ms: int,
    ) -> dict[str, int]:
        with self.db.worker_session("opennews_recovery_publish", 3.0) as repos, repos.transaction():
            return cast(
                dict[str, int],
                repos.news.record_opennews_events(
                    source=self.opennews_source,
                    events=events,
                    observed_at_ms=observed_at_ms,
                    ingest_mode="recovery",
                ),
            )

    def _record_opennews_events_sync(
        self,
        events: Sequence[OpenNewsEvent],
        observed_at_ms: int,
    ) -> dict[str, int]:
        with self.db.worker_session("opennews_publish", 3.0) as repos, repos.transaction():
            return cast(
                dict[str, int],
                repos.news.record_opennews_events(
                    source=self.opennews_source,
                    events=events,
                    observed_at_ms=observed_at_ms,
                    ingest_mode="live",
                ),
            )


class NewsBriefCandidate:
    """The one serial-model consumer of the frozen public selection snapshot."""

    def __init__(
        self,
        *,
        db: Any,
        model_adapter: Any,
        publisher: NewsBriefPublisher,
        runtime_id: str,
        stable_order: int = 20,
    ) -> None:
        self.db = db
        self.model_adapter = model_adapter
        self.publisher = publisher
        self.lease_owner = f"news_brief:{runtime_id}"
        self.stable_order = stable_order

    async def peek(self, *, now_ms: int) -> ModelCandidate | None:
        row = await self.db.run_business(
            "news_brief_peek",
            self._peek_sync,
            now_ms,
            operation_timeout_seconds=0.5,
        )
        if row is None:
            return None
        return ModelCandidate(
            kind="news_brief",
            target_key=str(row["slot_at_ms"]),
            due_at_ms=int(row["next_due_at_ms"]),
            stable_order=self.stable_order,
        )

    async def execute(self, candidate: ModelCandidate) -> bool:
        try:
            prepared = await self.db.run_business(
                "news_brief_prepare",
                self._prepare_sync,
                candidate.target_key,
                operation_timeout_seconds=_BRIEF_PREPARE_TIMEOUT_SECONDS,
            )
        except ResourceAdmissionTimeout:
            return False
        if prepared is None:
            return False
        if bool(prepared["completed_without_model"]):
            return True

        claim = dict(prepared["claim"])
        model_submitted = False

        def mark_model_submitted() -> None:
            nonlocal model_submitted
            model_submitted = True

        async def start_attempt_before_submit() -> None:
            start_submitted = False

            def mark_start_submitted() -> None:
                nonlocal start_submitted
                start_submitted = True

            try:
                started = await self.db.run_business(
                    "news_brief_start_model",
                    self._start_model_sync,
                    claim,
                    operation_timeout_seconds=0.5,
                    on_submitted=mark_start_submitted,
                )
            except ResourceAdmissionTimeout as exc:
                if start_submitted:
                    raise RuntimeError("news_brief_start_model_outcome_unknown") from exc
                raise _BriefStartAdmissionTimeout from exc
            if not started:
                raise _BriefClaimLost

        try:
            generated = await self.model_adapter.run(
                "news_brief_inference",
                self._generate_sync,
                prepared["stories"],
                timeout_seconds=_BRIEF_MODEL_TIMEOUT_SECONDS,
                before_submit=start_attempt_before_submit,
                on_submitted=mark_model_submitted,
            )
        except asyncio.CancelledError:
            if not model_submitted:
                await asyncio.shield(self._release_prework(claim))
            raise
        except ResourceAdmissionTimeout:
            if model_submitted:
                raise
            await self._release_prework(claim)
            return False
        except _BriefStartAdmissionTimeout:
            await self._release_prework(claim)
            return False
        except _BriefClaimLost:
            return False

        publish_submitted = False

        def mark_publish_submitted() -> None:
            nonlocal publish_submitted
            publish_submitted = True

        try:
            published = await self.db.run_business(
                "news_brief_publish",
                self._publish_sync,
                prepared,
                generated,
                operation_timeout_seconds=3.0,
                on_submitted=mark_publish_submitted,
            )
        except ResourceAdmissionTimeout:
            if publish_submitted:
                raise
            return False
        return published is not None

    async def _release_prework(self, claim: dict[str, Any]) -> bool:
        try:
            released = await self.db.run_business(
                "news_brief_release_prework",
                self._release_prework_sync,
                claim,
                operation_timeout_seconds=0.5,
            )
        except ResourceAdmissionTimeout:
            return False
        return bool(released)

    async def close(self) -> None:
        await self.model_adapter.run(
            "news_brief_client_close",
            self.publisher.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )

    def _peek_sync(self, now_ms: int) -> dict[str, Any] | None:
        with self.db.worker_session("news_brief_peek", 0.5) as repos, repos.transaction():
            return cast(dict[str, Any] | None, repos.news.peek_brief_candidate(now_ms=now_ms))

    def _prepare_sync(self, slot_at_ms: str) -> dict[str, Any] | None:
        now_ms = _now_ms()
        with self.db.worker_session("news_brief_prepare", 3.0) as repos, repos.transaction():
            prepared = repos.news.prepare_brief_run(
                slot_at_ms=int(slot_at_ms),
                lease_owner=self.lease_owner,
                lease_token=uuid4().hex,
                now_ms=now_ms,
            )
        if prepared is None or bool(prepared["completed_without_model"]):
            return cast(dict[str, Any] | None, prepared)
        return {
            **dict(prepared),
            "stories": [NewsBriefStory.model_validate(story) for story in prepared["top_stories"]],
        }

    def _start_model_sync(self, claim: dict[str, Any]) -> bool:
        with self.db.worker_session("news_brief_start_model", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.start_brief_model(
                    slot_at_ms=int(claim["slot_at_ms"]),
                    lease_owner=str(claim["lease_owner"]),
                    lease_token=str(claim["lease_token"]),
                    now_ms=_now_ms(),
                )
            )

    def _release_prework_sync(self, claim: dict[str, Any]) -> bool:
        with self.db.worker_session("news_brief_release_prework", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.release_brief_claim(
                    slot_at_ms=int(claim["slot_at_ms"]),
                    lease_owner=str(claim["lease_owner"]),
                    lease_token=str(claim["lease_token"]),
                    now_ms=_now_ms(),
                )
            )

    def _generate_sync(self, stories: list[NewsBriefStory]) -> NewsBriefSynthesisResult:
        return self.publisher.publish(stories)

    def _publish_sync(
        self,
        prepared: dict[str, Any],
        generated: NewsBriefSynthesisResult,
    ) -> str | None:
        with self.db.worker_session("news_brief_publish", 3.0) as repos, repos.transaction():
            return cast(
                str | None,
                repos.news.publish_brief(
                    claim=prepared["claim"],
                    result=generated,
                    now_ms=_now_ms(),
                ),
            )


def _now_ms() -> int:
    return int(time.time() * 1_000)


async def _receive_or_stop(client: Any, *, stop_event: asyncio.Event) -> Any | None:
    receive_task = asyncio.create_task(client.receive())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {receive_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and stop_task.result():
            return None
        return await receive_task
    finally:
        for task in (receive_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive_task, stop_task, return_exceptions=True)


async def _queue_get_or_stop(
    queue: asyncio.Queue[OpenNewsEvent],
    *,
    stop_event: asyncio.Event,
) -> OpenNewsEvent | None:
    if stop_event.is_set() and queue.empty():
        return None
    try:
        return await asyncio.wait_for(queue.get(), timeout=0.250)
    except TimeoutError:
        return None


async def _status_get_or_done(
    queue: asyncio.Queue[_OpenNewsStatusObservation],
    *,
    done: asyncio.Event,
) -> _OpenNewsStatusObservation | None:
    if done.is_set() and queue.empty():
        return None
    get_task = asyncio.create_task(queue.get())
    done_task = asyncio.create_task(done.wait())
    try:
        completed, _pending = await asyncio.wait(
            {get_task, done_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if get_task in completed:
            return get_task.result()
        return None
    finally:
        get_task.cancel()
        done_task.cancel()
        await asyncio.gather(get_task, done_task, return_exceptions=True)


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))
    except TimeoutError:
        return


__all__ = ["NewsAcquisition", "NewsBriefCandidate"]
