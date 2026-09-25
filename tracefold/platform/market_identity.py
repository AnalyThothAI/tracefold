"""Pure economic-asset and native-instrument identities for analysis and execution.

A ticker is a claim about an asset, not its identity.  In particular, contract
prefixes are never stripped heuristically: a verified alias must name the
economic asset and its unit multiplier explicitly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

AssetClass = Literal["crypto", "commodity"]
ResolutionStatus = Literal["resolved", "unknown", "ambiguous"]


@dataclass(frozen=True, slots=True)
class AssetId:
    asset_class: AssetClass
    symbol: str

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper() or not self.symbol.isalnum():
            raise ValueError("asset_id_invalid")

    @property
    def key(self) -> str:
        return f"{self.asset_class}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class VerifiedAlias:
    """One reviewed provider spelling; units per native contract are explicit."""

    asset_class: AssetClass
    source_symbol: str
    asset_id: AssetId
    units_per_contract: Decimal = Decimal(1)
    evidence_ref: str = ""
    native_symbol: str | None = None

    def __post_init__(self) -> None:
        if not self.source_symbol or self.units_per_contract <= 0 or not self.evidence_ref or self.native_symbol == "":
            raise ValueError("asset_alias_unverified")


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    venue: str
    environment: str
    product: str
    native_symbol: str
    asset_id: AssetId
    quote_asset: str
    settlement_asset: str
    units_per_contract: Decimal
    price_unit: str
    quantity_unit: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.venue,
                self.environment,
                self.product,
                self.native_symbol,
                self.quote_asset,
                self.settlement_asset,
                self.price_unit,
                self.quantity_unit,
            )
        ):
            raise ValueError("instrument_identity_incomplete")
        if self.units_per_contract <= 0:
            raise ValueError("instrument_multiplier_invalid")

    @property
    def semantics_digest(self) -> str:
        """Unrelated catalogue updates cannot invalidate this contract."""

        payload = (
            self.venue,
            self.product,
            self.native_symbol,
            self.asset_id.key,
            self.quote_asset,
            self.settlement_asset,
            str(self.units_per_contract),
            self.price_unit,
            self.quantity_unit,
        )
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedAsset:
    status: ResolutionStatus
    raw_symbol: str
    asset_class: AssetClass
    asset_id: AssetId | None
    basis: str
    registry_snapshot_ref: str
    instrument: InstrumentRef | None = None


class AssetRegistry:
    """A frozen native catalogue with a small, reviewed alias list."""

    def __init__(
        self,
        *,
        snapshot_ref: str,
        instruments: tuple[InstrumentRef, ...],
        aliases: tuple[VerifiedAlias, ...] = (),
        known_assets: frozenset[AssetId] = frozenset(),
    ) -> None:
        if not snapshot_ref:
            raise ValueError("registry_snapshot_missing")
        self.snapshot_ref = snapshot_ref
        self.instruments = instruments
        self.aliases = aliases
        self.known_assets = known_assets

    def resolve(
        self,
        raw_symbol: str,
        asset_class: AssetClass = "crypto",
        *,
        quote_asset: str = "USDT",
    ) -> ResolvedAsset:
        symbol = raw_symbol.strip().upper()
        aliases = [a for a in self.aliases if a.asset_class == asset_class and a.source_symbol == symbol]
        direct = [
            i
            for i in self.instruments
            if i.asset_id.asset_class == asset_class and symbol in (i.asset_id.symbol, i.native_symbol)
        ]
        asset_ids = (
            {a.asset_id for a in aliases}
            | {i.asset_id for i in direct}
            | {asset for asset in self.known_assets if asset.asset_class == asset_class and asset.symbol == symbol}
        )
        if len(asset_ids) != 1:
            status: ResolutionStatus = "ambiguous" if asset_ids else "unknown"
            return ResolvedAsset(status, raw_symbol, asset_class, None, status, self.snapshot_ref)
        asset_id = next(iter(asset_ids))
        matching_aliases = [a for a in aliases if a.asset_id == asset_id]
        routes = [
            i
            for i in self.instruments
            if i.asset_id == asset_id
            and i.product == "perpetual"
            and i.venue == "binance.usdm"
            and i.quote_asset == quote_asset
            and (
                not matching_aliases
                or any(
                    a.units_per_contract == i.units_per_contract
                    and (a.native_symbol is None or a.native_symbol == i.native_symbol)
                    for a in matching_aliases
                )
            )
        ]
        # Only an explicit native route can reach execution.  Multiple routes are
        # ambiguous until the caller supplies a reviewed preference.
        if len(routes) != 1:
            status = "ambiguous" if routes else "unknown"
            return ResolvedAsset(status, raw_symbol, asset_class, asset_id, "route_" + status, self.snapshot_ref)
        basis = "verified_alias" if aliases else "native_catalogue"
        return ResolvedAsset("resolved", raw_symbol, asset_class, asset_id, basis, self.snapshot_ref, routes[0])


@dataclass(frozen=True, slots=True)
class UniversePolicy:
    version: str
    excluded_asset_ids: frozenset[AssetId]

    def permits(self, asset_id: AssetId) -> bool:
        return asset_id.asset_class == "crypto" and asset_id not in self.excluded_asset_ids

    @property
    def digest(self) -> str:
        payload = [self.version, *sorted(asset.key for asset in self.excluded_asset_ids)]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


DEFAULT_UNIVERSE = UniversePolicy(
    version="universe_v1",
    excluded_asset_ids=frozenset(
        {
            AssetId("commodity", "CL"),
            AssetId("crypto", "BTC"),
            AssetId("crypto", "ETH"),
            AssetId("crypto", "USDT"),
            AssetId("crypto", "USDC"),
        }
    ),
)
