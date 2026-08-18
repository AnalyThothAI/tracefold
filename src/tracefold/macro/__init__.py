"""Public Macro decision-system interface."""

from .acquisition import MacroAcquisitionService, acquisition_loop_policy
from .backfill import MacroBackfillPolicy, professional_backfill_policies
from .calculations import (
    CALCULATION_REGISTRY,
    NATURAL_CHANGE_REGISTRY,
    CalculationSpec,
    NaturalChangeCalculationSpec,
    natural_change_calculation,
)
from .coverage import COVERAGE_MANIFEST, CoverageSpec, coverage_for_module
from .domain import (
    MACRO_MODULE_DEFINITIONS,
    MACRO_MODULE_IDS,
    MACRO_MODULE_LABELS,
    DatasetSpec,
    DocumentFact,
    FedOfficialRoleFact,
    FetchBatch,
    MacroClockKind,
    MacroFactFamily,
    MacroModelExpectedError,
    MacroModuleDefinition,
    MacroModuleId,
    MacroSeasonalAdjustment,
    MacroSourceClientProtocol,
    MacroSourceError,
    MacroSourceRole,
    MacroSourceUnavailable,
    MarketObservationFact,
    MarketSettlementFact,
    ReleaseFact,
    SeriesFact,
)
from .fed_analysis import (
    FED_DOCUMENT_ANALYSIS_PROMPT_VERSION,
    FED_FOMC_ANALYSIS_LOOKBACK_DAYS,
    FED_SPEECH_ANALYSIS_LOOKBACK_DAYS,
    FedAnalysisEvidence,
    FedDocumentAnalysisDraft,
    MacroDocumentAnalysisService,
)
from .fed_document_agent import FedDocumentAnalysisAgent
from .market_calendar import is_us_market_session
from .market_facts import GeneralMarketInstrumentSpec, MarketPositionFact, MarketTrustTier
from .market_facts_repository import GeneralMarketRepository
from .module_payloads import build_typed_module_payload, schema_version_for_module
from .projection import rebuild_all_macro_modules_for_maintenance
from .projection_worker import MacroProjectionCandidate
from .reasons import MacroReason, MacroReasonImpact, MacroReasonRecovery, macro_reason
from .registry import (
    DATASET_REGISTRY,
    MACRO_ACQUISITION_ADAPTER_IDS,
    datasets_for_clock,
    datasets_for_module,
    require_dataset,
)
from .repository import MacroRepository
from .runtime import MacroAcquisition

__all__ = [
    "CALCULATION_REGISTRY",
    "COVERAGE_MANIFEST",
    "DATASET_REGISTRY",
    "FED_DOCUMENT_ANALYSIS_PROMPT_VERSION",
    "FED_FOMC_ANALYSIS_LOOKBACK_DAYS",
    "FED_SPEECH_ANALYSIS_LOOKBACK_DAYS",
    "MACRO_ACQUISITION_ADAPTER_IDS",
    "MACRO_MODULE_DEFINITIONS",
    "MACRO_MODULE_IDS",
    "MACRO_MODULE_LABELS",
    "NATURAL_CHANGE_REGISTRY",
    "CalculationSpec",
    "CoverageSpec",
    "DatasetSpec",
    "DocumentFact",
    "FedAnalysisEvidence",
    "FedDocumentAnalysisAgent",
    "FedDocumentAnalysisDraft",
    "FedOfficialRoleFact",
    "FetchBatch",
    "GeneralMarketInstrumentSpec",
    "GeneralMarketRepository",
    "MacroAcquisition",
    "MacroAcquisitionService",
    "MacroBackfillPolicy",
    "MacroClockKind",
    "MacroDocumentAnalysisService",
    "MacroFactFamily",
    "MacroModelExpectedError",
    "MacroModuleDefinition",
    "MacroModuleId",
    "MacroProjectionCandidate",
    "MacroReason",
    "MacroReasonImpact",
    "MacroReasonRecovery",
    "MacroRepository",
    "MacroSeasonalAdjustment",
    "MacroSourceClientProtocol",
    "MacroSourceError",
    "MacroSourceRole",
    "MacroSourceUnavailable",
    "MarketObservationFact",
    "MarketPositionFact",
    "MarketSettlementFact",
    "MarketTrustTier",
    "NaturalChangeCalculationSpec",
    "ReleaseFact",
    "SeriesFact",
    "acquisition_loop_policy",
    "build_typed_module_payload",
    "coverage_for_module",
    "datasets_for_clock",
    "datasets_for_module",
    "is_us_market_session",
    "macro_reason",
    "natural_change_calculation",
    "professional_backfill_policies",
    "rebuild_all_macro_modules_for_maintenance",
    "require_dataset",
    "schema_version_for_module",
]
