from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, cast
from uuid import uuid4

from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from .models import (
    INSIGHTS_SYNTHESIS_PROVIDER,
    NewsBriefPublisher,
    NewsBriefStory,
    NewsBriefSynthesisResult,
    NewsSourceDefinition,
)
from .opennews import OpenNewsEvent, OpenNewsExpectedError, parse_opennews_message

_BRIEF_MODEL_TIMEOUT_SECONDS = 60.0
_BRIEF_PREPARE_TIMEOUT_SECONDS = 7.0
_OPENNEWS_BUFFER_CAPACITY = 256
_OPENNEWS_RECONNECT_SECONDS = 3.0
_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS = 5 * 60
_OPENNEWS_RECOVERY_MAX_PAGES = 11


class NewsAcquisition:
    """One OpenNews live/recovery runtime."""

    def __init__(
        self,
        *,
        db: Any,
        finite_operations: Any,
        opennews_source: NewsSourceDefinition,
        opennews_rest_client: Any | None = None,
        opennews_ws_client: Any | None = None,
    ) -> None:
        self.db = db
        self.finite_operations = finite_operations
        self.opennews_source = opennews_source
        self.opennews_rest_client = opennews_rest_client
        self.opennews_ws_client = opennews_ws_client
        self._opennews_queue: asyncio.Queue[OpenNewsEvent] = asyncio.Queue(maxsize=_OPENNEWS_BUFFER_CAPACITY)
        self._opennews_recovery_requested = asyncio.Event()
        self._opennews_connected = False
        self._opennews_gap_unclosed = True
        self._opennews_last_recovery_attempt_at_ms: int | None = None
        self._opennews_gap_boundary_provider_record_id: str | None = None
        self._opennews_gap_version = 0

    @property
    def opennews_enabled(self) -> bool:
        return self.opennews_rest_client is not None and self.opennews_ws_client is not None

    async def reconcile(self) -> None:
        last_attempt_at_ms, gap_boundary_provider_record_id, gap_version = await self.db.run_business(
            "news_source_reconcile",
            self._reconcile_sync,
            operation_timeout_seconds=3.0,
        )
        self._opennews_last_recovery_attempt_at_ms = int(last_attempt_at_ms) if last_attempt_at_ms is not None else None
        self._opennews_gap_boundary_provider_record_id = (
            str(gap_boundary_provider_record_id) if gap_boundary_provider_record_id is not None else None
        )
        self._opennews_gap_version = int(gap_version)
        if self.opennews_rest_client is None or self.opennews_ws_client is None:
            await self._update_opennews_status(
                connected=False,
                error_code="opennews_token_missing",
                gap_unclosed=True,
            )

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
                self._opennews_connected = False
                self._opennews_gap_unclosed = True
                self._opennews_recovery_requested.clear()
            if not stop_event.is_set():
                await _wait_or_stop(stop_event, _OPENNEWS_RECONNECT_SECONDS)

    async def _opennews_receive_loop(self, stop_event: asyncio.Event) -> None:
        client = self.opennews_ws_client
        if client is None:
            raise RuntimeError("opennews_websocket_client_missing")
        while not stop_event.is_set():
            try:
                await client.connect()
                self._opennews_connected = True
                self._opennews_gap_unclosed = True
                await self._update_opennews_status(
                    connected=True,
                    error_code=None,
                    gap_unclosed=True,
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
                        self._opennews_gap_unclosed = True
                        if self._opennews_gap_boundary_provider_record_id is None:
                            self._opennews_gap_boundary_provider_record_id = event.provider_record_id
                        await self._update_opennews_status(
                            connected=True,
                            error_code="opennews_buffer_overflow",
                            gap_unclosed=True,
                        )
                        self._opennews_recovery_requested.set()
            except asyncio.CancelledError:
                raise
            except OpenNewsExpectedError as exc:
                self._opennews_connected = False
                self._opennews_gap_unclosed = True
                await self._update_opennews_status(
                    connected=False,
                    error_code=exc.code,
                    gap_unclosed=True,
                )
            finally:
                await client.close()
            if not stop_event.is_set():
                await _wait_or_stop(stop_event, _OPENNEWS_RECONNECT_SECONDS)
        self._opennews_connected = False
        await self._update_opennews_status(
            connected=False,
            error_code=None,
            gap_unclosed=self._opennews_gap_unclosed,
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
            try:
                await self.db.run_business(
                    "opennews_live_publish",
                    self._publish_opennews_sync,
                    tuple(events),
                    _now_ms(),
                    None,
                    operation_timeout_seconds=3.0,
                )
            except BaseException:
                self._opennews_gap_unclosed = True
                if self._opennews_gap_boundary_provider_record_id is None:
                    self._opennews_gap_boundary_provider_record_id = events[0].provider_record_id
                raise

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
            self._opennews_last_recovery_attempt_at_ms = started_at_ms
            recovery_gap_version = self._opennews_gap_version
            try:
                await self.db.run_business(
                    "opennews_recovery_start",
                    self._record_opennews_recovery_attempt_sync,
                    started_at_ms,
                    operation_timeout_seconds=3.0,
                )
                recovery_covered_gap = False
                for page in range(1, _OPENNEWS_RECOVERY_MAX_PAGES + 1):
                    try:
                        events = await self.finite_operations.run(
                            "opennews_rest_recovery",
                            client.fetch_page,
                            page,
                            timeout_seconds=25.0,
                        )
                    except ResourceOperationOverrun:
                        raise OpenNewsExpectedError("opennews_rest_timeout") from None
                    await self.db.run_business(
                        "opennews_recovery_publish",
                        self._publish_opennews_sync,
                        tuple(events[:100]),
                        _now_ms(),
                        started_at_ms,
                        operation_timeout_seconds=3.0,
                    )
                    recovery_covered_gap = _opennews_recovery_covers_boundary(
                        events,
                        boundary_provider_record_id=self._opennews_gap_boundary_provider_record_id,
                    )
                    if recovery_covered_gap:
                        break
                if recovery_covered_gap:
                    gap_closed = await self._update_opennews_status(
                        connected=self._opennews_connected,
                        error_code=None,
                        gap_unclosed=False,
                        expected_gap_version=recovery_gap_version,
                    )
                    if gap_closed and self._opennews_gap_version == recovery_gap_version:
                        self._opennews_gap_unclosed = False
                        self._opennews_gap_boundary_provider_record_id = None
                    else:
                        self._opennews_gap_unclosed = True
                        self._opennews_recovery_requested.set()
                else:
                    self._opennews_gap_unclosed = True
                    await self._update_opennews_status(
                        connected=self._opennews_connected,
                        error_code="opennews_recovery_window_incomplete",
                        gap_unclosed=True,
                    )
            except asyncio.CancelledError:
                raise
            except OpenNewsExpectedError as exc:
                self._opennews_gap_unclosed = True
                await self.db.run_business(
                    "opennews_recovery_failure",
                    self._record_opennews_recovery_failure_sync,
                    started_at_ms,
                    _now_ms(),
                    exc,
                    operation_timeout_seconds=3.0,
                )
                await self._update_opennews_status(
                    connected=self._opennews_connected,
                    error_code=exc.code,
                    gap_unclosed=True,
                )
                if exc.code != "opennews_auth_failed":
                    self._opennews_recovery_requested.set()
            except ResourceAdmissionTimeout:
                self._opennews_gap_unclosed = True
                self._opennews_recovery_requested.set()

    async def close(self) -> None:
        if self.opennews_ws_client is not None:
            await self.opennews_ws_client.close()
        if self.opennews_rest_client is not None:
            await self.finite_operations.run(
                "opennews_rest_client_close",
                self.opennews_rest_client.close,
                timeout_seconds=5.0,
                allow_shutdown=True,
            )

    def _reconcile_sync(self) -> tuple[int | None, str | None, int]:
        with self.db.worker_session("news_source_reconcile", 3.0) as repos, repos.transaction():
            repos.news.sync_source(self.opennews_source, now_ms=_now_ms())
            return cast(
                tuple[int | None, str | None, int],
                repos.news.opennews_recovery_state(
                    source_id=self.opennews_source.source_id,
                ),
            )

    async def _update_opennews_status(
        self,
        *,
        connected: bool,
        error_code: str | None,
        gap_unclosed: bool,
        expected_gap_version: int | None = None,
    ) -> bool:
        if not gap_unclosed and expected_gap_version is None:
            expected_gap_version = self._opennews_gap_version
        state = await self.db.run_business(
            "opennews_status",
            self._update_opennews_status_sync,
            self.opennews_source.source_id,
            connected,
            _now_ms(),
            error_code,
            gap_unclosed,
            self._opennews_gap_boundary_provider_record_id,
            expected_gap_version,
            operation_timeout_seconds=3.0,
        )
        if state is None:
            return False
        boundary_provider_record_id, gap_version = state
        self._opennews_gap_version = max(self._opennews_gap_version, int(gap_version))
        if gap_unclosed:
            self._opennews_gap_unclosed = True
            if isinstance(boundary_provider_record_id, str):
                self._opennews_gap_boundary_provider_record_id = boundary_provider_record_id
        return True

    def _update_opennews_status_sync(
        self,
        source_id: str,
        connected: bool,
        now_ms: int,
        error_code: str | None,
        gap_unclosed: bool,
        gap_boundary_provider_record_id: str | None,
        expected_gap_version: int | None,
    ) -> tuple[str | None, int] | None:
        with self.db.worker_session("opennews_status", 3.0) as repos, repos.transaction():
            return cast(
                tuple[str | None, int] | None,
                repos.news.update_opennews_live_status(
                    source_id=source_id,
                    connected=connected,
                    now_ms=now_ms,
                    error_code=error_code,
                    gap_unclosed=gap_unclosed,
                    gap_boundary_provider_record_id=gap_boundary_provider_record_id,
                    expected_gap_version=expected_gap_version,
                ),
            )

    def _publish_opennews_sync(
        self,
        events: Sequence[OpenNewsEvent],
        observed_at_ms: int,
        recovery_started_at_ms: int | None,
    ) -> dict[str, int]:
        with self.db.worker_session("opennews_publish", 3.0) as repos, repos.transaction():
            return cast(
                dict[str, int],
                repos.news.record_opennews_events(
                    source=self.opennews_source,
                    events=events,
                    observed_at_ms=observed_at_ms,
                    recovery_started_at_ms=recovery_started_at_ms,
                ),
            )

    def _record_opennews_recovery_attempt_sync(self, started_at_ms: int) -> None:
        with self.db.worker_session("opennews_recovery_start", 3.0) as repos, repos.transaction():
            repos.news.mark_opennews_recovery_attempt(
                source_id=self.opennews_source.source_id,
                started_at_ms=started_at_ms,
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
            target_key=str(row["target_fingerprint"]),
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
        claim_lost = False

        def mark_model_submitted() -> None:
            nonlocal model_submitted
            model_submitted = True

        async def start_attempt_after_submit() -> None:
            nonlocal claim_lost
            started = await self.db.run_business(
                "news_brief_start_model",
                self._start_model_sync,
                claim,
                operation_timeout_seconds=0.5,
            )
            if not started:
                claim_lost = True

        try:
            generated = await self.model_adapter.run(
                "news_brief_inference",
                self._generate_sync,
                prepared["stories"],
                timeout_seconds=_BRIEF_MODEL_TIMEOUT_SECONDS,
                after_submit=start_attempt_after_submit,
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
        except ResourceOperationOverrun:
            failed = await self.db.run_business(
                "news_brief_publish_failure",
                self._fail_sync,
                claim,
                operation_timeout_seconds=3.0,
            )
            return failed is not None
        if claim_lost:
            return False

        try:
            published = await self.db.run_business(
                "news_brief_publish",
                self._publish_sync,
                prepared,
                generated,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
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

    def _prepare_sync(self, target_fingerprint_value: str) -> dict[str, Any] | None:
        now_ms = _now_ms()
        with self.db.worker_session("news_brief_prepare", 3.0) as repos, repos.transaction():
            prepared = repos.news.prepare_brief_run(
                target_fingerprint=target_fingerprint_value,
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
                    run_id=str(claim["run_id"]),
                    lease_owner=str(claim["lease_owner"]),
                    lease_token=str(claim["lease_token"]),
                    now_ms=_now_ms(),
                )
            )

    def _release_prework_sync(self, claim: dict[str, Any]) -> bool:
        with self.db.worker_session("news_brief_release_prework", 0.5) as repos, repos.transaction():
            return bool(
                repos.news.release_brief_claim(
                    run_id=str(claim["run_id"]),
                    lease_owner=str(claim["lease_owner"]),
                    lease_token=str(claim["lease_token"]),
                    due_at_ms=int(claim["release_due_at_ms"]),
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
                    selection=prepared["selection"],
                    result=generated,
                    now_ms=_now_ms(),
                ),
            )

    def _fail_sync(self, claim: dict[str, Any]) -> str | None:
        with self.db.worker_session("news_brief_publish_failure", 3.0) as repos, repos.transaction():
            return cast(
                str | None,
                repos.news.fail_brief_run(
                    claim=claim,
                    error_code=INSIGHTS_SYNTHESIS_PROVIDER,
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


def _opennews_recovery_covers_boundary(
    events: Sequence[OpenNewsEvent],
    *,
    boundary_provider_record_id: str | None,
) -> bool:
    report_ids = [
        event.provider_record_id
        for event in events
        if event.observation_kind == "report" and event.entry is not None and event.entry.published_at_ms is not None
    ]
    if not report_ids:
        return False
    if boundary_provider_record_id is None:
        return True
    return boundary_provider_record_id in report_ids


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
