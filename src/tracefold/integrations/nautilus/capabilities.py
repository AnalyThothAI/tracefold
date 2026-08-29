"""Public, credential-free Binance Demo capability discovery."""

from __future__ import annotations

from typing import Any

from nautilus_trader.adapters.binance.common.enums import BinanceAccountType, BinanceEnvironment
from nautilus_trader.adapters.binance.common.urls import get_http_base_url
from nautilus_trader.adapters.binance.futures.providers import BinanceFuturesInstrumentProvider
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.common.component import LiveClock
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.instruments.crypto_perpetual import CryptoPerpetual

from tracefold.trading import ExecutionInstrumentCapabilityV1, ProviderInstrumentCandidateV1


async def load_binance_usdm_demo_capabilities() -> list[ProviderInstrumentCandidateV1]:
    """Load every instrument through the pinned official v1 provider, without credentials."""

    account_type = BinanceAccountType.USDT_FUTURES
    clock = LiveClock()
    client = BinanceHttpClient(
        clock=clock,
        api_key=None,
        api_secret=None,
        base_url=get_http_base_url(account_type, BinanceEnvironment.DEMO, is_us=False),
    )
    provider = BinanceFuturesInstrumentProvider(
        client=client,
        clock=clock,
        account_type=account_type,
        config=InstrumentProviderConfig(load_all=True, log_warnings=True),
    )
    await provider.load_all_async()
    rows: list[ProviderInstrumentCandidateV1] = []
    for instrument in sorted(provider.list_all(), key=lambda item: item.id.value):
        info = instrument.info or {}
        base_currency = instrument.get_base_currency()
        rows.append(
            ProviderInstrumentCandidateV1(
                instrument_id=instrument.id.value,
                native_symbol=instrument.raw_symbol.value,
                base_currency="" if base_currency is None else base_currency.code,
                quote_currency=instrument.quote_currency.code,
                active=str(info.get("status") or "") == "TRADING",
                linear=not bool(instrument.is_inverse),
                inverse=bool(instrument.is_inverse),
                perpetual=isinstance(instrument, CryptoPerpetual)
                and str(info.get("contractType") or "") == "PERPETUAL",
                price_precision=instrument.price_precision,
                size_precision=instrument.size_precision,
                price_increment=str(instrument.price_increment.as_decimal()),
                size_increment=str(instrument.size_increment.as_decimal()),
                min_quantity=None if instrument.min_quantity is None else str(instrument.min_quantity.as_decimal()),
                min_notional=None if instrument.min_notional is None else str(instrument.min_notional.as_decimal()),
                # Proved from the venue's own product contract, never defaulted (#331 §3). Binance
                # USD-M publishes the order types each symbol accepts; a native stop is the only
                # protection this lane has, so an instrument the exchange has not said accepts
                # `STOP_MARKET` must be excluded rather than admitted on a model default.
                supports_native_stop=_supports_native_stop(info),
                load_error="provider_parse_failed" if base_currency is None else None,
            )
        )
    return rows


def _supports_native_stop(info: dict[str, Any]) -> bool:
    """Whether Binance's own `orderTypes` for this product names a venue-native stop."""

    order_types = info.get("orderTypes")
    if not isinstance(order_types, list | tuple):
        return False
    return "STOP_MARKET" in {str(value) for value in order_types}


def instrument_matches_capability(
    instrument: CryptoPerpetual,
    capability: ExecutionInstrumentCapabilityV1,
) -> bool:
    """Revalidate every frozen provider fact used for capital admission."""

    info = instrument.info or {}
    return (
        instrument.id.value == capability.instrument_id
        and instrument.raw_symbol.value == capability.native_symbol
        and instrument.base_currency.code == capability.underlying_key.removeprefix("crypto:")
        and instrument.quote_currency.code == capability.quote_currency
        and str(info.get("status") or "") == "TRADING"
        and str(info.get("contractType") or "") == "PERPETUAL"
        and not instrument.is_inverse
        and instrument.price_precision == capability.price_precision
        and instrument.size_precision == capability.size_precision
        and str(instrument.price_increment.as_decimal()) == capability.price_increment
        and str(instrument.size_increment.as_decimal()) == capability.size_increment
        and _optional_decimal(instrument.min_quantity) == capability.min_quantity
        and _optional_decimal(instrument.min_notional) == capability.min_notional
        and _supports_native_stop(info) == capability.supports_native_stop
    )


def _optional_decimal(value: Any | None) -> str | None:
    if value is None:
        return None
    return str(value.as_decimal())


__all__ = ["instrument_matches_capability", "load_binance_usdm_demo_capabilities"]
