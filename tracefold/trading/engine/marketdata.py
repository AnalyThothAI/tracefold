"""Small typed raw-market-data port; profile decisions stay in Trading."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

Dataset = Literal["perp_bars", "spot_bars", "open_interest", "funding_basis", "market_bars"]
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
        if self.dataset in ("perp_bars", "spot_bars", "market_bars"):
            if self.start_ms is None or self.end_ms is None or self.interval_ms is None:
                raise ValueError("market_data_window_incomplete")
            if self.start_ms >= self.end_ms or self.interval_ms <= 0:
                raise ValueError("market_data_window_invalid")
        elif self.max_age_ms is None or self.max_age_ms <= 0:
            raise ValueError("market_data_age_invalid")


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
