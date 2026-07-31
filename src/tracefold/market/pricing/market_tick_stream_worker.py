from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, cast

from tracefold.market.identity.chain_identity import chain_address_key
from tracefold.market.pricing.market_tick import (
    MarketTick,
    MarketTickSourceProvider,
    MarketTickSourceTier,
)
from tracefold.market.pricing.market_tick_id import market_tick_id
from tracefold.market.pricing.market_tick_persistence import (
    MarketTickPersistenceResult,
    MarketTickPersistenceService,
)
from tracefold.market.provider_contracts import (
    DexMarketFactUpdate,
    DexMarketStreamProvider,
    DexMarketStreamTarget,
    MarketStreamExpectedError,
)
from tracefold.market.radar.constants import TOKEN_RADAR_PROJECTION_VERSION, WINDOW_MS
from tracefold.platform.resource import ResourceAdmissionTimeout

SOURCE_TIER: MarketTickSourceTier = "tier1_ws"
SOURCE_PROVIDER: MarketTickSourceProvider = "okx_dex_ws"
_STREAM_FLUSH_COUNT = 500
_STREAM_FLUSH_BYTES = 1 * 1024 * 1024
_STREAM_FLUSH_SECONDS = 1.0
_STREAM_RECONNECT_BACKOFF_SECONDS = 3.0


class _AsyncCloseIterator(Protocol):
    async def aclose(self) -> None: ...


class MarketTickStream:
    def __init__(
        self,
        *,
        db: Any,
        stream_dex_market: DexMarketStreamProvider,
        clock: Any | None = None,
    ) -> None:
        if db is None:
            raise RuntimeError("market_tick_stream_db_required")
        if stream_dex_market is None:
            raise RuntimeError("market_tick_stream_provider_required")
        self.db = db
        self.stream_dex_market = stream_dex_market
        self.subscription_limit = 100
        self.stream_cycle_seconds = 30.0
        self.clock = clock or _now_ms

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            result = await self._cycle(stop_event=stop_event)
            if result is not None and result.degraded:
                await _wait_or_stop(stop_event, _STREAM_RECONNECT_BACKOFF_SECONDS)
            await asyncio.sleep(0)

    async def _cycle(self, *, stop_event: asyncio.Event) -> _StreamPersistResult | None:
        rows = await self.db.run_business(
            "market_tick_stream_load",
            self._list_stream_rows,
            operation_timeout_seconds=3.0,
        )
        targets, _skipped_targets = _stream_targets(rows, limit=self.subscription_limit)
        if not targets:
            await _wait_or_stop(stop_event, 5.0)
            return None

        stream_dex_market = self.stream_dex_market
        return await self._stream_and_persist_ticks(
            targets,
            stream_dex_market=stream_dex_market,
            stop_event=stop_event,
        )

    def _list_stream_rows(self) -> list[dict[str, Any]]:
        now_ms = int(self.clock())
        with self.db.worker_session("market_tick_stream") as repos:
            rows = repos.registry.ranked_market_targets(
                projection_version=TOKEN_RADAR_PROJECTION_VERSION,
                since_ms=now_ms - WINDOW_MS["24h"],
                target_types=("chain_token",),
                limit=self.subscription_limit,
            )
        return [dict(row) for row in rows]

    async def _stream_and_persist_ticks(
        self,
        targets: list[DexMarketStreamTarget],
        *,
        stream_dex_market: DexMarketStreamProvider,
        stop_event: asyncio.Event,
    ) -> _StreamPersistResult:
        target_by_key = {_target_key(target.chain_id, target.address): target for target in targets}
        ticks: list[MarketTick] = []
        tick_bytes = 0
        attempted = 0
        skipped = 0
        inserted = 0
        degraded_result: _StreamPersistResult | None = None
        try:
            try:
                await _await_stream_operation_or_stop(
                    stream_dex_market.replace_subscriptions(targets),
                    stop_event=stop_event,
                    timeout_seconds=self.stream_cycle_seconds,
                )
            except TimeoutError as exc:
                raise MarketStreamExpectedError("market_stream_subscription_timeout") from exc
            iterator = stream_dex_market.iter_price_info().__aiter__()
            deadline = time.monotonic() + self.stream_cycle_seconds
            flush_deadline = time.monotonic() + _STREAM_FLUSH_SECONDS
            stop_task = asyncio.create_task(stop_event.wait(), name="market-stream-stop-wait")
            next_task: asyncio.Future[Any] | None = None
            try:
                while not stop_event.is_set():
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        break
                    if next_task is None:
                        next_task = asyncio.ensure_future(
                            iterator.__anext__(),
                        )
                    done, _ = await asyncio.wait(
                        {next_task, stop_task},
                        timeout=max(
                            0.001,
                            min(remaining_seconds, flush_deadline - time.monotonic()),
                        ),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_task in done:
                        break
                    if next_task not in done:
                        pending = tuple(ticks)
                        ticks.clear()
                        tick_bytes = 0
                        inserted += await self._flush_ticks(pending)
                        flush_deadline = time.monotonic() + _STREAM_FLUSH_SECONDS
                        continue
                    try:
                        update = next_task.result()
                    except StopAsyncIteration:
                        break
                    finally:
                        next_task = None
                    target = target_by_key.get(_target_key(update.chain_id, update.address))
                    if target is None:
                        skipped += 1
                        continue
                    tick = _tick_from_update(update, target=target, received_at_ms=int(self.clock()))
                    if tick is None:
                        skipped += 1
                        continue
                    encoded_bytes = _tick_encoded_bytes(tick)
                    if encoded_bytes > _STREAM_FLUSH_BYTES:
                        raise MarketStreamExpectedError("market_stream_tick_byte_limit_exceeded")
                    if ticks and tick_bytes + encoded_bytes > _STREAM_FLUSH_BYTES:
                        pending = tuple(ticks)
                        ticks.clear()
                        tick_bytes = 0
                        inserted += await self._flush_ticks(pending)
                        flush_deadline = time.monotonic() + _STREAM_FLUSH_SECONDS
                    ticks.append(tick)
                    tick_bytes += encoded_bytes
                    attempted += 1
                    if len(ticks) >= _STREAM_FLUSH_COUNT or tick_bytes >= _STREAM_FLUSH_BYTES:
                        pending = tuple(ticks)
                        ticks.clear()
                        tick_bytes = 0
                        inserted += await self._flush_ticks(pending)
                        flush_deadline = time.monotonic() + _STREAM_FLUSH_SECONDS
            except MarketStreamExpectedError as exc:
                pending = tuple(ticks)
                ticks.clear()
                inserted += await self._flush_ticks(pending)
                degraded_result = _degraded_stream_result(
                    inserted=inserted,
                    attempted=attempted,
                    skipped=skipped,
                    stream_dex_market=stream_dex_market,
                    exc=exc,
                )
            finally:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                if next_task is not None:
                    next_task.cancel()
                    await asyncio.gather(next_task, return_exceptions=True)
                await cast(_AsyncCloseIterator, iterator).aclose()
            if degraded_result is not None:
                return degraded_result
        except MarketStreamExpectedError as exc:
            pending = tuple(ticks)
            ticks.clear()
            inserted += await self._flush_ticks(pending)
            return _degraded_stream_result(
                inserted=inserted,
                attempted=attempted,
                skipped=skipped,
                stream_dex_market=stream_dex_market,
                exc=exc,
            )
        pending = tuple(ticks)
        ticks.clear()
        inserted += await self._flush_ticks(pending)
        return _StreamPersistResult(inserted=inserted, attempted=attempted, skipped=skipped)

    async def _flush_ticks(self, ticks: Iterable[MarketTick]) -> int:
        try:
            return await self._persist_ticks(ticks)
        except ResourceAdmissionTimeout as exc:
            raise MarketStreamExpectedError("market_stream_database_admission_timeout") from exc

    async def _persist_ticks(self, ticks: Iterable[MarketTick]) -> int:
        materialized = list(ticks)
        if not materialized:
            return 0
        result = cast(
            MarketTickPersistenceResult,
            await self.db.run_business(
                "market_tick_stream_publish",
                self._persist_ticks_sync,
                materialized,
                operation_timeout_seconds=3.0,
            ),
        )
        return result.inserted

    def _persist_ticks_sync(self, ticks: list[MarketTick]) -> MarketTickPersistenceResult:
        with self.db.worker_session("market_tick_stream") as repos, repos.transaction():
            return MarketTickPersistenceService(repos).persist_ticks(
                ticks,
                now_ms=int(self.clock()),
            )


@dataclass(frozen=True, slots=True)
class _TargetParts:
    chain_id: str
    address: str


@dataclass(frozen=True, slots=True)
class _StreamPersistResult:
    inserted: int
    attempted: int
    skipped: int
    degraded: bool = False
    provider_state: dict[str, Any] | None = None
    failure_category: str | None = None


def _tick_encoded_bytes(tick: MarketTick) -> int:
    return len(
        json.dumps(
            asdict(tick),
            default=str,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.001, float(seconds)))
    except TimeoutError:
        return


async def _await_stream_operation_or_stop(
    awaitable: Any,
    *,
    stop_event: asyncio.Event,
    timeout_seconds: float,
) -> None:
    operation = asyncio.create_task(awaitable, name="market-stream-operation")
    stop_task = asyncio.create_task(stop_event.wait(), name="market-stream-operation-stop")
    try:
        done, _ = await asyncio.wait(
            {operation, stop_task},
            timeout=max(0.001, float(timeout_seconds)),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            await operation
            return
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        if stop_task in done:
            return
        raise TimeoutError
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


def _degraded_stream_result(
    *,
    inserted: int,
    attempted: int,
    skipped: int,
    stream_dex_market: DexMarketStreamProvider,
    exc: BaseException,
) -> _StreamPersistResult:
    provider_state = _provider_connection_state_payload(stream_dex_market)
    return _StreamPersistResult(
        inserted=inserted,
        attempted=attempted,
        skipped=skipped,
        degraded=True,
        provider_state=provider_state,
        failure_category=_provider_failure_category(provider_state, exc),
    )


def _provider_connection_state_payload(provider: DexMarketStreamProvider) -> dict[str, Any]:
    value = provider.connection_state_payload()
    if not isinstance(value, dict):
        return {
            "state": "failed",
            "last_error_category": "provider_connection_state_payload_not_dict",
        }
    return value


def _provider_failure_category(provider_state: Mapping[str, Any], exc: BaseException) -> str:
    category = provider_state.get("last_error_category") if isinstance(provider_state, Mapping) else None
    if category:
        return str(category)
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


def _stream_targets(rows: Sequence[Mapping[str, Any]], *, limit: int) -> tuple[list[DexMarketStreamTarget], int]:
    targets: list[DexMarketStreamTarget] = []
    skipped = 0
    for row in rows[:limit]:
        target_type = str(row.get("target_type") or "").strip()
        if target_type != "chain_token":
            skipped += 1
            continue
        target_id = str(row.get("target_id") or "").strip()
        parts = _chain_token_parts(row, target_id=target_id)
        if parts is None:
            skipped += 1
            continue
        targets.append(
            DexMarketStreamTarget(
                chain_id=parts.chain_id,
                address=parts.address,
                subject_type="chain_token",
                subject_id=target_id,
                pricefeed_id=str(row.get("pricefeed_id") or "") or None,
            )
        )
    return targets, skipped


def _chain_token_parts(row: Mapping[str, Any], *, target_id: str) -> _TargetParts | None:
    chain_id = str(row.get("chain_id") or "").strip()
    address = str(row.get("address") or "").strip()
    if chain_id and address:
        return _TargetParts(chain_id=chain_id, address=address)
    if ":" not in target_id:
        return None
    parsed_chain_id, parsed_address = target_id.rsplit(":", 1)
    parsed_chain_id = parsed_chain_id.strip()
    parsed_address = parsed_address.strip()
    if not parsed_chain_id or not parsed_address:
        return None
    return _TargetParts(chain_id=parsed_chain_id, address=parsed_address)


def _tick_from_update(
    update: DexMarketFactUpdate,
    *,
    target: DexMarketStreamTarget,
    received_at_ms: int,
) -> MarketTick | None:
    price_usd = _positive_decimal(update.price_usd)
    if price_usd is None:
        return None
    observed_at_ms = int(update.observed_at_ms)
    return MarketTick(
        tick_id=market_tick_id(
            target_type=target.subject_type,
            target_id=target.subject_id,
            source_provider=SOURCE_PROVIDER,
            observed_at_ms=observed_at_ms,
        ),
        target_type="chain_token",
        target_id=target.subject_id,
        chain=target.chain_id,
        token_address=target.address,
        exchange=None,
        instrument=None,
        pricefeed_id=target.pricefeed_id,
        source_tier=SOURCE_TIER,
        source_provider=SOURCE_PROVIDER,
        observed_at_ms=observed_at_ms,
        received_at_ms=received_at_ms,
        price_usd=price_usd,
        liquidity_usd=_decimal_or_none(update.liquidity_usd),
        volume_24h_usd=_decimal_or_none(update.volume_24h_usd),
        market_cap_usd=_decimal_or_none(update.market_cap_usd),
        holders=_int_or_none(update.holders),
        created_at_ms=received_at_ms,
        open_interest_usd=_decimal_or_none(update.open_interest_usd),
        raw_payload_json=update.raw or {},
    )


def _positive_decimal(value: Any) -> Decimal | None:
    result = _decimal_or_none(value)
    if result is None or result <= 0:
        return None
    return result


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _target_key(chain_id: str, address: str) -> tuple[str, str]:
    return chain_address_key(chain_id, address)


def _now_ms() -> int:
    return int(time.time() * 1000)
