from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Any, cast
from uuid import uuid4

from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from .models import (
    NewsBriefPublisher,
    NewsBriefStory,
    NewsBriefSynthesisResult,
    NewsFeedExpectedError,
    NewsFeedFetch,
    NewsFeedReader,
    NewsSourceDefinition,
)
from .opennews import OpenNewsEvent, OpenNewsExpectedError, parse_opennews_message

_BRIEF_MODEL_TIMEOUT_SECONDS = 60.0
_BRIEF_PREPARE_TIMEOUT_SECONDS = 7.0
_OPENNEWS_BUFFER_CAPACITY = 256
_OPENNEWS_RECONNECT_SECONDS = 3.0
_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS = 5 * 60
_OPENNEWS_RECOVERY_MAX_PAGES = 11
_RSS_CLAIM_LEASE_SECONDS = 60
_RSS_FETCH_TIMEOUT_SECONDS = 25.0


class _BriefClaimLost(RuntimeError):
    pass


class _BriefStartAdmissionTimeout(RuntimeError):
    pass


class NewsAcquisition:
    """Primary OpenNews live/overlap ingest plus bounded public RSS corroboration."""

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        rss_sources: Sequence[NewsSourceDefinition],
        rss_feed_reader: NewsFeedReader,
        rss_feed_parser: Callable[..., NewsFeedFetch],
        opennews_source: NewsSourceDefinition,
        opennews_rest_client: Any | None = None,
        opennews_ws_client: Any | None = None,
    ) -> None:
        self.db = db
        self.finite_operations = finite_operations
        self.rss_sources = tuple(rss_sources)
        self.rss_feed_reader = rss_feed_reader
        self.rss_feed_parser = rss_feed_parser
        self.opennews_source = opennews_source
        self.opennews_rest_client = opennews_rest_client
        self.opennews_ws_client = opennews_ws_client
        if any(source.source_kind != "rss" for source in self.rss_sources):
            raise ValueError("news_rss_source_kind_invalid")
        if len({source.source_id for source in self.rss_sources}) != len(self.rss_sources):
            raise ValueError("news_rss_source_ids_must_be_unique")
        if self.opennews_source.source_kind != "opennews":
            raise ValueError("opennews_source_required")
        if (self.opennews_rest_client is None) is not (self.opennews_ws_client is None):
            raise ValueError("opennews_clients_must_be_paired")
        self._rss_sources_by_id = {source.source_id: source for source in self.rss_sources}
        self._opennews_queue: asyncio.Queue[OpenNewsEvent] = asyncio.Queue(maxsize=_OPENNEWS_BUFFER_CAPACITY)
        self._opennews_recovery_requested = asyncio.Event()
        self._opennews_last_recovery_attempt_at_ms: int | None = None

    @property
    def opennews_enabled(self) -> bool:
        return self.opennews_rest_client is not None and self.opennews_ws_client is not None

    async def reconcile(self) -> None:
        last_attempt_at_ms = await self.db.run_business(
            "news_source_reconcile",
            self._reconcile_sync,
            operation_timeout_seconds=3.0,
        )
        self._opennews_last_recovery_attempt_at_ms = int(last_attempt_at_ms) if last_attempt_at_ms is not None else None
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
        return True if published is not None else None

    async def run_opennews(self, *, stop_event: asyncio.Event) -> None:
        if self.opennews_rest_client is None or self.opennews_ws_client is None:
            await stop_event.wait()
            return
        while not stop_event.is_set():
            try:
                async with asyncio.TaskGroup() as group:
                    group.create_task(self._opennews_receive_loop(stop_event), name="opennews-receiver")
                    group.create_task(self._opennews_publish_loop(stop_event), name="opennews-publisher")
                    group.create_task(self._opennews_recovery_loop(stop_event), name="opennews-recovery")
            except* ResourceAdmissionTimeout:
                pass
            if not stop_event.is_set():
                await _wait_or_stop(stop_event, _OPENNEWS_RECONNECT_SECONDS)

    async def _opennews_receive_loop(self, stop_event: asyncio.Event) -> None:
        client = self.opennews_ws_client
        if client is None:
            raise RuntimeError("opennews_websocket_client_missing")
        while not stop_event.is_set():
            try:
                await client.connect()
                await self._update_opennews_status(
                    connected=True,
                    error_code=None,
                )
                self._opennews_recovery_requested.set()
                while not stop_event.is_set():
                    message = await _receive_or_stop(
                        client,
                        stop_event=stop_event,
                    )
                    if message is None:
                        break
                    event = parse_opennews_message(message)
                    if event is None:
                        continue
                    try:
                        self._opennews_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        raise OpenNewsExpectedError("opennews_buffer_overflow") from None
            except asyncio.CancelledError:
                raise
            except OpenNewsExpectedError as exc:
                await self._update_opennews_status(
                    connected=False,
                    error_code=exc.code,
                )
            finally:
                await client.close()
            if not stop_event.is_set():
                await _wait_or_stop(stop_event, _OPENNEWS_RECONNECT_SECONDS)
        await self._update_opennews_status(
            connected=False,
            error_code=None,
        )

    async def _opennews_publish_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() or not self._opennews_queue.empty():
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
            await self.db.run_business(
                "opennews_live_publish",
                self._record_opennews_events_sync,
                tuple(events),
                _now_ms(),
                operation_timeout_seconds=3.0,
            )

    async def _opennews_recovery_loop(self, stop_event: asyncio.Event) -> None:
        client = self.opennews_rest_client
        if client is None:
            raise RuntimeError("opennews_rest_client_missing")
        while not stop_event.is_set():
            await _event_or_stop(
                self._opennews_recovery_requested,
                stop_event=stop_event,
            )
            if stop_event.is_set():
                return
            recovery_delay_seconds = _opennews_recovery_delay_seconds(
                last_attempt_at_ms=self._opennews_last_recovery_attempt_at_ms,
                now_ms=_now_ms(),
            )
            if recovery_delay_seconds > 0:
                await _wait_or_stop(stop_event, recovery_delay_seconds)
                if stop_event.is_set():
                    return
            self._opennews_recovery_requested.clear()
            started_at_ms = _now_ms()
            try:
                await self.db.run_business(
                    "opennews_recovery_start",
                    self._record_opennews_recovery_attempt_sync,
                    started_at_ms,
                    operation_timeout_seconds=3.0,
                )
                self._opennews_last_recovery_attempt_at_ms = started_at_ms
                items_seen = 0
                items_accepted = 0
                rejected = 0
                overlap_complete = False
                for page in range(1, _OPENNEWS_RECOVERY_MAX_PAGES + 1):
                    try:
                        overlap_page = await self.finite_operations.run(
                            "opennews_rest_recovery",
                            client.fetch_overlap_page,
                            page,
                            timeout_seconds=25.0,
                        )
                    except ResourceOperationOverrun:
                        raise OpenNewsExpectedError("opennews_rest_timeout") from None
                    outcome = await self.db.run_business(
                        "opennews_recovery_publish",
                        self._record_opennews_recovery_page_sync,
                        overlap_page.events,
                        started_at_ms,
                        operation_timeout_seconds=3.0,
                    )
                    items_seen += int(outcome["events_seen"])
                    items_accepted += (
                        int(outcome["items_inserted"])
                        + int(outcome["items_updated"])
                        + int(outcome["metadata_updated"])
                    )
                    rejected += int(outcome["rejected"])
                    overlap_complete = bool(outcome["overlap_complete"]) or overlap_page.is_last_page
                    if overlap_complete:
                        break
                completed = await self.db.run_business(
                    "opennews_recovery_complete",
                    self._complete_opennews_recovery_sync,
                    started_at_ms,
                    _now_ms(),
                    not overlap_complete,
                    items_seen,
                    items_accepted,
                    {"rejected": rejected},
                    operation_timeout_seconds=3.0,
                )
                if not completed:
                    raise RuntimeError("opennews_recovery_completion_lost")
            except asyncio.CancelledError:
                raise
            except OpenNewsExpectedError as exc:
                await self.db.run_business(
                    "opennews_recovery_failure",
                    self._record_opennews_recovery_failure_sync,
                    started_at_ms,
                    _now_ms(),
                    exc,
                    operation_timeout_seconds=3.0,
                )
                if exc.code != "opennews_auth_failed":
                    self._opennews_recovery_requested.set()
            except ResourceAdmissionTimeout:
                self._opennews_recovery_requested.set()

    async def close(self) -> None:
        await self.finite_operations.run(
            "news_rss_reader_close",
            self.rss_feed_reader.close,
            timeout_seconds=5.0,
            allow_shutdown=True,
        )
        if self.opennews_ws_client is not None:
            await self.opennews_ws_client.close()
        if self.opennews_rest_client is not None:
            await self.finite_operations.run(
                "opennews_rest_client_close",
                self.opennews_rest_client.close,
                timeout_seconds=5.0,
                allow_shutdown=True,
            )

    def _reconcile_sync(self) -> int | None:
        with self.db.worker_session("news_source_reconcile", 3.0) as repos, repos.transaction():
            repos.news.sync_sources(
                (*self.rss_sources, self.opennews_source),
                now_ms=_now_ms(),
            )
            return cast(
                int | None,
                repos.news.opennews_last_recovery_attempt(source_id=self.opennews_source.source_id),
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
    ) -> bool:
        return bool(
            await self.db.run_business(
                "opennews_status",
                self._update_opennews_status_sync,
                self.opennews_source.source_id,
                connected,
                _now_ms(),
                error_code,
                operation_timeout_seconds=3.0,
            )
        )

    def _update_opennews_status_sync(
        self,
        source_id: str,
        connected: bool,
        now_ms: int,
        error_code: str | None,
    ) -> bool:
        with self.db.worker_session("opennews_status", 3.0) as repos, repos.transaction():
            return bool(
                repos.news.update_opennews_live_status(
                    source_id=source_id,
                    connected=connected,
                    now_ms=now_ms,
                    error_code=error_code,
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
                ),
            )

    def _record_opennews_recovery_page_sync(
        self,
        events: Sequence[OpenNewsEvent],
        recovery_started_at_ms: int,
    ) -> dict[str, Any]:
        with self.db.worker_session("opennews_recovery_publish", 3.0) as repos, repos.transaction():
            return cast(
                dict[str, Any],
                repos.news.record_opennews_recovery_page(
                    source=self.opennews_source,
                    events=events,
                    observed_at_ms=recovery_started_at_ms,
                    recovery_started_at_ms=recovery_started_at_ms,
                ),
            )

    def _record_opennews_recovery_attempt_sync(self, started_at_ms: int) -> None:
        with self.db.worker_session("opennews_recovery_start", 3.0) as repos, repos.transaction():
            repos.news.mark_opennews_recovery_attempt(
                source_id=self.opennews_source.source_id,
                started_at_ms=started_at_ms,
            )

    def _complete_opennews_recovery_sync(
        self,
        started_at_ms: int,
        finished_at_ms: int,
        window_exhausted: bool,
        items_seen: int,
        items_accepted: int,
        rejection_counts: dict[str, int],
    ) -> bool:
        with self.db.worker_session("opennews_recovery_complete", 3.0) as repos, repos.transaction():
            return bool(
                repos.news.complete_opennews_recovery(
                    source_id=self.opennews_source.source_id,
                    started_at_ms=started_at_ms,
                    finished_at_ms=finished_at_ms,
                    window_exhausted=window_exhausted,
                    items_seen=items_seen,
                    items_accepted=items_accepted,
                    rejection_counts=rejection_counts,
                )
            )

    def _record_opennews_recovery_failure_sync(
        self,
        started_at_ms: int,
        finished_at_ms: int,
        error: OpenNewsExpectedError,
    ) -> None:
        with self.db.worker_session("opennews_recovery_failure", 3.0) as repos, repos.transaction():
            repos.news.record_opennews_recovery_failure(
                source_id=self.opennews_source.source_id,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
                error_code=error.code,
                status_code=error.status_code,
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


def _opennews_recovery_delay_seconds(
    *,
    last_attempt_at_ms: int | None,
    now_ms: int,
) -> float:
    if last_attempt_at_ms is None:
        return 0.0
    next_attempt_at_ms = int(last_attempt_at_ms) + int(_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS * 1_000)
    return max(0.0, (next_attempt_at_ms - int(now_ms)) / 1_000)


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


async def _event_or_stop(
    event: asyncio.Event,
    *,
    stop_event: asyncio.Event,
) -> None:
    event_task = asyncio.create_task(event.wait())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait(
            {event_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (event_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(event_task, stop_task, return_exceptions=True)


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))
    except TimeoutError:
        return


__all__ = ["NewsAcquisition", "NewsBriefCandidate"]
