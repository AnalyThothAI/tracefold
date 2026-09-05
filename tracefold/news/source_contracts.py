"""Closed OpenNews source contracts.

The provider account decides which Strategies are enabled.  This module answers the
smaller, code-owned question of what one normalized frame is and which existing parser
may read it.  A Strategy id alone is never enough -- except for the market Strategies,
where it is the *only* thing that is enough.

Two vocabularies live here and they are not the same list (#553). ``EventKind`` names what an
editorial Event can be: `news` and `listing`, the two families that reach the model policy, the
storyline and the reader card. ``MarketKind`` names what a market observation is: `oi`,
`liquidation`, `smart_money` and `unknown_market`, which are persisted as typed facts beside their
Item and never become an Event at all. A frame is one or the other, never both.

Market families are keyed on the provider's Strategy id alone. The four-tuple binding they used to
carry made a *display name* load-bearing: when the provider renamed `Large-scale liquidation`, every
frame under that id fell out of its own contract and was recorded as drift. A rename is a fact about
the provider's console, not about what the frame measures.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, NamedTuple

from .models import Admission

SourceContractFamily = Literal[
    "news_v1",
    "listing_v1",
    "oi_v1",
    "liquidation_v1",
    "smart_money_v1",
    "unknown_market",
]
EventKind = Literal["news", "listing"]
MarketKind = Literal["oi", "liquidation", "smart_money", "unknown_market"]

EVENT_KINDS: Final[tuple[EventKind, ...]] = ("news", "listing")
EVENT_SOURCE_CONTRACT_FAMILIES: Final[tuple[SourceContractFamily, ...]] = ("news_v1", "listing_v1")
MARKET_KINDS: Final[tuple[MarketKind, ...]] = ("oi", "liquidation", "smart_money", "unknown_market")
MARKET_SOURCE_CONTRACT_FAMILIES: Final[tuple[SourceContractFamily, ...]] = (
    "oi_v1",
    "liquidation_v1",
    "smart_money_v1",
    "unknown_market",
)
SOURCE_CONTRACT_FAMILIES: Final[tuple[SourceContractFamily, ...]] = (
    *EVENT_SOURCE_CONTRACT_FAMILIES,
    *MARKET_SOURCE_CONTRACT_FAMILIES,
)
# v2: market families key on the Strategy id, `smart_money_v1` exists, and `unsupported_market` is
# gone as an outcome -- an unbound market Strategy is stored as `unknown_market`, not refused.
SOURCE_CONTRACT_CLASSIFIER_VERSION: Final = "opennews_source_classifier_v2"

# The single upstream every market observation in this repository comes from. It is a stored column
# rather than an implied constant because the group key a notification merges on must be readable
# from the row, and a second provider would otherwise merge silently into the first one's groups.
MARKET_PROVIDER: Final = "opennews"

# Reasons a classified market frame carries no typed row. `MARKET_CATEGORY_CONFLICT` is the one where
# the frame named two different market families at once, so no single set of numeric semantics could
# be applied without inventing one; the parsers own the rest.
MARKET_CATEGORY_CONFLICT: Final = "market_category_conflict"
# A market Strategy with no template in this repository. The frame is stored and readable; what is
# absent is a typed row, not the observation.
UNKNOWN_MARKET_SOURCE: Final = "unknown_market_source"


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

# Strategy id -> market family. The id is the provider's own primary key for the Strategy; the name,
# source type and engine type are recorded on the fact and never gate it.
MARKET_STRATEGY_FAMILIES: Final[dict[str, SourceContractFamily]] = {
    OI_SOURCE_IDENTITY.strategy_id: "oi_v1",
    LIQUIDATION_SOURCE_IDENTITY.strategy_id: "liquidation_v1",
    LARGE_LIQUIDATION_SOURCE_IDENTITY.strategy_id: "liquidation_v1",
    SMART_MONEY_SOURCE_IDENTITY.strategy_id: "smart_money_v1",
}
_FAMILY_MARKET_KIND: Final[dict[SourceContractFamily, MarketKind]] = {
    "oi_v1": "oi",
    "liquidation_v1": "liquidation",
    "smart_money_v1": "smart_money",
    "unknown_market": "unknown_market",
}


@dataclass(frozen=True, slots=True)
class SourceContract:
    """One frame's proven route. Exactly one of ``event_kind`` and ``market_kind`` is set."""

    source_contract_family: SourceContractFamily
    identity: SourceIdentity
    event_kind: EventKind | None = None
    market_kind: MarketKind | None = None
    classifier_version: str = SOURCE_CONTRACT_CLASSIFIER_VERSION

    @property
    def is_market(self) -> bool:
        return self.market_kind is not None


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


def _market_contract(family: SourceContractFamily, identity: SourceIdentity) -> SourceContract:
    return SourceContract(
        source_contract_family=family,
        identity=identity,
        market_kind=_FAMILY_MARKET_KIND[family],
    )


def classify_source_contract(provider_metadata: Any) -> SourceContract:
    """Classify one adapter-normalized frame without I/O or title inference."""

    identity = source_identity(provider_metadata)
    market_family = MARKET_STRATEGY_FAMILIES.get(identity.strategy_id)
    if market_family is not None:
        return _market_contract(market_family, identity)

    # Listing stays a generic Program route for every provider-enabled listing Strategy. The exact
    # tuple below is the one the fixtures were captured under; a drifted name still lands on the same
    # family through the generic `engine_type` branch rather than falling out of its contract.
    if identity == LISTING_SOURCE_IDENTITY or identity.engine_type == "listing":
        return SourceContract(source_contract_family="listing_v1", identity=identity, event_kind="listing")

    has_score = (
        isinstance(provider_metadata, Mapping)
        and isinstance(provider_metadata.get("score"), int | float)
        and not isinstance(provider_metadata.get("score"), bool)
    )
    if not has_score and (identity.source_type in {"market", "wallet"} or identity.engine_type == "market"):
        # A market Strategy this code has no template for. It is stored and read as its own raw card;
        # it is never sent to the model wearing a news costume.
        return _market_contract("unknown_market", identity)
    return SourceContract(source_contract_family="news_v1", identity=identity, event_kind="news")


def classify_source_contracts(provider_metadata: Any) -> tuple[SourceContract, ...]:
    """Classify every durable Strategy tuple on one frame, preserving first-seen order."""

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


def market_route(contracts: tuple[SourceContract, ...]) -> tuple[MarketKind, str | None] | None:
    """The market kind one frame routes to, or ``None`` when it stays on the ordinary News branch.

    One rule, and it is the rule the backfill in `20260905_0365` applies too, so a frame admitted
    live and the same frame classified by the migration cannot disagree about what it is.

    The frame's *primary* Strategy decides the branch. An Item that has accumulated a market Strategy
    across replays does not drag an already-classified news frame into the market plane, and a market
    frame is not taken away from its parser because a second tuple on it happens to be news --
    additional non-market Strategies are metadata about the record, not a second reading of it.

    The one thing that does change the answer is two different *market* families on one frame: no
    single set of numeric semantics can be applied without inventing one, so the observation is stored
    as `unknown_market` with an explicit reason. Reinterpreting one family's numbers under another's
    is what this function must never do quietly.
    """

    if not contracts or not contracts[0].is_market:
        return None
    families = {contract.source_contract_family for contract in contracts if contract.is_market}
    if len(families) > 1:
        return "unknown_market", MARKET_CATEGORY_CONFLICT
    # Read back through the family map rather than off the contract, so the kind is proven by the same
    # table that assigned it instead of by a narrowing the type checker has to be told about.
    return _FAMILY_MARKET_KIND[contracts[0].source_contract_family], None


def source_contract_admission(
    contract: SourceContract,
    *,
    generic_admission: Admission,
    ingest_mode: str,
) -> Admission:
    """Compose the source contract with the unchanged generic Gate result.

    Market frames never reach this function: they do not open an Event, so they have no admission.
    """

    if ingest_mode == "recovery":
        return "recovery"
    if contract.source_contract_family == "listing_v1":
        return "listing_deterministic"
    return generic_admission


__all__ = [
    "EVENT_KINDS",
    "EVENT_SOURCE_CONTRACT_FAMILIES",
    "LARGE_LIQUIDATION_SOURCE_IDENTITY",
    "LIQUIDATION_SOURCE_IDENTITY",
    "LISTING_SOURCE_IDENTITY",
    "MARKET_CATEGORY_CONFLICT",
    "MARKET_KINDS",
    "MARKET_PROVIDER",
    "MARKET_SOURCE_CONTRACT_FAMILIES",
    "MARKET_STRATEGY_FAMILIES",
    "OI_SOURCE_IDENTITY",
    "SMART_MONEY_SOURCE_IDENTITY",
    "SOURCE_CONTRACT_CLASSIFIER_VERSION",
    "SOURCE_CONTRACT_FAMILIES",
    "UNKNOWN_MARKET_SOURCE",
    "EventKind",
    "MarketKind",
    "SourceContract",
    "SourceContractFamily",
    "SourceIdentity",
    "classify_source_contract",
    "classify_source_contracts",
    "market_route",
    "source_contract_admission",
    "source_identity",
]
