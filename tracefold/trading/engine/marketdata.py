"""Small typed raw-market-data port; profile decisions stay in Trading."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Dataset = Literal[
    "perp_bars",
    "spot_bars",
    "open_interest",
    "open_interest_history",
    "funding_basis",
    "market_bars",
    "book_ticker",
    "mark_bars",
    "funding_history",
    "instrument_rules",
]
DataStatus = Literal["ok", "partial", "stale", "missing", "error", "not_applicable"]


@dataclass(frozen=True, slots=True)
class MarketDataRequest:
    dataset: Dataset
    native_symbol: str
    venue: str
    environment: str
    product: str
    source_identity: str
    unit_definition: str
    start_ms: int | None
    end_ms: int | None
    interval_ms: int | None
    max_age_ms: int | None
    deadline_at_monotonic: float

    def __post_init__(self) -> None:
        if not all(
            (self.native_symbol, self.venue, self.environment, self.product, self.source_identity, self.unit_definition)
        ):
            raise ValueError("market_data_identity_incomplete")
        if self.dataset in ("perp_bars", "spot_bars", "market_bars", "mark_bars"):
            if self.start_ms is None or self.end_ms is None or self.interval_ms is None:
                raise ValueError("market_data_window_incomplete")
            if self.start_ms >= self.end_ms or self.interval_ms <= 0:
                raise ValueError("market_data_window_invalid")
        elif self.dataset in ("funding_history", "open_interest_history"):
            if self.start_ms is None or self.end_ms is None or self.start_ms >= self.end_ms:
                raise ValueError("market_data_window_invalid")
            if self.dataset == "open_interest_history" and self.interval_ms not in (
                300_000,
                900_000,
                1_800_000,
                3_600_000,
                7_200_000,
                14_400_000,
                21_600_000,
                43_200_000,
                86_400_000,
            ):
                raise ValueError("open_interest_interval_unsupported")
        elif self.max_age_ms is None or self.max_age_ms <= 0:
            raise ValueError("market_data_age_invalid")


def analysis_market_request(
    *,
    dataset: Dataset,
    native_symbol: str,
    end_ms: int | None,
    window_minutes: int | None,
    deadline_at_monotonic: float,
) -> MarketDataRequest:
    """One Trading Analysis request identity for frozen and on-demand observations."""
    if dataset not in (
        "perp_bars",
        "spot_bars",
        "market_bars",
        "open_interest_history",
        "open_interest",
        "funding_basis",
        "instrument_rules",
    ):
        raise ValueError("analysis_market_dataset_invalid")
    bars = dataset in ("perp_bars", "spot_bars", "market_bars")
    history = dataset == "open_interest_history"
    interval = 60_000 if bars else 300_000 if history else None
    if bars and (end_ms is None or window_minutes is None):
        raise ValueError("market_data_window_incomplete")
    if history and (end_ms is None or window_minutes is None):
        raise ValueError("market_data_window_incomplete")
    start = (
        end_ms - (window_minutes + 1) * 60_000
        if bars and end_ms is not None and window_minutes is not None
        else end_ms - window_minutes * 60_000
        if history and end_ms is not None and window_minutes is not None
        else None
    )
    unit = (
        "base_quantity_and_quote_value_v1"
        if history
        else "native_contract_quantity_v1"
        if dataset == "open_interest"
        else "quote_price_and_rate_v1"
        if dataset == "funding_basis"
        else "binance_usdm_contract_rules_v1"
        if dataset == "instrument_rules"
        else "quote_per_base_and_volume_v1"
    )
    return MarketDataRequest(
        dataset=dataset,
        native_symbol="BTCUSDT" if dataset == "market_bars" else native_symbol,
        venue="binance.usdm",
        environment="live",
        product="spot" if dataset == "spot_bars" else "perpetual",
        source_identity="binance_public_v1",
        unit_definition=unit,
        start_ms=start,
        end_ms=end_ms if bars or history else None,
        interval_ms=interval,
        max_age_ms=None if bars or history else 3_600_000 if dataset == "instrument_rules" else 90_000,
        deadline_at_monotonic=deadline_at_monotonic,
    )


@dataclass(frozen=True, slots=True)
class MarketDataResult:
    status: DataStatus
    payload: tuple[dict[str, Any], ...]
    schema_version: str
    source_version: str
    unit_definition: str
    source_identity: str
    event_start_ms: int | None
    event_end_ms: int | None
    received_at_ms: int | None
    missing_reasons: tuple[str, ...]
    request_receipts: tuple[dict[str, Any], ...]


class MarketDataPort(Protocol):
    async def fetch(self, request: MarketDataRequest) -> MarketDataResult: ...
