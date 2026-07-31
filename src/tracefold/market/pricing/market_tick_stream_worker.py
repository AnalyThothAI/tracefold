from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
)
from tracefold.market.radar.constants import TOKEN_RADAR_PROJECTION_VERSION, WINDOW_MS
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult

SOURCE_TIER: MarketTickSourceTier = "tier1_ws"
SOURCE_PROVIDER: MarketTickSourceProvider = "okx_dex_ws"


class _AsyncCloseIterator(Protocol):
    async def aclose(self) -> None: ...


class MarketTickStreamWorker(WorkerBase):
    worker_name = "market_tick_stream"

    def __init__(
        self,
        *,
        pool_bundle: Any,
        stream_dex_market: DexMarketStreamProvider,
        resources: Any,
        provider_governor: Any,
        clock: Any | None = None,
        name: str = "market_tick_stream",
        telemetry: Any,
    ) -> None:
        if pool_bundle is None:
            raise RuntimeError("market_tick_stream_db_required")
        if stream_dex_market is None:
            raise RuntimeError("market_tick_stream_provider_required")
        super().__init__(
            name=name,
            interval_seconds=5.0,
            telemetry=telemetry,
        )
        self.db = pool_bundle
        self.resources = resources
        self.provider_governor = provider_governor
        self.stream_dex_market = stream_dex_market
        self.subscription_limit = 100
        self.stream_cycle_seconds = 30.0
        self.clock = clock or _now_ms

    async def run_once(self) -> WorkerResult:
        rows = await self.resources.run_realtime_db(self._list_stream_rows)
        targets, skipped_targets = _stream_targets(rows, limit=self.subscription_limit)
        if not targets:
            return WorkerResult(
                skipped=skipped_targets,
                notes={"targets_selected": len(rows), "stream_targets": 0},
            )

        stream_dex_market = self.stream_dex_market
        async with self.provider_governor.acquire(host="dex_stream"):
            stream_result = await self._stream_and_persist_ticks(targets, stream_dex_market=stream_dex_market)
        notes: dict[str, Any] = {
            "targets_selected": len(rows),
            "stream_targets": len(targets),
            "ticks_attempted": stream_result.attempted,
            "ticks_inserted": stream_result.inserted,
            "invalid_frames": stream_result.skipped,
        }
        if stream_result.degraded:
            provider_state = stream_result.provider_state or {}
            notes.update(
                {
                    "degraded": True,
                    "provider_state": provider_state.get("state"),
                    "provider_state_payload": provider_state,
                    "failure_category": stream_result.failure_category,
                }
            )

        return WorkerResult(
            processed=stream_result.inserted,
            skipped=skipped_targets + stream_result.skipped,
            notes=notes,
        )

    def _list_stream_rows(self) -> list[dict[str, Any]]:
        now_ms = int(self.clock())
        with self.db.worker_session(self.name) as repos:
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
    ) -> _StreamPersistResult:
        target_by_key = {_target_key(target.chain_id, target.address): target for target in targets}
        ticks: list[MarketTick] = []
        skipped = 0
        inserted: int | None = None
        degraded_result: _StreamPersistResult | None = None
        try:
            await asyncio.wait_for(
                stream_dex_market.replace_subscriptions(targets),
                timeout=max(0.001, self.stream_cycle_seconds),
            )
            iterator = stream_dex_market.iter_price_info().__aiter__()
            deadline = time.monotonic() + self.stream_cycle_seconds
            try:
                while True:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        break
                    try:
                        update = await asyncio.wait_for(iterator.__anext__(), timeout=remaining_seconds)
                    except (TimeoutError, StopAsyncIteration):
                        break
                    target = target_by_key.get(_target_key(update.chain_id, update.address))
                    if target is None:
                        skipped += 1
                        continue
                    tick = _tick_from_update(update, target=target, received_at_ms=int(self.clock()))
                    if tick is None:
                        skipped += 1
                        continue
                    ticks.append(tick)
            except Exception as exc:
                inserted = await self._persist_ticks(ticks)
                degraded_result = _degraded_stream_result(
                    inserted=inserted,
                    attempted=len(ticks),
                    skipped=skipped,
                    stream_dex_market=stream_dex_market,
                    exc=exc,
                )
            finally:
                await cast(_AsyncCloseIterator, iterator).aclose()
            if degraded_result is not None:
                return degraded_result
        except Exception as exc:
            if inserted is None:
                inserted = await self._persist_ticks(ticks)
            return _degraded_stream_result(
                inserted=inserted,
                attempted=len(ticks),
                skipped=skipped,
                stream_dex_market=stream_dex_market,
                exc=exc,
            )
        inserted = await self._persist_ticks(ticks)
        return _StreamPersistResult(inserted=inserted, attempted=len(ticks), skipped=skipped)

    async def _persist_ticks(self, ticks: Iterable[MarketTick]) -> int:
        materialized = list(ticks)
        if not materialized:
            return 0
        result = cast(
            MarketTickPersistenceResult,
            await self.resources.run_realtime_db(
                self._persist_ticks_sync,
                materialized,
            ),
        )
        return result.inserted

    def _persist_ticks_sync(self, ticks: list[MarketTick]) -> MarketTickPersistenceResult:
        with self.db.worker_session(self.name) as repos, repos.transaction():
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
    try:
        value = provider.connection_state_payload()
    except AttributeError:
        return {
            "state": "failed",
            "last_error_category": "provider_connection_state_contract_missing",
        }
    except Exception as exc:
        return {"state": "unknown", "last_error_category": type(exc).__name__}
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
