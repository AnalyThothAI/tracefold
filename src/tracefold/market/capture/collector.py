from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger

from tracefold.market.capture.normalizer import normalize_gmgn_payload, parse_gmgn_frame
from tracefold.market.capture.provider_contracts import (
    GmgnStreamExpectedError,
    IngestStoreProtocol,
    UpstreamClientProtocol,
)
from tracefold.platform.resource import (
    ResourceAdmissionTimeout,
    ResourceCapability,
    ResourceOperationOverrun,
)

_GMGN_FRAME_MAX_BYTES = 1 * 1024 * 1024
_GMGN_FRAME_MAX_ITEMS = 500
_GMGN_PENDING_SNAPSHOT_LIMIT = 256


@dataclass(slots=True)
class CollectorStatus:
    started_at_ms: int
    last_frame_at_ms: int | None = None
    last_event_at_ms: int | None = None
    frames_received: int = 0
    twitter_events: int = 0
    duplicate_twitter_events: int = 0
    parse_errors: int = 0
    snapshot_gate_outcomes: dict[str, int] = field(
        default_factory=lambda: {
            "immediate_complete": 0,
            "debounced_complete": 0,
            "debounced_timeout": 0,
            "non_tw_channel": 0,
        }
    )

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)


class CollectorService:
    def __init__(
        self,
        *,
        store: IngestStoreProtocol,
        upstream_client: UpstreamClientProtocol | None,
        db: Any,
    ):
        self.store = store
        self.upstream_client = upstream_client
        self.db = db
        self.snapshot_timeout = 0.5
        self._pending_snapshots: dict[str, asyncio.Task[None]] = {}
        self._snapshot_task_group: asyncio.TaskGroup | None = None
        self._upstream_task: asyncio.Task[None] | None = None
        self._event_publish_lock = asyncio.Lock()
        self.status = CollectorStatus(started_at_ms=_now_ms())

    def source_is_streaming(self) -> bool:
        client = self.upstream_client
        if client is None:
            return False
        return str(client.connection_state_payload().get("state") or "") == "streaming"

    async def run(self, *, stop_event: asyncio.Event) -> None:
        if self.upstream_client is None:
            raise RuntimeError("upstream_client is required")
        async with asyncio.TaskGroup() as snapshot_task_group:
            self._snapshot_task_group = snapshot_task_group
            self._upstream_task = asyncio.create_task(
                self.upstream_client.run(),
                name="gmgn-stream",
            )
            stop_task = asyncio.create_task(stop_event.wait(), name="gmgn-stop-wait")
            graceful_stop = False
            try:
                done, _ = await asyncio.wait(
                    {self._upstream_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._upstream_task in done:
                    await self._upstream_task
                    if not stop_event.is_set():
                        raise RuntimeError("gmgn_stream_returned")
                else:
                    graceful_stop = True
                    self._upstream_task.cancel()
                    await asyncio.gather(self._upstream_task, return_exceptions=True)
            finally:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                if self._upstream_task is not None and not self._upstream_task.done():
                    self._upstream_task.cancel()
                    await asyncio.gather(self._upstream_task, return_exceptions=True)
                self._upstream_task = None
                if graceful_stop:
                    await self._flush_pending_snapshots()
                else:
                    await self._clear_pending_snapshots()
                self._snapshot_task_group = None

    async def close(self) -> None:
        if self.upstream_client is None:
            return
        try:
            aclose = self.upstream_client.aclose
        except AttributeError as exc:
            raise RuntimeError("collector_upstream_client_aclose_required") from exc
        if not callable(aclose):
            raise RuntimeError("collector_upstream_client_aclose_required")
        await aclose()

    async def _clear_pending_snapshots(self) -> None:
        tasks = list(self._pending_snapshots.values())
        self._pending_snapshots.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _flush_pending_snapshots(self) -> None:
        tasks = list(self._pending_snapshots.values())
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self._pending_snapshots.clear()
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def handle_frame(self, frame_data: Any, *, received_at_ms: int | None = None) -> None:
        received_at_ms = received_at_ms or _now_ms()
        raw_frame = frame_data if isinstance(frame_data, str) else str(frame_data)
        if len(raw_frame.encode("utf-8")) > _GMGN_FRAME_MAX_BYTES:
            raise GmgnStreamExpectedError("gmgn_frame_byte_limit_exceeded")
        self.status.frames_received += 1
        self.status.last_frame_at_ms = received_at_ms

        try:
            parsed = parse_gmgn_frame(frame_data)
        except json.JSONDecodeError as exc:
            self.status.parse_errors += 1
            logger.warning(f"Failed to parse GMGN frame: {exc}")
            return

        if not parsed:
            return
        items = parsed["data"]
        if not isinstance(items, list) or len(items) > _GMGN_FRAME_MAX_ITEMS:
            raise GmgnStreamExpectedError("gmgn_frame_item_limit_exceeded")

        channel = parsed["channel"]
        try:
            await self.db.run_business(
                "gmgn_raw_frame_publish",
                self.store.insert_raw_frame,
                operation_timeout_seconds=3.0,
                source="gmgn",
                channel=channel,
                received_at_ms=received_at_ms,
                raw_payload_json=raw_frame,
            )
        except ResourceAdmissionTimeout as exc:
            raise GmgnStreamExpectedError("gmgn_database_admission_timeout") from exc
        except ResourceOperationOverrun as exc:
            if exc.capability is not ResourceCapability.DATABASE_BUSINESS:
                raise
            raise GmgnStreamExpectedError("gmgn_database_operation_overrun") from exc
        for item in items:
            if not isinstance(item, dict):
                continue
            await self._handle_item(channel, item, received_at_ms)

    async def _handle_item(self, channel: str, item: dict[str, Any], received_at_ms: int) -> None:
        if channel == "public_broadcast" or not item.get("tw"):
            self._record_snapshot_gate_outcome("non_tw_channel")
            await self._process_item(channel, item, received_at_ms)
            return

        internal_id = item.get("i")
        if not internal_id:
            self._record_snapshot_gate_outcome("immediate_complete")
            await self._process_item(channel, item, received_at_ms)
            return

        if item.get("cp") == 1:
            pending_task = self._pending_snapshots.pop(str(internal_id), None)
            if pending_task:
                pending_task.cancel()
                await asyncio.gather(pending_task, return_exceptions=True)
                self._record_snapshot_gate_outcome("debounced_complete")
            else:
                self._record_snapshot_gate_outcome("immediate_complete")
            await self._process_item(channel, item, received_at_ms)
            return

        if str(internal_id) not in self._pending_snapshots:
            if len(self._pending_snapshots) >= _GMGN_PENDING_SNAPSHOT_LIMIT:
                raise GmgnStreamExpectedError("gmgn_pending_snapshot_limit_exceeded")
            if self._snapshot_task_group is None:
                raise RuntimeError("collector_snapshot_task_group_not_running")
            self._pending_snapshots[str(internal_id)] = self._snapshot_task_group.create_task(
                self._dispatch_snapshot_after_timeout(channel, item, received_at_ms, str(internal_id)),
                name=f"gmgn-snapshot:{internal_id}",
            )

    async def _dispatch_snapshot_after_timeout(
        self,
        channel: str,
        item: dict[str, Any],
        received_at_ms: int,
        internal_id: str,
    ) -> None:
        try:
            await asyncio.sleep(self.snapshot_timeout)
            self._record_snapshot_gate_outcome("debounced_timeout")
            await self._process_item(channel, item, received_at_ms)
        except GmgnStreamExpectedError as exc:
            logger.warning(f"GMGN delayed snapshot skipped after expected failure: {exc}")
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            if self._pending_snapshots.get(internal_id) is current_task:
                self._pending_snapshots.pop(internal_id, None)

    async def _process_item(self, channel: str, item: dict[str, Any], received_at_ms: int) -> None:
        async with self._event_publish_lock:
            payload = {"channel": channel, "data": [item]}
            for event in normalize_gmgn_payload(payload, received_at_ms=received_at_ms):
                try:
                    ingested = await self.db.run_business(
                        "gmgn_event_publish",
                        self.store.ingest_event,
                        event,
                        operation_timeout_seconds=5.0,
                    )
                except ResourceAdmissionTimeout as exc:
                    raise GmgnStreamExpectedError("gmgn_database_admission_timeout") from exc
                except ResourceOperationOverrun as exc:
                    if exc.capability is not ResourceCapability.DATABASE_BUSINESS:
                        raise
                    raise GmgnStreamExpectedError("gmgn_database_operation_overrun") from exc
                if ingested.inserted:
                    self.status.twitter_events += 1
                    self.status.last_event_at_ms = received_at_ms
                else:
                    self.status.duplicate_twitter_events += 1

    def _record_snapshot_gate_outcome(self, outcome: str) -> None:
        self.status.snapshot_gate_outcomes[outcome] = self.status.snapshot_gate_outcomes.get(outcome, 0) + 1


def _now_ms() -> int:
    return int(time.time() * 1000)
