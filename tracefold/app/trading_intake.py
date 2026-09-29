"""Relay News public Trading facts and select one LIVE asset."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from tracefold.news.updates.contracts import PublicUpdate
from tracefold.platform.market_identity import AssetId, AssetRegistry, InstrumentRef, UniversePolicy, VerifiedAlias
from tracefold.trading.engine.features import CATALYST_SOURCE_KIND
from tracefold.trading.engine.marketdata import MarketDataPort, MarketDataRequest
from tracefold.trading.engine.target import SourceAsset, TargetSelection, TriggerKind, select_target

_MAX_SOURCE_ASSETS = 8


def _assets(payload: dict[str, Any]) -> tuple[SourceAsset, ...]:
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError("trade_event_assets_invalid")
    if len(raw_assets) > _MAX_SOURCE_ASSETS:
        raise ValueError("trade_event_assets_oversized")
    return tuple(SourceAsset(str(item["symbol"]), str(item["market_type"]), str(item["role"])) for item in raw_assets)


def public_update(event: dict[str, Any]) -> PublicUpdate:
    """Map one News outbox row to its exact public contract."""

    payload = event["payload"]
    update = PublicUpdate.model_validate(payload)
    expected = "source_update" if event["kind"] == "source_update" else CATALYST_SOURCE_KIND
    if (
        update.kind != expected
        or update.event_id != event["source_fact_key"]
        or update.content_revision != event["source_revision"]
    ):
        raise ValueError("trade_event_public_identity_mismatch")
    return update


def catalyst_assets(update: PublicUpdate) -> tuple[SourceAsset, ...]:
    """The union of primary assets over the claims this delta changed.

    A mentioned asset never becomes a target; several eligible primaries stay ambiguous
    in target selection rather than fanning out one Case per claim.
    """

    assets = sorted(
        {
            (asset.symbol, asset.market_type)
            for claim in update.claims
            for asset in claim.fields.assets
            if asset.role == "primary"
        }
    )
    if len(assets) > _MAX_SOURCE_ASSETS:
        raise ValueError("trade_event_assets_oversized")
    return tuple(SourceAsset(symbol, market_type, "primary") for symbol, market_type in assets)


def _registry_from_rows(
    rows: list[dict[str, Any]],
    *,
    environment: str,
    universe: UniversePolicy,
    verified_routes: list[Any],
) -> AssetRegistry:
    instruments: list[InstrumentRef] = []
    verified = {route.native_symbol: route for route in verified_routes}
    aliases = []
    for row in rows:
        if "venue" in row and (row["venue"] != "binance.perp" or row["instrument_class"] != "crypto"):
            continue
        if row.get("trading_status", "TRADING") != "TRADING" or row.get("contract_type", "PERPETUAL") != "PERPETUAL":
            continue
        native = str(row.get("native_symbol", row.get("venue_symbol", "")))
        base = str(row.get("base_asset", row.get("base_symbol", "")))
        quote = str(row["quote_asset"])
        if quote != "USDT" or row.get("settlement_asset", quote) != "USDT":
            continue
        reviewed = verified.get(native)
        if reviewed is None and (not base or base[0].isdigit() or native != base + quote):
            # A multiplier or a nonstandard native spelling needs reviewed
            # asset and unit semantics before any executable target exists.
            continue
        try:
            asset_id = (
                AssetId("crypto", reviewed.asset_id.split(":", 1)[1])
                if reviewed is not None
                else AssetId("crypto", base)
            )
        except ValueError:
            continue
        instruments.append(
            InstrumentRef(
                venue="binance.usdm",
                environment=environment,
                product="perpetual",
                native_symbol=native,
                asset_id=asset_id,
                quote_asset=quote,
                settlement_asset=quote,
                units_per_contract=reviewed.units_per_contract if reviewed is not None else Decimal(1),
                price_unit="native_quote",
                quantity_unit="native_base",
            )
        )
        if reviewed is not None:
            aliases.append(
                VerifiedAlias(
                    "crypto",
                    reviewed.source_symbol,
                    asset_id,
                    reviewed.units_per_contract,
                    reviewed.evidence_ref,
                    native,
                )
            )
    snapshot = str(max((int(row.get("received_at_ms", row.get("observed_at_ms", 0))) for row in rows), default=0))
    return AssetRegistry(
        snapshot_ref=f"binance_connection_catalogue:{environment}:{snapshot}",
        instruments=tuple(instruments),
        aliases=tuple(aliases),
        known_assets=universe.excluded_asset_ids,
    )


async def _select_from_connection(
    market_data: MarketDataPort,
    kind: TriggerKind,
    assets: tuple[SourceAsset, ...],
    *,
    environment: str,
    universe: UniversePolicy,
    verified_routes: list[Any],
) -> TargetSelection:
    symbols = {asset.symbol for asset in assets if asset.role == "primary"}
    for route in verified_routes:
        if route.source_symbol in symbols:
            symbols.add(route.native_symbol[:-4])
    native_symbols: set[str] = set()
    for symbol in sorted(symbols):
        native_symbols.add(symbol if symbol.endswith("USDT") else symbol + "USDT")
    native_symbols.update(route.native_symbol for route in verified_routes if route.source_symbol in symbols)
    deadline = time.monotonic() + 5.0
    requests = tuple(
        MarketDataRequest(
            dataset="instrument_rules",
            native_symbol=native,
            venue="binance.usdm",
            environment=environment,
            product="perpetual",
            source_identity="binance_public_v1",
            unit_definition="binance_usdm_contract_rules_v1",
            start_ms=None,
            end_ms=None,
            interval_ms=None,
            max_age_ms=3_600_000,
            deadline_at_monotonic=deadline,
        )
        for native in sorted(native_symbols)
    )
    results = await asyncio.gather(*(market_data.fetch(request) for request in requests))
    if any(result.status in ("error", "stale") for result in results):
        raise TimeoutError("connection_catalogue_unavailable")
    rows = [dict(result.payload[0]) for result in results if result.status == "ok" and result.payload]
    registry = _registry_from_rows(rows, environment=environment, universe=universe, verified_routes=verified_routes)
    return select_target(kind=kind, assets=assets, registry=registry, universe=universe)


def _configured_universe(settings: Any) -> UniversePolicy:
    exclusions = []
    for value in settings.trading.analysis.excluded_asset_ids:
        category, symbol = value.split(":", 1)
        exclusions.append(AssetId(category, symbol))
    return UniversePolicy(version="universe_v1", excluded_asset_ids=frozenset(exclusions))
