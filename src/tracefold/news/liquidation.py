"""Provider-neutral potential-liquidation snapshots for the bounded shadow plane (#144).

These are model estimates, not executed liquidations. Raw provider side/size codes remain explicitly raw
until an entitled CoinGlass contract defines their business meaning; nothing in this module may turn them
into "long liquidations" or USD amounts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

LIQUIDATION_PROVIDER: Final = "coinglass_web"
LIQUIDATION_CONTRACT: Final = "undocumented_public_web_http"
LIQUIDATION_MODEL_VERSION: Final = "coinglass_liquidation_levels_v2_raw_top64_v1"
LIQUIDATION_RANGE: Final = "3d"
LIQUIDATION_LEVEL_MAX: Final = 64
LIQUIDATION_PERIOD_SECONDS: Final = 60.0
LIQUIDATION_REFRESH_SECONDS: Final = 4 * 60.0
LIQUIDATION_FRESH_MAX_AGE_MS: Final = 10 * 60_000
LIQUIDATION_TURN_DEADLINE_SECONDS: Final = 45.0
LIQUIDATION_TARGET_MAX_PER_TURN: Final = 1

LiquidationFreshness = Literal["fresh", "stale", "unavailable"]


@dataclass(frozen=True, slots=True)
class LiquidationTarget:
    """One exact venue contract; no alias or aggregate-symbol fallback is allowed."""

    venue: str
    venue_symbol: str
    base_symbol: str
    quote_asset: str
    provider_exchange: str


LIQUIDATION_TARGETS: Final = tuple(
    LiquidationTarget(
        venue="binance.perp",
        venue_symbol=f"{symbol}USDT",
        base_symbol=symbol,
        quote_asset="USDT",
        provider_exchange="Binance",
    )
    for symbol in ("BTC", "ETH", "SOL", "DOGE")
)


@dataclass(frozen=True, slots=True)
class LiquidationZone:
    """One strongest raw model level retained from a much larger provider snapshot."""

    price: Decimal
    size: Decimal
    raw_side: int
    model_level: int
    model_level2: str
    begin_at_ms: int
    x: int

    def as_json(self) -> dict[str, Any]:
        return {
            "price": str(self.price),
            "size": str(self.size),
            "raw_side": self.raw_side,
            "model_level": self.model_level,
            "model_level2": self.model_level2,
            "begin_at_ms": self.begin_at_ms,
            "x": self.x,
        }


@dataclass(frozen=True, slots=True)
class ProviderLiquidationSnapshot:
    """One normalized provider attempt, successful or unavailable."""

    target: LiquidationTarget
    provider: str
    contract: str
    authenticated: bool
    completeness: str
    model_version: str
    range_key: str
    zones: tuple[LiquidationZone, ...]
    source_at_ms: int | None
    received_at_ms: int
    freshness: LiquidationFreshness
    degraded: bool
    error_class: str | None
    payload_sha256: str | None
    raw_level_count: int
    raw_price_count: int

    def zones_json(self) -> list[dict[str, Any]]:
        return [zone.as_json() for zone in self.zones]


class LiquidationSnapshotProvider(Protocol):
    """The cold loop depends on this interface, never on CoinGlass response or process details."""

    async def fetch(
        self,
        target: LiquidationTarget,
        *,
        model_version: str,
        range_key: str,
    ) -> ProviderLiquidationSnapshot: ...


def unavailable_snapshot(
    target: LiquidationTarget,
    *,
    received_at_ms: int,
    error_class: str,
    provider: str = LIQUIDATION_PROVIDER,
    contract: str = LIQUIDATION_CONTRACT,
    model_version: str = LIQUIDATION_MODEL_VERSION,
    range_key: str = LIQUIDATION_RANGE,
) -> ProviderLiquidationSnapshot:
    return ProviderLiquidationSnapshot(
        target=target,
        provider=provider,
        contract=contract,
        authenticated=False,
        completeness="unknown",
        model_version=model_version,
        range_key=range_key,
        zones=(),
        source_at_ms=None,
        received_at_ms=int(received_at_ms),
        freshness="unavailable",
        degraded=True,
        error_class=str(error_class or "unavailable"),
        payload_sha256=None,
        raw_level_count=0,
        raw_price_count=0,
    )


def target_key(target: LiquidationTarget) -> tuple[str, str]:
    return target.venue, target.venue_symbol


def target_from_mapping(value: Mapping[str, Any]) -> LiquidationTarget:
    return LiquidationTarget(
        venue=str(value["venue"]),
        venue_symbol=str(value["venue_symbol"]),
        base_symbol=str(value["base_symbol"]),
        quote_asset=str(value["quote_asset"]),
        provider_exchange=str(value["provider_exchange"]),
    )


__all__ = [
    "LIQUIDATION_CONTRACT",
    "LIQUIDATION_FRESH_MAX_AGE_MS",
    "LIQUIDATION_LEVEL_MAX",
    "LIQUIDATION_MODEL_VERSION",
    "LIQUIDATION_PERIOD_SECONDS",
    "LIQUIDATION_PROVIDER",
    "LIQUIDATION_RANGE",
    "LIQUIDATION_REFRESH_SECONDS",
    "LIQUIDATION_TARGETS",
    "LIQUIDATION_TARGET_MAX_PER_TURN",
    "LIQUIDATION_TURN_DEADLINE_SECONDS",
    "LiquidationSnapshotProvider",
    "LiquidationTarget",
    "LiquidationZone",
    "ProviderLiquidationSnapshot",
    "target_from_mapping",
    "target_key",
    "unavailable_snapshot",
]
