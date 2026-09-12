"""The single current wallet product: a token's net-buy episode (#641)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

WALLET_PROVIDER: Final = "robinhood_chain"
WALLET_SOURCE_ID: Final = "news-robinhood-chain"
OutcomeHorizon = Literal["15m", "1h", "4h"]
WALLET_OUTCOME_HORIZONS: Final[tuple[tuple[OutcomeHorizon, int], ...]] = (
    ("15m", 900_000),
    ("1h", 3_600_000),
    ("4h", 14_400_000),
)
OUTCOME_MAX_DELAY_MS: Final = 60_000


class NetBuyMember(BaseModel):
    """One address, one window, one arithmetic set, including exclusions."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    wallet: str
    handle: str
    rank_quality: int | None
    roster_version: int | None
    roster_known_at_ms: int | None
    monitoring_from_ms: int | None
    source_closed_trades: int | None
    source_profit_factor: str | None
    buy_usd: Decimal
    sell_usd: Decimal
    net_usd: Decimal | None
    buy_token_raw: str
    sell_token_raw: str
    net_token_raw: str
    unpriced_count: int
    transfer_out_count: int
    qualified: bool
    reasons: tuple[str, ...]


class NetBuyWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    window: Literal["5m", "30m"]
    from_ms: int
    to_ms: int
    required_n: int
    qualified_n: int
    buy_usd: Decimal
    sell_usd: Decimal
    net_usd: Decimal
    matched: bool
    members: tuple[NetBuyMember, ...]


class NetBuySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    chain_id: int
    token: str
    token_symbol: str | None
    token_decimals: int | None
    cutoff_at_ms: int
    cutoff_block: int
    cutoff_log: int
    roster_version: int | None
    min_net_buy_usd: Decimal
    coverage_from_ms: int | None
    coverage_gap_at_ms: int | None
    fast: NetBuyWindow
    slow: NetBuyWindow

    @property
    def matched(self) -> bool:
        return self.fast.matched or self.slow.matched

    @property
    def primary(self) -> NetBuyWindow:
        return self.fast if self.fast.matched else self.slow


@dataclass(frozen=True, slots=True)
class WalletEvent:
    """First trigger is immutable. Only the detector writes the current snapshot."""

    item_id: str
    chain_id: int
    token: str
    token_symbol: str | None
    trigger_tx_hash: str
    event_at_ms: int
    received_at_ms: int
    detected_at_ms: int
    initial_snapshot: NetBuySnapshot
    trigger_max_age_s: int
    notification_eligible: bool
    notification_reason: str | None
    reference_price: Decimal | None = None
    reference_at_ms: int | None = None
    reference_source: str | None = None


@dataclass(frozen=True, slots=True)
class WalletOutcome:
    item_id: str
    horizon: OutcomeHorizon
    price: Decimal | None
    at_ms: int
    source: str
    reference_price: Decimal | None
    reference_at_ms: int | None
    target_at_ms: int
    status: str
    delivery_key: str | None = None
