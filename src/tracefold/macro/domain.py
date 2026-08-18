from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .market_facts import (
    MarketObservationFact,
    MarketPositionFact,
    MarketSettlementFact,
    MarketTrustTier,
)

MacroModuleId = Literal[
    "rates_fed",
    "economy_inflation",
    "liquidity_funding",
    "credit",
    "volatility",
    "cross_asset",
]
MacroClockKind = Literal[
    "intraday_market",
    "daily_settlement",
    "scheduled_release",
    "official_state",
    "official_document",
    "derived",
    "backfill",
]
MacroFactFamily = Literal[
    "series",
    "release",
    "document",
    "official_role",
    "document_analysis",
    "market_observation",
    "market_position",
    "market_settlement",
]
MacroTrustTier = MarketTrustTier
MacroSeasonalAdjustment = Literal[
    "seasonally_adjusted",
    "not_seasonally_adjusted",
    "seasonally_adjusted_annual_rate",
    "unknown",
]
MacroSourceRole = Literal[
    "decision_primary",
    "history",
    "release",
    "intraday_proxy",
    "reconciliation_only",
    "official_document",
    "derived",
]


class MacroSourceError(RuntimeError):
    pass


class MacroSourceUnavailable(MacroSourceError):
    pass


class MacroModelExpectedError(RuntimeError):
    """A declared provider/response failure safe for a native model retry."""


class MacroSourceClientProtocol(Protocol):
    def fetch(
        self,
        spec: DatasetSpec,
        *,
        partition_key: str,
        cursor: dict[str, Any],
        now_ms: int | None = None,
    ) -> FetchBatch: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MacroModuleDefinition:
    module_id: MacroModuleId
    label: str
    schema_version: str
    builder_key: MacroModuleId


_MACRO_MODULE_DEFINITIONS = (
    MacroModuleDefinition("rates_fed", "利率与美联储", "macro_rates_fed_v8", "rates_fed"),
    MacroModuleDefinition(
        "economy_inflation",
        "经济与通胀",
        "macro_economy_inflation_v6",
        "economy_inflation",
    ),
    MacroModuleDefinition(
        "liquidity_funding",
        "流动性与融资",
        "macro_liquidity_funding_v5",
        "liquidity_funding",
    ),
    MacroModuleDefinition("credit", "信用市场", "macro_credit_v7", "credit"),
    MacroModuleDefinition("volatility", "波动率", "macro_volatility_v7", "volatility"),
    MacroModuleDefinition(
        "cross_asset",
        "大类资产与期货",
        "macro_cross_asset_v8",
        "cross_asset",
    ),
)
MACRO_MODULE_DEFINITIONS = MappingProxyType(
    {definition.module_id: definition for definition in _MACRO_MODULE_DEFINITIONS}
)
if len(MACRO_MODULE_DEFINITIONS) != len(_MACRO_MODULE_DEFINITIONS):
    raise RuntimeError("macro_module_definition_duplicate_id")
if {definition.builder_key for definition in _MACRO_MODULE_DEFINITIONS} != set(MACRO_MODULE_DEFINITIONS):
    raise RuntimeError("macro_module_definition_builder_key_drift")

MACRO_MODULE_IDS: tuple[MacroModuleId, ...] = tuple(definition.module_id for definition in _MACRO_MODULE_DEFINITIONS)
MACRO_MODULE_LABELS = MappingProxyType(
    {definition.module_id: definition.label for definition in _MACRO_MODULE_DEFINITIONS}
)


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    dataset_id: str
    concept_id: str
    source_role: MacroSourceRole
    module_id: MacroModuleId
    clock_kind: MacroClockKind
    fact_family: MacroFactFamily
    adapter_id: str
    source_id: str
    source_url: str
    label: str
    series_id: str
    unit: str
    frequency: str
    freshness_seconds: int
    refresh_seconds: int
    seasonal_adjustment: MacroSeasonalAdjustment
    critical: bool = False
    trust_tier: MacroTrustTier = "official"
    instrument_id: str | None = None
    symbol: str | None = None
    instrument_name: str | None = None
    asset_class: str | None = None
    instrument_type: str | None = None
    venue: str | None = None
    currency: str = "USD"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def target_key(self) -> str:
        return f"{self.dataset_id}:latest"


@dataclass(frozen=True, slots=True)
class SeriesFact:
    dataset_id: str
    series_id: str
    reference_date: date
    vintage_date: date
    value_numeric: float | None
    value_text: str | None
    unit: str
    published_at_ms: int | None
    received_at_ms: int
    source_url: str
    raw_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReleaseFact:
    dataset_id: str
    release_id: str
    series_id: str
    reference_period: str
    scheduled_at_ms: int | None
    published_at_ms: int | None
    received_at_ms: int
    actual_value: float | None
    prior_value: float | None
    revised_prior_value: float | None
    estimate_value: float | None
    unit: str
    importance_tier: int
    source_url: str
    raw_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DocumentFact:
    document_id: str
    dataset_id: str
    document_type: Literal[
        "statement",
        "implementation",
        "minutes",
        "sep",
        "speech",
        "auction",
        "survey",
        "calendar",
    ]
    title: str
    effective_date: date
    published_at_ms: int
    received_at_ms: int
    source_url: str
    content_text: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FedOfficialRoleFact:
    role_fact_id: str
    dataset_id: str
    official_id: str
    official_name: str
    role_title: str
    organization: str
    effective_start: date
    effective_end: date | None
    fomc_participant: bool
    fomc_voter: bool
    source_url: str
    received_at_ms: int
    raw_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FetchBatch:
    dataset_id: str
    partition_key: str
    facts: tuple[
        SeriesFact | ReleaseFact | DocumentFact | MarketObservationFact | MarketPositionFact | MarketSettlementFact,
        ...,
    ]
    cursor: dict[str, Any]
    response_hash: str
    source_url: str
    http_status: int | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    completion: Literal["complete", "continuation"] = "complete"


__all__ = [
    "MACRO_MODULE_DEFINITIONS",
    "MACRO_MODULE_IDS",
    "MACRO_MODULE_LABELS",
    "DatasetSpec",
    "DocumentFact",
    "FedOfficialRoleFact",
    "FetchBatch",
    "MacroClockKind",
    "MacroFactFamily",
    "MacroModelExpectedError",
    "MacroModuleDefinition",
    "MacroModuleId",
    "MacroSeasonalAdjustment",
    "MacroSourceClientProtocol",
    "MacroSourceError",
    "MacroSourceRole",
    "MacroSourceUnavailable",
    "MacroTrustTier",
    "MarketObservationFact",
    "MarketPositionFact",
    "MarketSettlementFact",
    "ReleaseFact",
    "SeriesFact",
]
