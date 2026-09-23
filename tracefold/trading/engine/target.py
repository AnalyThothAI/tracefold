"""Select exactly one executable economic asset from a frozen source fact."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tracefold.platform.market_identity import AssetId, AssetRegistry, InstrumentRef, UniversePolicy

TriggerKind = Literal["catalyst", "oi"]
SelectionReason = Literal[
    "selected",
    "no_eligible_primary",
    "excluded_asset",
    "focus_ambiguous",
    "asset_unknown",
    "asset_ambiguous",
    "instrument_unmapped",
]


@dataclass(frozen=True, slots=True)
class SourceAsset:
    symbol: str
    market_type: str
    role: str


@dataclass(frozen=True, slots=True)
class TargetSelection:
    reason: SelectionReason
    asset_id: AssetId | None
    instrument: InstrumentRef | None
    candidates: tuple[str, ...]
    registry_snapshot_ref: str
    version: str = "target_selector_v1"


def select_target(
    *,
    kind: TriggerKind,
    assets: tuple[SourceAsset, ...],
    registry: AssetRegistry,
    universe: UniversePolicy,
    execution_environment: str = "live",
) -> TargetSelection:
    """Mentioned assets never replace a primary; order has no decision meaning."""

    selected = (
        assets
        if kind == "oi"
        else tuple(
            asset for asset in assets if asset.role == "primary" and asset.market_type in ("crypto", "commodity")
        )
    )
    if not selected:
        return TargetSelection("no_eligible_primary", None, None, (), registry.snapshot_ref)
    resolved = [
        registry.resolve(
            asset.symbol,
            "commodity" if asset.market_type == "commodity" else "crypto",
            execution_environment=execution_environment,
        )
        for asset in selected
    ]
    candidates = tuple(sorted({r.asset_id.key if r.asset_id else f"{r.status}:{r.raw_symbol}" for r in resolved}))
    # Unknown and ambiguous primary identities can hide another eligible asset.
    # Terminate by name; never guess or silently pick the first element.
    if any(r.status == "ambiguous" and r.asset_id is None for r in resolved):
        return TargetSelection("asset_ambiguous", None, None, candidates, registry.snapshot_ref)
    if any(r.asset_id is None for r in resolved):
        return TargetSelection("asset_unknown", None, None, candidates, registry.snapshot_ref)
    eligible = {r.asset_id: r for r in resolved if r.asset_id is not None and universe.permits(r.asset_id)}
    if not eligible:
        reason: SelectionReason = (
            "excluded_asset"
            if any(r.asset_id in universe.excluded_asset_ids for r in resolved)
            else "no_eligible_primary"
        )
        return TargetSelection(reason, None, None, candidates, registry.snapshot_ref)
    if len(eligible) > 1:
        return TargetSelection("focus_ambiguous", None, None, candidates, registry.snapshot_ref)
    asset_id, target = next(iter(eligible.items()))
    if target.instrument is None or target.status != "resolved":
        return TargetSelection("instrument_unmapped", asset_id, None, candidates, registry.snapshot_ref)
    return TargetSelection("selected", asset_id, target.instrument, candidates, registry.snapshot_ref)
