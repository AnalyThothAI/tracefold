"""Async backfill worker for short-lived event-anchor jobs.

``enriched_events`` stores the event-anchor fact lifecycle. The separate
``event_anchor_backfill_jobs`` table stores retry, due-time, and expiry
control state. This worker consumes due jobs, attaches an event-adjacent
tick when one exists, and terminalizes jobs that can no longer produce a
semantically valid event anchor.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, cast

from tracefold.market.pricing.event_market_capture import (
    EventMarketCaptureService,
)
from tracefold.market.pricing.market_tick import EnrichedEventCapture, MarketTick
from tracefold.market.pricing.market_tick_persistence import MarketTickPersistenceService
from tracefold.market.provider_contracts import AssetMarketProviderBundle
from tracefold.platform.resource import ResourceAdmissionTimeout

TEMPORARY_RETRY_BACKOFF_MS = 10_000
TEMPORARY_REASONS = frozenset({"provider_error", "provider_timeout", "rate_limited"})


@dataclass(frozen=True, slots=True)
class _AttachOutcome:
    row: dict[str, Any]
    tick: MarketTick
    capture: EnrichedEventCapture
    insert_tick: bool


@dataclass(frozen=True, slots=True)
class _TerminalOutcome:
    row: dict[str, Any]
    reason: str
    status: str


@dataclass(frozen=True, slots=True)
class _RescheduleOutcome:
    row: dict[str, Any]
    reason: str
    next_run_at_ms: int


_BackfillOutcome = _AttachOutcome | _TerminalOutcome | _RescheduleOutcome


class _AttachSkipped(Exception):
    pass


class _TerminalSkipped(Exception):
    pass


class EventAnchorBackfill:
    """Catch up unavailable/pending_backfill enriched events asynchronously."""

    worker_name = "event_anchor_backfill"

    def __init__(
        self,
        *,
        db: Any | None = None,
        capture_service: Any | None = None,
        providers: Any | None = None,
        finite_operations: Any,
        runtime_id: str,
        clock: Any | None = None,
    ) -> None:
        if db is None:
            raise RuntimeError("event_anchor_backfill_db_required")
        self.db = db
        self.finite_operations = finite_operations
        self.name = "event_anchor_backfill"
        self.claim_owner = f"event_anchor_backfill:{runtime_id}"
        self.clock = clock or _now_ms
        if capture_service is None:
            if providers is None:
                raise RuntimeError("event_anchor_backfill_providers_required")
            capture_service = EventMarketCaptureService(
                providers=cast("AssetMarketProviderBundle", providers),
                now_ms=lambda: int(self.clock()),
            )
        self._capture_service = capture_service
        self.batch_size = 1
        self.max_attempts = 3
        self.min_age_ms = 250
        self.lease_ms = 120_000
        self.active_window_ms = 300_000
        self.max_anchor_lag_ms = 60_000

    async def turn(self) -> bool | None:
        now_ms = int(self.clock())
        stale_jobs = await self.db.run_business(
            "event_anchor_expire",
            self._expire_stale_jobs,
            now_ms=now_ms,
            operation_timeout_seconds=3.0,
        )
        stale_terminal = int(stale_jobs["expired"]) + int(stale_jobs["failed"])
        stale_rescheduled = int(stale_jobs["rescheduled"])
        rows = await self.db.run_business(
            "event_anchor_claim",
            self._claim_due_jobs,
            now_ms=now_ms,
            operation_timeout_seconds=3.0,
        )
        if not rows:
            return bool(stale_terminal or stale_rescheduled)

        submitted = False

        def mark_submitted() -> None:
            nonlocal submitted
            submitted = True

        try:
            outcomes = [
                await self._capture_one(
                    row,
                    now_ms=now_ms,
                    on_submitted=mark_submitted,
                )
                for row in rows
            ]
        except asyncio.CancelledError:
            if not submitted:
                await asyncio.shield(self._release_prework(rows[0]))
            raise
        except ResourceAdmissionTimeout:
            await self._release_prework(rows[0])
            return None

        attaches: list[_AttachOutcome] = []
        terminals: list[_TerminalOutcome] = []
        reschedules: list[_RescheduleOutcome] = []
        skipped_reasons: Counter[str] = Counter()
        for outcome in outcomes:
            if isinstance(outcome, _AttachOutcome):
                attaches.append(outcome)
                continue
            if isinstance(outcome, _RescheduleOutcome):
                reschedules.append(outcome)
                skipped_reasons[outcome.reason] += 1
                continue
            terminals.append(outcome)
            skipped_reasons[outcome.reason] += 1

        await self.db.run_business(
            "event_anchor_publish",
            self._persist,
            attaches=attaches,
            terminals=terminals,
            reschedules=reschedules,
            now_ms=now_ms,
            operation_timeout_seconds=3.0,
        )
        return True

    async def _release_prework(self, row: Mapping[str, Any]) -> bool:
        return bool(
            await self.db.run_business(
                "event_anchor_release_prework",
                self._release_prework_sync,
                row,
                operation_timeout_seconds=0.5,
            )
        )

    def _release_prework_sync(self, row: Mapping[str, Any]) -> bool:
        with self._worker_session() as repos, repos.transaction():
            return bool(
                repos.event_anchor_jobs.release_prework(
                    event_id=str(row["event_id"]),
                    intent_id=str(row["intent_id"]),
                    lease_owner=_lease_owner(row),
                    attempt_count=_attempt_count(row),
                )
            )

    async def _capture_one(
        self,
        row: Mapping[str, Any],
        *,
        now_ms: int,
        on_submitted: Any,
    ) -> _BackfillOutcome:
        resolution = _resolution_from_row(row)
        existing = await self.db.run_business(
            "event_anchor_existing_tick",
            self._capture_existing_tick,
            row=row,
            now_ms=now_ms,
            operation_timeout_seconds=3.0,
        )
        if existing is not None:
            return cast(_BackfillOutcome, existing)
        if abs(now_ms - int(row["t_event_ms"])) > self.max_anchor_lag_ms:
            return _TerminalOutcome(
                row=dict(row),
                reason="backfill_expired",
                status="expired",
            )
        return cast(
            _BackfillOutcome,
            await self.finite_operations.run(
                "event_anchor_provider_quote",
                self._capture_provider_quote,
                row,
                resolution,
                now_ms,
                timeout_seconds=30.0,
                on_submitted=on_submitted,
            ),
        )

    def _capture_provider_quote(
        self,
        row: Mapping[str, Any],
        resolution: Mapping[str, Any],
        now_ms: int,
    ) -> _BackfillOutcome:
        result = self._capture_service.capture_backfill_quote(
            event_id=str(row["event_id"]),
            intent_id=str(row["intent_id"]),
            resolution_id=str(row["resolution_id"]),
            resolution=resolution,
            event_ms=int(row["t_event_ms"]),
        )
        if result.tick is not None:
            capture = replace(
                result.capture,
                capture_method="tier3_inline",
                capture_reason="async_backfill",
                event_id=str(row["event_id"]),
                intent_id=str(row["intent_id"]),
                resolution_id=str(row["resolution_id"]),
            )
            if capture.tick_lag_ms is not None and capture.tick_lag_ms <= self.max_anchor_lag_ms:
                return _AttachOutcome(row=dict(row), tick=result.tick, capture=capture, insert_tick=True)
            return _TerminalOutcome(row=dict(row), reason="backfill_expired", status="expired")

        reason = result.capture.capture_reason or "unknown"
        if self._should_reschedule(row=row, reason=reason, now_ms=now_ms):
            return _RescheduleOutcome(
                row=dict(row),
                reason=reason,
                next_run_at_ms=min(int(row["active_until_ms"]), now_ms + TEMPORARY_RETRY_BACKOFF_MS),
            )
        return _TerminalOutcome(row=dict(row), reason=reason, status="failed")

    def _capture_existing_tick(
        self,
        *,
        row: Mapping[str, Any],
        now_ms: int,
    ) -> _AttachOutcome | None:
        with self._worker_session() as repos:
            tick_row = repos.market_ticks.nearest_around(
                target_type=str(row["target_type"]),
                target_id=str(row["target_id"]),
                at_ms=int(row["t_event_ms"]),
                max_lag_ms=self.max_anchor_lag_ms,
            )
        if tick_row is None:
            return None
        tick = _market_tick_from_row(tick_row)
        capture = EnrichedEventCapture(
            event_id=str(row["event_id"]),
            intent_id=str(row["intent_id"]),
            resolution_id=str(row["resolution_id"]),
            target_type=cast(Any, str(row["target_type"])),
            target_id=str(row["target_id"]),
            t_event_ms=int(row["t_event_ms"]),
            tick_observed_at_ms=tick.observed_at_ms,
            tick_id=tick.tick_id,
            tick_lag_ms=abs(tick.observed_at_ms - int(row["t_event_ms"])),
            capture_method=tick.source_tier,
            capture_reason="async_backfill",
            created_at_ms=now_ms,
        )
        return _AttachOutcome(row=dict(row), tick=tick, capture=capture, insert_tick=False)

    def _should_reschedule(self, *, row: Mapping[str, Any], reason: str, now_ms: int) -> bool:
        if reason not in TEMPORARY_REASONS:
            return False
        if _attempt_count(row) >= self.max_attempts:
            return False
        if int(row["active_until_ms"]) <= now_ms:
            return False
        return abs(now_ms - int(row["t_event_ms"])) <= self.max_anchor_lag_ms

    def _expire_stale_jobs(self, *, now_ms: int) -> dict[str, int]:
        with self._worker_session() as repos, repos.transaction():
            summary = repos.event_anchor_jobs.expire_stale(
                limit=self.batch_size,
                now_ms=now_ms,
                max_attempts=self.max_attempts,
                retry_backoff_ms=TEMPORARY_RETRY_BACKOFF_MS,
            )
            terminal_rows = [dict(row) for row in summary.get("terminal_rows") or ()]
            for row in terminal_rows:
                repos.enriched_events.mark_backfill_terminal(
                    event_id=str(row["event_id"]),
                    intent_id=str(row["intent_id"]),
                    reason=_terminal_reason(row),
                )
            expired = int(summary.get("expired") or 0)
            failed = int(summary.get("failed") or 0)
            rescheduled = int(summary.get("rescheduled") or 0)
            return {"expired": expired, "failed": failed, "rescheduled": rescheduled}

    def _claim_due_jobs(self, *, now_ms: int) -> list[dict[str, Any]]:
        with self._worker_session() as repos, repos.transaction():
            rows = repos.event_anchor_jobs.claim_due(
                limit=self.batch_size,
                now_ms=now_ms,
                min_age_ms=self.min_age_ms,
                lease_owner=self.claim_owner,
                lease_ms=self.lease_ms,
            )
        return [dict(row) for row in rows]

    def _persist(
        self,
        *,
        attaches: Sequence[_AttachOutcome],
        terminals: Sequence[_TerminalOutcome],
        reschedules: Sequence[_RescheduleOutcome],
        now_ms: int,
    ) -> tuple[int, list[MarketTick], int, int]:
        if not attaches and not terminals and not reschedules:
            return 0, [], 0, 0
        with self._worker_session() as repos, repos.transaction():
            persistence = MarketTickPersistenceService(repos)
            inserted = 0
            attached_ticks: list[MarketTick] = []
            for attach in attaches:
                try:
                    with repos.transaction():
                        tick_inserted = 0
                        if attach.insert_tick:
                            tick_result = persistence.persist_ticks(
                                [attach.tick],
                                now_ms=now_ms,
                            )
                            tick_inserted = tick_result.inserted
                        if not repos.enriched_events.attach_backfill_capture(attach.capture):
                            raise _AttachSkipped
                        marked_done = repos.event_anchor_jobs.mark_done(
                            event_id=str(attach.row["event_id"]),
                            intent_id=str(attach.row["intent_id"]),
                            now_ms=now_ms,
                            lease_owner=_lease_owner(attach.row),
                            attempt_count=_attempt_count(attach.row),
                        )
                        if not marked_done:
                            raise _AttachSkipped
                except _AttachSkipped:
                    continue
                inserted += tick_inserted
                attached_ticks.append(attach.tick)
            terminal_count = 0
            for terminal in terminals:
                try:
                    with repos.transaction():
                        if not repos.event_anchor_jobs.mark_terminal(
                            event_id=str(terminal.row["event_id"]),
                            intent_id=str(terminal.row["intent_id"]),
                            status=terminal.status,
                            reason=terminal.reason,
                            now_ms=now_ms,
                            lease_owner=_lease_owner(terminal.row),
                            attempt_count=_attempt_count(terminal.row),
                        ):
                            raise _TerminalSkipped
                        repos.enriched_events.mark_backfill_terminal(
                            event_id=str(terminal.row["event_id"]),
                            intent_id=str(terminal.row["intent_id"]),
                            reason=terminal.reason,
                        )
                except _TerminalSkipped:
                    continue
                terminal_count += 1
            rescheduled_count = 0
            for reschedule in reschedules:
                if repos.event_anchor_jobs.reschedule(
                    event_id=str(reschedule.row["event_id"]),
                    intent_id=str(reschedule.row["intent_id"]),
                    reason=reschedule.reason,
                    now_ms=now_ms,
                    next_run_at_ms=reschedule.next_run_at_ms,
                    lease_owner=_lease_owner(reschedule.row),
                    attempt_count=_attempt_count(reschedule.row),
                ):
                    rescheduled_count += 1
        return inserted, attached_ticks, terminal_count, rescheduled_count

    @contextmanager
    def _worker_session(self) -> Iterator[Any]:
        with self.db.worker_session(
            self.name,
            statement_timeout_seconds=30.0,
        ) as repos:
            yield repos


def _resolution_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"target_type": str(row["target_type"]), "target_id": str(row["target_id"])}


def _market_tick_from_row(row: Mapping[str, Any]) -> MarketTick:
    return MarketTick(
        tick_id=str(row["tick_id"]),
        target_type=cast(Any, str(row["target_type"])),
        target_id=str(row["target_id"]),
        chain=_str_or_none(row.get("chain")),
        token_address=_str_or_none(row.get("token_address")),
        exchange=_str_or_none(row.get("exchange")),
        instrument=_str_or_none(row.get("instrument")),
        pricefeed_id=_str_or_none(row.get("pricefeed_id")),
        source_tier=cast(Any, str(row["source_tier"])),
        source_provider=cast(Any, str(row["source_provider"])),
        observed_at_ms=int(row["observed_at_ms"]),
        received_at_ms=int(row["received_at_ms"]),
        price_usd=_decimal(row["price_usd"]),
        liquidity_usd=_decimal_or_none(row.get("liquidity_usd")),
        volume_24h_usd=_decimal_or_none(row.get("volume_24h_usd")),
        market_cap_usd=_decimal_or_none(row.get("market_cap_usd")),
        holders=_int_or_none(row.get("holders")),
        created_at_ms=int(row["created_at_ms"]),
        open_interest_usd=_decimal_or_none(row.get("open_interest_usd")),
        raw_payload_json=dict(row.get("raw_payload_json") or {}),
    )


def _terminal_reason(row: Mapping[str, Any]) -> str:
    reason = str(row.get("last_reason") or "").strip()
    if reason:
        return reason
    if str(row.get("status") or "") == "failed":
        return "lease_expired_max_attempts"
    return "backfill_expired"


def _lease_owner(row: Mapping[str, Any]) -> str:
    try:
        value = row["lease_owner"]
    except KeyError as exc:
        raise ValueError("event_anchor_backfill_claim_lease_owner_required") from exc
    if value is None:
        raise ValueError("event_anchor_backfill_claim_lease_owner_required")
    lease_owner = str(value).strip()
    if not lease_owner:
        raise ValueError("event_anchor_backfill_claim_lease_owner_required")
    return lease_owner


def _attempt_count(row: Mapping[str, Any]) -> int:
    try:
        attempt_count = int(row["attempt_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("event_anchor_backfill_claim_attempt_count_required") from exc
    if attempt_count <= 0:
        raise ValueError("event_anchor_backfill_claim_attempt_count_required")
    return attempt_count


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["EventAnchorBackfill"]
