from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OkxDexTokenCandidate:
    chain_index: str
    chain: str | None
    address: str
    symbol: str
    name: str | None
    price_usd: float | None
    market_cap_usd: float | None
    liquidity_usd: float | None
    holders: int | None
    community_recognized: bool | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OkxDexTokenPrice:
    chain_index: str
    address: str
    observed_at_ms: int
    price_usd: float | None
    market_cap_usd: float | None
    liquidity_usd: float | None
    holders: int | None
    raw: dict[str, Any]
