"""Closed OpenNews source contracts.

The provider account decides which Strategies are enabled.  This module answers the
smaller, code-owned question of what one normalized frame is and which existing parser
may read it.  A Strategy id alone is never enough.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, NamedTuple

from .models import Admission

SourceContractFamily = Literal["news_v1", "listing_v1", "oi_v1", "liquidation_v1", "unsupported_market"]
EventKind = Literal["news", "listing", "oi", "liquidation", "unsupported_market"]
SourceContractReason = Literal[
    "source_contract_drift",
    "unsupported_market_contract",
]

EVENT_KINDS: Final[tuple[EventKind, ...]] = ("news", "listing", "oi", "liquidation", "unsupported_market")
SOURCE_CONTRACT_FAMILIES: Final[tuple[SourceContractFamily, ...]] = (
    "news_v1",
    "listing_v1",
    "oi_v1",
    "liquidation_v1",
    "unsupported_market",
)
SOURCE_CONTRACT_CLASSIFIER_VERSION: Final = "opennews_source_classifier_v1"


class SourceIdentity(NamedTuple):
    strategy_id: str
    strategy_name: str
    source_type: str
    engine_type: str


OI_SOURCE_IDENTITY: Final = SourceIdentity("1019", "OI Event Monitor", "market", "market")
LISTING_SOURCE_IDENTITY: Final = SourceIdentity("1353", "Listing and Delisting Announcements", "news", "listing")
LIQUIDATION_SOURCE_IDENTITY: Final = SourceIdentity("2000", "实时清算", "market", "market")
SMART_MONEY_SOURCE_IDENTITY: Final = SourceIdentity("2026", "聪明钱监控", "wallet", "market")
LARGE_LIQUIDATION_SOURCE_IDENTITY: Final = SourceIdentity("2083", "Large-scale liquidation", "market", "market")


@dataclass(frozen=True, slots=True)
class SourceContract:
    source_contract_family: SourceContractFamily
    event_kind: EventKind
    identity: SourceIdentity
    reason: SourceContractReason | None = None
    classifier_version: str = SOURCE_CONTRACT_CLASSIFIER_VERSION


_EXACT_CONTRACTS: Final[dict[SourceIdentity, tuple[SourceContractFamily, EventKind, SourceContractReason | None]]] = {
    OI_SOURCE_IDENTITY: ("oi_v1", "oi", None),
    LISTING_SOURCE_IDENTITY: ("listing_v1", "listing", None),
    LIQUIDATION_SOURCE_IDENTITY: ("liquidation_v1", "liquidation", None),
    SMART_MONEY_SOURCE_IDENTITY: (
        "unsupported_market",
        "unsupported_market",
        "unsupported_market_contract",
    ),
    LARGE_LIQUIDATION_SOURCE_IDENTITY: (
        "unsupported_market",
        "unsupported_market",
        "unsupported_market_contract",
    ),
}
_BOUND_STRATEGY_IDS: Final = frozenset(identity.strategy_id for identity in _EXACT_CONTRACTS)


def source_identity(provider_metadata: Any) -> SourceIdentity:
    strategies = provider_metadata.get("strategies") if isinstance(provider_metadata, Mapping) else None
    first = strategies[0] if isinstance(strategies, list | tuple) and strategies else None
    if not isinstance(first, Mapping):
        return SourceIdentity("", "", "", "")
    return SourceIdentity(
        str(first.get("id") or ""),
        str(first.get("name") or ""),
        str(first.get("source_type") or ""),
        str(first.get("engine_type") or ""),
    )


def classify_source_contract(provider_metadata: Any) -> SourceContract:
    """Classify one adapter-normalized frame without I/O or title inference."""

    identity = source_identity(provider_metadata)
    exact = _EXACT_CONTRACTS.get(identity)
    if exact is not None:
        source_contract_family, event_kind, reason = exact
        return SourceContract(
            source_contract_family=source_contract_family,
            event_kind=event_kind,
            identity=identity,
            reason=reason,
        )

    # A provider handle already bound to a contract may not silently change meaning.
    if identity.strategy_id in _BOUND_STRATEGY_IDS:
        return SourceContract(
            source_contract_family="unsupported_market",
            event_kind="unsupported_market",
            identity=identity,
            reason="source_contract_drift",
        )

    # Listing remains a generic Program route for every provider-enabled listing Strategy.
    if identity.engine_type == "listing":
        return SourceContract(source_contract_family="listing_v1", event_kind="listing", identity=identity)

    has_score = (
        isinstance(provider_metadata, Mapping)
        and isinstance(provider_metadata.get("score"), int | float)
        and not isinstance(provider_metadata.get("score"), bool)
    )
    if not has_score and (identity.source_type in {"market", "wallet"} or identity.engine_type == "market"):
        return SourceContract(
            source_contract_family="unsupported_market",
            event_kind="unsupported_market",
            identity=identity,
            reason="unsupported_market_contract",
        )
    return SourceContract(source_contract_family="news_v1", event_kind="news", identity=identity)


def classify_source_contracts(provider_metadata: Any) -> tuple[SourceContract, ...]:
    """Classify every durable Strategy tuple on one Item, preserving first-seen order."""

    strategies = provider_metadata.get("strategies") if isinstance(provider_metadata, Mapping) else None
    if not isinstance(strategies, list | tuple):
        return (classify_source_contract(provider_metadata),)
    contracts: list[SourceContract] = []
    seen: set[SourceIdentity] = set()
    for strategy in strategies:
        if not isinstance(strategy, Mapping):
            continue
        view = {**provider_metadata, "strategies": [strategy]}
        contract = classify_source_contract(view)
        if contract.identity not in seen:
            seen.add(contract.identity)
            contracts.append(contract)
    return tuple(contracts) or (classify_source_contract(provider_metadata),)


def source_contract_admission(
    contract: SourceContract,
    *,
    generic_admission: Admission,
    ingest_mode: str,
) -> Admission:
    """Compose the source contract with the unchanged generic Gate result."""

    if contract.source_contract_family == "unsupported_market":
        return "unsupported_market_contract"
    if ingest_mode == "recovery":
        return "recovery"
    if contract.source_contract_family == "oi_v1":
        return "telemetry_deterministic"
    if contract.source_contract_family == "liquidation_v1":
        return "liquidation_deterministic"
    if contract.source_contract_family == "listing_v1":
        return "listing_deterministic"
    return generic_admission


__all__ = [
    "EVENT_KINDS",
    "LARGE_LIQUIDATION_SOURCE_IDENTITY",
    "LIQUIDATION_SOURCE_IDENTITY",
    "LISTING_SOURCE_IDENTITY",
    "OI_SOURCE_IDENTITY",
    "SMART_MONEY_SOURCE_IDENTITY",
    "SOURCE_CONTRACT_CLASSIFIER_VERSION",
    "SOURCE_CONTRACT_FAMILIES",
    "EventKind",
    "SourceContract",
    "SourceContractFamily",
    "SourceContractReason",
    "SourceIdentity",
    "classify_source_contract",
    "classify_source_contracts",
    "source_contract_admission",
    "source_identity",
]
