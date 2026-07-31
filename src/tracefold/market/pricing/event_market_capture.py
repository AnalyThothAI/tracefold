from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast

from tracefold.market.pricing.market_tick import (
    DEX_QUOTE_SOURCE_PROVIDERS,
    EnrichedEventCapture,
    MarketTick,
    MarketTickSourceProvider,
)
from tracefold.market.pricing.market_tick_id import market_tick_id
from tracefold.market.provider_contracts import (
    AssetMarketProviderBundle,
    CexTicker,
    DexTokenQuote,
    DexTokenQuoteRequest,
    MarketProviderExpectedError,
)

TargetType = Literal["chain_token", "cex_symbol"]
DEX_SOURCE_PROVIDER: MarketTickSourceProvider = "okx_dex_rest"
CEX_SOURCE_PROVIDER: MarketTickSourceProvider = "binance_cex_rest"


@dataclass(frozen=True, slots=True)
class TickLookup:
    latest_at_or_before: Callable[[str, str, int, int], Mapping[str, Any] | None]


@dataclass(frozen=True, slots=True)
class CaptureResult:
    tick: MarketTick | None
    capture: EnrichedEventCapture


@dataclass(frozen=True, slots=True)
class _CaptureRequest:
    event_id: str
    intent_id: str
    resolution_id: str
    target_type: TargetType
    target_id: str
    event_ms: int


class EventMarketCaptureService:
    def __init__(
        self,
        providers: AssetMarketProviderBundle,
        now_ms: Callable[[], int],
        max_existing_tick_lag_ms: int = 60_000,
    ) -> None:
        self._providers = providers
        self._now_ms = now_ms
        self._max_existing_tick_lag_ms = int(max_existing_tick_lag_ms)

    def capture_for_event(
        self,
        *,
        event_id: str,
        intent_id: str,
        resolution_id: str,
        resolution: Mapping[str, Any],
        event_ms: int,
        tick_lookup: TickLookup,
    ) -> CaptureResult:
        """Lookup-only inline capture used by the collector path.

        Returns an existing fresh tick when one is present within
        ``max_existing_tick_lag_ms``. Otherwise emits an ``unavailable`` /
        ``pending_backfill`` capture so the async backfill worker can pick it
        up — never calls upstream providers inside the collector hot path.
        """
        target_type = _target_type(resolution.get("target_type"))
        target_id = _clean_str(resolution.get("target_id"))
        if target_type is None or not target_id:
            req = _CaptureRequest(event_id, intent_id, resolution_id, target_type or "chain_token", target_id, event_ms)
            return _unavailable(req, reason="invalid_resolution", created_at_ms=self._now_ms())

        req = _CaptureRequest(event_id, intent_id, resolution_id, target_type, target_id, event_ms)
        row = tick_lookup.latest_at_or_before(target_type, target_id, event_ms, self._max_existing_tick_lag_ms)
        if row is not None:
            return _existing_capture(req, row=row, created_at_ms=self._now_ms())
        return _unavailable(req, reason="pending_backfill", created_at_ms=self._now_ms())

    def capture_backfill_quote(
        self,
        *,
        event_id: str,
        intent_id: str,
        resolution_id: str,
        resolution: Mapping[str, Any],
        event_ms: int,
    ) -> CaptureResult:
        """Async-backfill capture that calls the appropriate provider.

        Dispatches deterministically by ``target_type``: ``chain_token``
        uses ``providers.dex_quote_market`` (GMGN OpenAPI primary + OKX DEX
        REST fallback) and ``cex_symbol`` uses
        ``providers.cex_market`` (Binance USD-M futures REST). No free-form
        provider choice.
        """
        target_type = _target_type(resolution.get("target_type"))
        target_id = _clean_str(resolution.get("target_id"))
        if target_type is None or not target_id:
            req = _CaptureRequest(event_id, intent_id, resolution_id, target_type or "chain_token", target_id, event_ms)
            return _unavailable(req, reason="invalid_resolution", created_at_ms=self._now_ms())

        req = _CaptureRequest(event_id, intent_id, resolution_id, target_type, target_id, event_ms)
        if target_type == "chain_token":
            return _capture_chain_token(req, providers=self._providers, now_ms=self._now_ms)
        return _capture_cex_symbol(req, providers=self._providers, now_ms=self._now_ms)


def _capture_chain_token(
    req: _CaptureRequest,
    *,
    providers: AssetMarketProviderBundle,
    now_ms: Callable[[], int],
) -> CaptureResult:
    provider = providers.dex_quote_market
    if provider is None:
        return _unavailable(req, reason="missing_provider", created_at_ms=now_ms())
    chain_id, address = _parse_chain_token_target_id(req.target_id)
    if not chain_id or not address:
        return _unavailable(req, reason="missing_market_key", created_at_ms=now_ms())

    try:
        quotes = provider.token_quotes([DexTokenQuoteRequest(chain_id=chain_id, address=address)])
    except MarketProviderExpectedError as exc:
        return _unavailable(req, reason=_provider_error_reason(exc), created_at_ms=now_ms())
    if not quotes:
        return _unavailable(req, reason="provider_no_quote", created_at_ms=now_ms())
    quote = quotes[0]
    price_usd = _positive_decimal(quote.price_usd)
    if price_usd is None:
        return _unavailable(req, reason="no_market_data", created_at_ms=now_ms())

    created_at_ms = now_ms()
    observed_at_ms = int(quote.observed_at_ms or created_at_ms)
    source_provider = _dex_source_provider(quote)
    tick = MarketTick(
        tick_id=market_tick_id(
            target_type=req.target_type,
            target_id=req.target_id,
            source_provider=source_provider,
            observed_at_ms=observed_at_ms,
        ),
        target_type=req.target_type,
        target_id=req.target_id,
        chain=chain_id,
        token_address=address,
        exchange=None,
        instrument=None,
        pricefeed_id=None,
        source_tier="tier3_inline",
        source_provider=source_provider,
        observed_at_ms=observed_at_ms,
        received_at_ms=created_at_ms,
        price_usd=price_usd,
        liquidity_usd=_optional_decimal(quote.liquidity_usd),
        volume_24h_usd=_optional_decimal(quote.volume_24h_usd),
        market_cap_usd=_optional_decimal(quote.market_cap_usd),
        holders=_int_or_none(quote.holders),
        created_at_ms=created_at_ms,
        raw_payload_json=dict(quote.raw),
    )
    return CaptureResult(
        tick=tick,
        capture=_capture(req, tick=tick, reason="inline_quote", created_at_ms=created_at_ms),
    )


def _capture_cex_symbol(
    req: _CaptureRequest,
    *,
    providers: AssetMarketProviderBundle,
    now_ms: Callable[[], int],
) -> CaptureResult:
    provider = providers.cex_market
    if provider is None:
        return _unavailable(req, reason="missing_provider", created_at_ms=now_ms())
    exchange, instrument = _parse_cex_symbol_target_id(req.target_id)
    if not instrument:
        return _unavailable(req, reason="missing_market_key", created_at_ms=now_ms())

    try:
        ticker = provider.ticker(inst_id=instrument)
    except MarketProviderExpectedError as exc:
        return _unavailable(req, reason=_provider_error_reason(exc), created_at_ms=now_ms())
    if ticker is None:
        return _unavailable(req, reason="provider_no_quote", created_at_ms=now_ms())
    price_usd = _positive_decimal(ticker.last_price)
    if price_usd is None:
        return _unavailable(req, reason="no_market_data", created_at_ms=now_ms())

    created_at_ms = now_ms()
    observed_at_ms = _ticker_observed_at_ms(ticker) or created_at_ms
    tick = MarketTick(
        tick_id=market_tick_id(
            target_type=req.target_type,
            target_id=req.target_id,
            source_provider=CEX_SOURCE_PROVIDER,
            observed_at_ms=observed_at_ms,
        ),
        target_type=req.target_type,
        target_id=req.target_id,
        chain=None,
        token_address=None,
        exchange=exchange or None,
        instrument=instrument,
        pricefeed_id=None,
        source_tier="tier3_inline",
        source_provider=CEX_SOURCE_PROVIDER,
        observed_at_ms=observed_at_ms,
        received_at_ms=created_at_ms,
        price_usd=price_usd,
        liquidity_usd=None,
        volume_24h_usd=_optional_decimal(ticker.volume_24h),
        market_cap_usd=None,
        holders=None,
        created_at_ms=created_at_ms,
        open_interest_usd=_ticker_open_interest_usd(ticker),
        raw_payload_json=dict(ticker.raw),
    )
    return CaptureResult(
        tick=tick,
        capture=_capture(req, tick=tick, reason="inline_ticker", created_at_ms=created_at_ms),
    )


def _existing_capture(
    req: _CaptureRequest,
    *,
    row: Mapping[str, Any],
    created_at_ms: int,
) -> CaptureResult:
    observed_at_ms = _int_or_none(row.get("observed_at_ms"))
    tick = _market_tick_from_row(row)
    return CaptureResult(
        tick=tick,
        capture=EnrichedEventCapture(
            event_id=req.event_id,
            intent_id=req.intent_id,
            resolution_id=req.resolution_id,
            target_type=req.target_type,
            target_id=req.target_id,
            t_event_ms=int(req.event_ms),
            tick_observed_at_ms=observed_at_ms,
            tick_id=_clean_str(row.get("tick_id")) or None,
            tick_lag_ms=_tick_lag_ms(observed_at_ms=observed_at_ms, event_ms=req.event_ms),
            capture_method=cast(Any, _clean_str(row.get("source_tier")) or "unavailable"),
            capture_reason="fresh_tick",
            created_at_ms=created_at_ms,
        ),
    )


def _unavailable(req: _CaptureRequest, *, reason: str, created_at_ms: int) -> CaptureResult:
    return CaptureResult(
        tick=None,
        capture=EnrichedEventCapture(
            event_id=req.event_id,
            intent_id=req.intent_id,
            resolution_id=req.resolution_id,
            target_type=req.target_type,
            target_id=req.target_id,
            t_event_ms=int(req.event_ms),
            tick_observed_at_ms=None,
            tick_id=None,
            tick_lag_ms=None,
            capture_method="unavailable",
            capture_reason=reason,
            created_at_ms=created_at_ms,
        ),
    )


def _capture(
    req: _CaptureRequest,
    *,
    tick: MarketTick,
    reason: str,
    created_at_ms: int,
) -> EnrichedEventCapture:
    return EnrichedEventCapture(
        event_id=req.event_id,
        intent_id=req.intent_id,
        resolution_id=req.resolution_id,
        target_type=req.target_type,
        target_id=req.target_id,
        t_event_ms=int(req.event_ms),
        tick_observed_at_ms=tick.observed_at_ms,
        tick_id=tick.tick_id,
        tick_lag_ms=_tick_lag_ms(observed_at_ms=tick.observed_at_ms, event_ms=req.event_ms),
        capture_method=tick.source_tier,
        capture_reason=reason,
        created_at_ms=created_at_ms,
    )


def _market_tick_from_row(row: Mapping[str, Any]) -> MarketTick | None:
    required = (
        "tick_id",
        "target_type",
        "target_id",
        "source_tier",
        "source_provider",
        "observed_at_ms",
        "received_at_ms",
        "price_usd",
        "created_at_ms",
    )
    if any(row.get(field) is None for field in required):
        return None
    return MarketTick(
        tick_id=str(row["tick_id"]),
        target_type=cast(Any, str(row["target_type"])),
        target_id=str(row["target_id"]),
        chain=_optional_str(row.get("chain")),
        token_address=_optional_str(row.get("token_address")),
        exchange=_optional_str(row.get("exchange")),
        instrument=_optional_str(row.get("instrument")),
        pricefeed_id=_optional_str(row.get("pricefeed_id")),
        source_tier=cast(Any, str(row["source_tier"])),
        source_provider=cast(Any, str(row["source_provider"])),
        observed_at_ms=int(row["observed_at_ms"]),
        received_at_ms=int(row["received_at_ms"]),
        price_usd=_decimal(row["price_usd"]),
        liquidity_usd=_optional_decimal(row.get("liquidity_usd")),
        volume_24h_usd=_optional_decimal(row.get("volume_24h_usd")),
        market_cap_usd=_optional_decimal(row.get("market_cap_usd")),
        holders=_int_or_none(row.get("holders")),
        created_at_ms=int(row["created_at_ms"]),
        open_interest_usd=_optional_decimal(row.get("open_interest_usd")),
        raw_payload_json=dict(row.get("raw_payload_json") or {}),
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


def _parse_chain_token_target_id(target_id: str) -> tuple[str, str]:
    chain_id, separator, address = target_id.rpartition(":")
    if not separator:
        return "", ""
    return chain_id.strip(), address.strip()


def _parse_cex_symbol_target_id(target_id: str) -> tuple[str, str]:
    exchange, separator, instrument = target_id.partition(":")
    if not separator:
        return "", ""
    return exchange.strip(), instrument.strip()


def _provider_error_reason(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "provider_timeout"
    text = f"{type(exc).__name__} {exc}".lower()
    if "429" in text or ("rate" in text and "limit" in text):
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "provider_timeout"
    return "provider_error"


def _target_type(value: Any) -> TargetType | None:
    text = _clean_str(value)
    return cast(TargetType, text) if text in {"chain_token", "cex_symbol"} else None


def _dex_source_provider(quote: DexTokenQuote) -> MarketTickSourceProvider:
    source_provider = _clean_str(quote.raw.get("source_provider"))
    if source_provider in DEX_QUOTE_SOURCE_PROVIDERS:
        return cast(MarketTickSourceProvider, source_provider)
    return DEX_SOURCE_PROVIDER


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        decimal = _decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal.is_finite() or decimal <= 0:
        return None
    return decimal


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        decimal = _decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return decimal if decimal.is_finite() else None


def _tick_lag_ms(*, observed_at_ms: int | None, event_ms: int) -> int | None:
    if observed_at_ms is None:
        return None
    return abs(int(observed_at_ms) - int(event_ms))


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _optional_str(value: Any) -> str | None:
    text = _clean_str(value)
    return text or None
