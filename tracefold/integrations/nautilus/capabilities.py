"""Pinned mainnet capability evidence for both closed execution bindings."""

from __future__ import annotations

from typing import Any

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType, BinanceEnvironment
from nautilus_trader.adapters.binance.common.urls import get_http_base_url
from nautilus_trader.adapters.binance.futures.providers import BinanceFuturesInstrumentProvider
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.adapters.hyperliquid import HyperliquidProductType
from nautilus_trader.adapters.hyperliquid.factories import (
    get_cached_hyperliquid_http_client,
    get_cached_hyperliquid_instrument_provider,
)
from nautilus_trader.common.component import LiveClock
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.instruments.crypto_perpetual import CryptoPerpetual

from tracefold.trading import (
    ExecutionInstrumentCapabilityV2,
    ExecutionInstrumentEvidenceV1,
    VenueInstrumentCatalogSnapshotV1,
)


async def load_binance_usdm_execution_evidence(
    catalog: VenueInstrumentCatalogSnapshotV1,
) -> list[ExecutionInstrumentEvidenceV1]:
    """Load exact mainnet client facts and bind each one to its public catalogue row."""

    if catalog.binding != "BINANCE_USDM" or catalog.venue != "binance.usdm":
        raise ValueError("binance_capability_catalog_binding_invalid")
    account_type = BinanceAccountType.USDT_FUTURES
    clock = LiveClock()
    client = BinanceHttpClient(
        clock=clock,
        api_key=None,
        api_secret=None,
        base_url=get_http_base_url(account_type, BinanceEnvironment.LIVE, is_us=False),
    )
    provider = BinanceFuturesInstrumentProvider(
        client=client,
        clock=clock,
        account_type=account_type,
        config=InstrumentProviderConfig(load_all=True, log_warnings=True),
    )
    await provider.load_all_async()
    catalog_rows = {row.provider_instrument_id: row for row in catalog.instruments}
    rows: list[ExecutionInstrumentEvidenceV1] = []
    for instrument in sorted(provider.list_all(), key=lambda item: item.id.value):
        native_symbol = instrument.raw_symbol.value
        catalog_row = catalog_rows.get(native_symbol)
        if catalog_row is None:
            continue
        info = instrument.info or {}
        base_currency = instrument.get_base_currency()
        execution_eligible = (
            base_currency is not None
            and isinstance(instrument, CryptoPerpetual)
            and str(info.get("status") or "") == "TRADING"
            and str(info.get("contractType") or "") == "PERPETUAL"
            and not bool(instrument.is_inverse)
        )
        rows.append(
            ExecutionInstrumentEvidenceV1(
                provider_instrument_id=catalog_row.provider_instrument_id,
                catalog_raw_metadata_sha256=catalog_row.raw_metadata_sha256,
                instrument_id=instrument.id.value,
                native_symbol=native_symbol,
                price_precision=instrument.price_precision,
                size_precision=instrument.size_precision,
                price_increment=str(instrument.price_increment.as_decimal()),
                size_increment=str(instrument.size_increment.as_decimal()),
                min_quantity=_optional_decimal(instrument.min_quantity),
                min_notional=_optional_decimal(instrument.min_notional),
                execution_eligible=execution_eligible,
                protection_eligible=_supports_native_stop(info),
                error=None if base_currency is not None else "provider_parse_failed",
            )
        )
    return rows


async def load_hyperliquid_perp_execution_evidence(
    catalog: VenueInstrumentCatalogSnapshotV1,
) -> list[ExecutionInstrumentEvidenceV1]:
    """Load exact mainnet core/HIP-3 client facts for one public catalogue snapshot."""

    if catalog.binding != "HYPERLIQUID_PERP" or catalog.venue != "hyperliquid.perp":
        raise ValueError("hyperliquid_capability_catalog_binding_invalid")
    provider = get_cached_hyperliquid_instrument_provider(
        client=get_cached_hyperliquid_http_client(),
        config=InstrumentProviderConfig(load_all=True, log_warnings=True),
        product_types=(HyperliquidProductType.PERP, HyperliquidProductType.PERP_HIP3),
    )
    await provider.load_all_async()
    instruments = {item.raw_symbol.value: item for item in provider.list_all()}
    rows: list[ExecutionInstrumentEvidenceV1] = []
    for catalog_row in catalog.instruments:
        instrument = instruments.get(_hyperliquid_client_symbol(catalog_row))
        if instrument is None:
            continue
        base_currency = instrument.get_base_currency()
        eligible = isinstance(instrument, CryptoPerpetual) and base_currency is not None
        rows.append(
            ExecutionInstrumentEvidenceV1(
                provider_instrument_id=catalog_row.provider_instrument_id,
                catalog_raw_metadata_sha256=catalog_row.raw_metadata_sha256,
                instrument_id=instrument.id.value,
                native_symbol=catalog_row.provider_symbol,
                price_precision=instrument.price_precision,
                size_precision=instrument.size_precision,
                price_increment=str(instrument.price_increment.as_decimal()),
                size_increment=str(instrument.size_increment.as_decimal()),
                min_quantity=_optional_decimal(instrument.min_quantity),
                min_notional=_optional_decimal(instrument.min_notional),
                execution_eligible=eligible,
                protection_eligible=eligible,
                error=None if eligible else "provider_parse_failed",
            )
        )
    return rows


def _hyperliquid_client_symbol(row: Any) -> str:
    namespace = str(row.canonical_namespace or "")
    symbol = str(row.provider_symbol)
    if namespace == "main":
        return symbol
    if namespace.startswith("dex:"):
        dex = namespace.split(":", 1)[1]
        prefix = f"{dex.upper()}-"
        coin = symbol[len(prefix) :] if symbol.startswith(prefix) else symbol
        return f"{dex}:{coin}"
    return symbol


def _supports_native_stop(info: dict[str, Any]) -> bool:
    order_types = info.get("orderTypes")
    if not isinstance(order_types, list | tuple):
        return False
    return "STOP_MARKET" in {str(value) for value in order_types}


def instrument_matches_capability(
    instrument: CryptoPerpetual,
    capability: ExecutionInstrumentCapabilityV2,
) -> bool:
    """Revalidate every frozen client fact used for capital admission."""

    if capability.binding == "HYPERLIQUID_PERP":
        native = _hyperliquid_client_symbol(capability)
        base = instrument.base_currency.code.split(":", 1)[-1]
        return (
            capability.venue == "hyperliquid.perp"
            and instrument.id.value == capability.instrument_id
            and instrument.raw_symbol.value == native
            and base == capability.canonical_asset
            and instrument.quote_currency.code == "USD"
            and instrument.price_precision == capability.price_precision
            and instrument.size_precision == capability.size_precision
            and str(instrument.price_increment.as_decimal()) == capability.price_increment
            and str(instrument.size_increment.as_decimal()) == capability.size_increment
            and _optional_decimal(instrument.min_quantity) == capability.min_quantity
            and _optional_decimal(instrument.min_notional) == capability.min_notional
            and capability.protection_eligible
        )
    info = instrument.info or {}
    return (
        capability.binding == "BINANCE_USDM"
        and capability.venue == "binance.usdm"
        and instrument.id.value == capability.instrument_id
        and instrument.raw_symbol.value == capability.native_symbol
        and instrument.base_currency.code == capability.canonical_asset
        and instrument.quote_currency.code == capability.settlement_asset
        and str(info.get("status") or "") == "TRADING"
        and str(info.get("contractType") or "") == "PERPETUAL"
        and not instrument.is_inverse
        and instrument.price_precision == capability.price_precision
        and instrument.size_precision == capability.size_precision
        and str(instrument.price_increment.as_decimal()) == capability.price_increment
        and str(instrument.size_increment.as_decimal()) == capability.size_increment
        and _optional_decimal(instrument.min_quantity) == capability.min_quantity
        and _optional_decimal(instrument.min_notional) == capability.min_notional
        and _supports_native_stop(info)
        and capability.protection_eligible
    )


def _optional_decimal(value: Any | None) -> str | None:
    return None if value is None else str(value.as_decimal())


__all__ = [
    "instrument_matches_capability",
    "load_binance_usdm_execution_evidence",
    "load_hyperliquid_perp_execution_evidence",
]
