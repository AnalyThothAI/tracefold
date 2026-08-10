from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from loguru import logger

from tracefold.market.identity.chain_identity import chain_address_key
from tracefold.market.pricing.market_tick import (
    DEX_QUOTE_SOURCE_PROVIDERS,
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
    CexTicker,
    DexTokenQuote,
    DexTokenQuoteRequest,
    MarketProviderExpectedError,
)
from tracefold.market.windows import PRODUCT_WINDOW_MS
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

SOURCE_TIER: MarketTickSourceTier = "tier2_poll"
DEX_SOURCE_PROVIDER: MarketTickSourceProvider = "okx_dex_rest"
CEX_SOURCE_PROVIDER: MarketTickSourceProvider = "binance_cex_rest"


class MarketTickPoll:
    def __init__(
        self,
        *,
        db: Any,
        providers: Any,
        finite_operations: Any,
        clock: Any | None = None,
    ) -> None:
        if providers is None:
            raise RuntimeError("market_tick_poll_providers_required")
        if db is None:
            raise RuntimeError("market_tick_poll_db_required")
        self.db = db
        self.finite_operations = finite_operations
        self.providers = providers
        self.dex_quote_market = providers.dex_quote_market
        self.cex_market = providers.cex_market
        self.batch_size = 100
        self.clock = clock or _now_ms
        self._recent_attempts: set[tuple[str, str]] = set()

    async def sample(self) -> None:
        # DB read happens off the event loop; provider IO must not run while a
        # DB session is held, so we materialize rows first, then drop the session.
        try:
            rows = await self.db.run_business(
                "market_tick_poll_load",
                self._list_poll_rows,
                operation_timeout_seconds=3.0,
            )
            targets = _poll_targets(rows)

            chain_result = await self._poll_chain_targets_async(targets.chain_targets)
            cex_result = await self._poll_cex_targets_async(targets.cex_targets)
        except ResourceAdmissionTimeout:
            return

        skipped_reasons: Counter[str] = Counter(targets.skipped_reasons)
        skipped_reasons.update(chain_result.skipped_reasons)
        skipped_reasons.update(cex_result.skipped_reasons)
        ticks = [*chain_result.ticks, *cex_result.ticks]

        try:
            await self.db.run_business(
                "market_tick_poll_publish",
                self._persist_ticks,
                ticks,
                operation_timeout_seconds=3.0,
            )
        except ResourceAdmissionTimeout:
            return

    def _list_poll_rows(self) -> list[dict[str, Any]]:
        now_ms = int(self.clock())
        exclude_keys = tuple(sorted(self._recent_attempts))
        with self.db.worker_session("market_tick_poll") as repos:
            rows = repos.registry.ranked_market_targets(
                since_ms=now_ms - PRODUCT_WINDOW_MS["24h"],
                target_types=("chain_token", "cex_symbol"),
                limit=self.batch_size,
                exclude_keys=exclude_keys,
            )
            if not rows and exclude_keys:
                self._recent_attempts.clear()
                rows = repos.registry.ranked_market_targets(
                    since_ms=now_ms - PRODUCT_WINDOW_MS["24h"],
                    target_types=("chain_token", "cex_symbol"),
                    limit=self.batch_size,
                )
        self._remember_attempts(rows)
        return [dict(row) for row in rows]

    def _remember_attempts(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            target_type = str(row.get("target_type") or "").strip()
            target_id = str(row.get("target_id") or "").strip()
            if target_type and target_id:
                self._recent_attempts.add((target_type, target_id))
        max_recent = max(self.batch_size * 50, 1_000)
        if len(self._recent_attempts) > max_recent:
            self._recent_attempts.clear()

    async def _poll_chain_targets_async(
        self,
        targets: list[_ChainTarget],
    ) -> _PollProviderResult:
        provider = self.dex_quote_market
        if provider is None:
            skipped: Counter[str] = Counter()
            skipped["dex_provider_unavailable"] += len(targets)
            return _PollProviderResult(ticks=[], skipped_reasons=skipped)
        if not targets:
            return _PollProviderResult(ticks=[], skipped_reasons=Counter())

        requests = [DexTokenQuoteRequest(chain_id=target.chain_id, address=target.address) for target in targets]
        try:
            quotes = await self.finite_operations.run(
                "market_tick_poll_dex",
                provider.token_quotes,
                requests,
                timeout_seconds=10.0,
            )
        except (MarketProviderExpectedError, ResourceOperationOverrun) as exc:
            reason = _provider_error_reason(exc)
            logger.bind(
                reason=reason,
                target_count=len(targets),
            ).warning("market tick poll batch quote failed")
            return _PollProviderResult(
                ticks=[],
                skipped_reasons=Counter({reason: len(targets)}),
            )

        skipped_reasons: Counter[str] = Counter()
        quotes_by_key = {_target_key(quote.chain_id, quote.address): quote for quote in quotes}
        ticks: list[MarketTick] = []
        for target in targets:
            quote = quotes_by_key.get(_target_key(target.chain_id, target.address))
            if quote is None:
                skipped_reasons["dex_quote_unavailable"] += 1
                logger.bind(
                    target_type="chain_token",
                    target_id=target.target_id,
                    reason="dex_quote_unavailable",
                ).warning("market tick poll quote skipped")
                continue
            tick = _tick_from_dex_quote(quote, target=target, received_at_ms=int(self.clock()))
            if tick is None:
                skipped_reasons["invalid_price"] += 1
                logger.bind(
                    target_type="chain_token",
                    target_id=target.target_id,
                    reason="invalid_price",
                ).warning("market tick poll quote skipped")
                continue
            ticks.append(tick)
        return _PollProviderResult(ticks=ticks, skipped_reasons=skipped_reasons)

    async def _poll_cex_targets_async(
        self,
        targets: list[_CexTarget],
    ) -> _PollProviderResult:
        provider = self.cex_market
        if provider is None:
            skipped: Counter[str] = Counter()
            skipped["cex_provider_unavailable"] += len(targets)
            return _PollProviderResult(ticks=[], skipped_reasons=skipped)
        if not targets:
            return _PollProviderResult(ticks=[], skipped_reasons=Counter())

        try:
            fetched = await self.finite_operations.run(
                "market_tick_poll_cex",
                provider.tickers,
                inst_type="SWAP",
                timeout_seconds=10.0,
            )
        except (MarketProviderExpectedError, ResourceOperationOverrun) as exc:
            reason = _provider_error_reason(exc)
            return _PollProviderResult(
                ticks=[],
                skipped_reasons=Counter({reason: len(targets)}),
            )
        by_instrument = {ticker.inst_id.upper(): ticker for ticker in fetched}
        outcomes: list[_SingleTargetOutcome] = []
        for target in targets:
            ticker = by_instrument.get(target.instrument.upper())
            if ticker is None:
                outcomes.append(
                    _SingleTargetOutcome(
                        tick=None,
                        skip_reason="cex_quote_unavailable",
                    )
                )
                continue
            tick = _tick_from_cex_ticker(ticker, target=target, received_at_ms=int(self.clock()))
            if tick is None:
                outcomes.append(
                    _SingleTargetOutcome(
                        tick=None,
                        skip_reason="invalid_price",
                    )
                )
                continue
            outcomes.append(_SingleTargetOutcome(tick=tick, skip_reason=None))

        return self._collect_outcomes(targets_kind="cex_symbol", targets=targets, outcomes=outcomes)

    def _collect_outcomes(
        self,
        *,
        targets_kind: str,
        targets: Sequence[Any],
        outcomes: Sequence[_SingleTargetOutcome],
    ) -> _PollProviderResult:
        skipped_reasons: Counter[str] = Counter()
        ticks: list[MarketTick] = []
        for target, outcome in zip(targets, outcomes, strict=True):
            if outcome.tick is not None:
                ticks.append(outcome.tick)
                continue
            reason = outcome.skip_reason or "provider_error"
            skipped_reasons[reason] += 1
            logger.bind(
                target_type=targets_kind,
                target_id=target.target_id,
                reason=reason,
            ).warning("market tick poll quote skipped")
        return _PollProviderResult(ticks=ticks, skipped_reasons=skipped_reasons)

    def _persist_ticks(self, ticks: Iterable[MarketTick]) -> MarketTickPersistenceResult:
        materialized = list(ticks)
        if not materialized:
            return MarketTickPersistenceResult(
                inserted_ids=[],
                current_rows=[],
                live_market_rows=[],
            )
        with self.db.worker_session("market_tick_poll") as repos, repos.transaction():
            return MarketTickPersistenceService(repos).persist_ticks(
                materialized,
                now_ms=int(self.clock()),
            )


@dataclass(frozen=True, slots=True)
class _ChainTarget:
    target_id: str
    chain_id: str
    address: str


@dataclass(frozen=True, slots=True)
class _CexTarget:
    target_id: str
    exchange: str
    instrument: str


@dataclass(frozen=True, slots=True)
class _PollTargets:
    chain_targets: list[_ChainTarget]
    cex_targets: list[_CexTarget]
    skipped_reasons: Counter[str]


@dataclass(frozen=True, slots=True)
class _PollProviderResult:
    ticks: list[MarketTick]
    skipped_reasons: Counter[str]


@dataclass(frozen=True, slots=True)
class _SingleTargetOutcome:
    tick: MarketTick | None
    skip_reason: str | None


def _poll_targets(rows: Sequence[Mapping[str, Any]]) -> _PollTargets:
    chain_targets: list[_ChainTarget] = []
    cex_targets: list[_CexTarget] = []
    skipped_reasons: Counter[str] = Counter()

    for row in rows:
        target_type = _clean_str(row.get("target_type"))
        target_id = _clean_str(row.get("target_id"))
        if target_type == "chain_token":
            chain_target = _chain_target(target_id)
            if chain_target is None:
                skipped_reasons["invalid_chain_target"] += 1
                continue
            chain_targets.append(chain_target)
            continue
        if target_type == "cex_symbol":
            cex_target = _cex_target(target_id)
            if cex_target is None:
                skipped_reasons["invalid_cex_target"] += 1
                continue
            cex_targets.append(cex_target)
            continue
        skipped_reasons["unsupported_target_type"] += 1

    return _PollTargets(
        chain_targets=chain_targets,
        cex_targets=cex_targets,
        skipped_reasons=skipped_reasons,
    )


def _chain_target(target_id: str) -> _ChainTarget | None:
    chain_id, separator, address = target_id.rpartition(":")
    if not separator:
        return None
    chain_id = chain_id.strip()
    address = address.strip()
    if not chain_id or not address:
        return None
    return _ChainTarget(target_id=target_id, chain_id=chain_id, address=address)


def _cex_target(target_id: str) -> _CexTarget | None:
    exchange, separator, instrument = target_id.partition(":")
    if not separator:
        return None
    exchange = exchange.strip()
    instrument = instrument.strip()
    if not exchange or not instrument or ":" in instrument:
        return None
    return _CexTarget(target_id=target_id, exchange=exchange, instrument=instrument)


def _tick_from_dex_quote(
    quote: DexTokenQuote,
    *,
    target: _ChainTarget,
    received_at_ms: int,
) -> MarketTick | None:
    price_usd = _positive_decimal(quote.price_usd)
    if price_usd is None:
        return None
    observed_at_ms = int(quote.observed_at_ms or received_at_ms)
    source_provider = _dex_source_provider(quote)
    return MarketTick(
        tick_id=market_tick_id(
            target_type="chain_token",
            target_id=target.target_id,
            source_provider=source_provider,
            observed_at_ms=observed_at_ms,
        ),
        target_type="chain_token",
        target_id=target.target_id,
        chain=target.chain_id,
        token_address=target.address,
        exchange=None,
        instrument=None,
        pricefeed_id=None,
        source_tier=SOURCE_TIER,
        source_provider=source_provider,
        observed_at_ms=observed_at_ms,
        received_at_ms=received_at_ms,
        price_usd=price_usd,
        liquidity_usd=_optional_decimal(quote.liquidity_usd),
        volume_24h_usd=_optional_decimal(quote.volume_24h_usd),
        market_cap_usd=_optional_decimal(quote.market_cap_usd),
        holders=_int_or_none(quote.holders),
        created_at_ms=received_at_ms,
        raw_payload_json=dict(quote.raw),
    )


def _tick_from_cex_ticker(
    ticker: CexTicker,
    *,
    target: _CexTarget,
    received_at_ms: int,
) -> MarketTick | None:
    price_usd = _positive_decimal(ticker.last_price)
    if price_usd is None:
        return None
    observed_at_ms = _ticker_observed_at_ms(ticker) or received_at_ms
    return MarketTick(
        tick_id=market_tick_id(
            target_type="cex_symbol",
            target_id=target.target_id,
            source_provider=CEX_SOURCE_PROVIDER,
            observed_at_ms=observed_at_ms,
        ),
        target_type="cex_symbol",
        target_id=target.target_id,
        chain=None,
        token_address=None,
        exchange=target.exchange,
        instrument=target.instrument,
        pricefeed_id=None,
        source_tier=SOURCE_TIER,
        source_provider=CEX_SOURCE_PROVIDER,
        observed_at_ms=observed_at_ms,
        received_at_ms=received_at_ms,
        price_usd=price_usd,
        liquidity_usd=None,
        volume_24h_usd=_optional_decimal(ticker.volume_24h),
        market_cap_usd=None,
        holders=None,
        created_at_ms=received_at_ms,
        open_interest_usd=_ticker_open_interest_usd(ticker),
        raw_payload_json=dict(ticker.raw),
    )


def _ticker_observed_at_ms(ticker: CexTicker) -> int | None:
    for key in ("observed_at_ms", "ts", "timestamp", "time"):
        observed_at_ms = _int_or_none(ticker.raw.get(key))
        if observed_at_ms is not None:
            return observed_at_ms
    return None


def _ticker_open_interest_usd(ticker: CexTicker) -> Decimal | None:
    for key in ("open_interest_usd", "openInterestUsd", "openInterestUSD", "oiUsd", "oiUSD"):
        open_interest_usd = _optional_decimal(ticker.raw.get(key))
        if open_interest_usd is not None:
            return open_interest_usd
    return None


def _positive_decimal(value: Any) -> Decimal | None:
    result = _optional_decimal(value)
    if result is None or result <= 0:
        return None
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
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


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _target_key(chain_id: str, address: str) -> tuple[str, str]:
    return chain_address_key(chain_id, address)


def _dex_source_provider(quote: DexTokenQuote) -> MarketTickSourceProvider:
    source_provider = _clean_str(quote.raw.get("source_provider"))
    if source_provider in DEX_QUOTE_SOURCE_PROVIDERS:
        return cast(MarketTickSourceProvider, source_provider)
    return DEX_SOURCE_PROVIDER


def _provider_error_reason(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, ResourceOperationOverrun)):
        return "provider_timeout"
    text = f"{type(exc).__name__} {exc}".lower()
    if "429" in text or ("rate" in text and "limit" in text):
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "provider_timeout"
    return "provider_error"


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["MarketTickPoll"]
