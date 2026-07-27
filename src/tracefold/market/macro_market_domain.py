from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

MarketTrustTier = Literal["official", "exchange", "untrusted_proxy"]


class GeneralMarketInstrumentSpec(Protocol):
    instrument_id: str | None
    symbol: str | None
    series_id: str
    instrument_name: str | None
    label: str
    asset_class: str | None
    instrument_type: str | None
    venue: str | None
    currency: str
    unit: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MarketObservationFact:
    dataset_id: str
    instrument_id: str
    source_id: str
    field_name: str
    value_numeric: float
    unit: str
    observed_at_ms: int
    published_at_ms: int | None
    received_at_ms: int
    trust_tier: MarketTrustTier
    source_url: str
    raw_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MarketSettlementFact:
    dataset_id: str
    instrument_id: str
    source_id: str
    trade_date: date
    contract_code: str
    settlement_price: float
    open_interest: float | None
    volume: float | None
    unit: str
    published_at_ms: int | None
    received_at_ms: int
    source_url: str
    raw_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MarketPositionFact:
    dataset_id: str
    contract_code: str
    contract_name: str
    report_date: date
    open_interest: float
    leveraged_long: float
    leveraged_short: float
    leveraged_net_pct_oi: float
    asset_manager_net_pct_oi: float
    dealer_net_pct_oi: float
    published_at_ms: int | None
    received_at_ms: int
    source_url: str
    raw_data: dict[str, Any]


__all__ = [
    "GeneralMarketInstrumentSpec",
    "MarketObservationFact",
    "MarketPositionFact",
    "MarketSettlementFact",
    "MarketTrustTier",
]
