"""Public Macro decision-system interface."""

from .acquisition_worker import MacroAcquisitionWorker
from .backfill import MacroBackfillPolicy, professional_backfill_policies
from .coverage import COVERAGE_MANIFEST, CoverageSpec, coverage_for_module
from .document_analysis_worker import MacroDocumentAnalysisWorker
from .domain import (
    MACRO_MODULE_IDS,
    MACRO_MODULE_LABELS,
    DatasetSpec,
    DocumentFact,
    FedOfficialRoleFact,
    FetchBatch,
    MacroClockKind,
    MacroFactFamily,
    MacroModuleId,
    MacroSourceClientProtocol,
    MacroSourceError,
    MacroSourceUnavailable,
    MarketObservationFact,
    MarketSettlementFact,
    ReleaseFact,
    SeriesFact,
)
from .fed_analysis import (
    FedAnalysisEvidence,
    FedDocumentAnalysisDraft,
    MacroDocumentAnalysisService,
)
from .judgment import resolve_judgment_session
from .judgment_worker import MacroJudgmentWorker
from .module_payloads import build_typed_module_payload, schema_version_for_module
from .projection_worker import MacroProjectionWorker
from .registry import DATASET_REGISTRY, datasets_for_clock, datasets_for_module, require_dataset
from .repository import MacroRepository
from .research.completed_session import CompletedSessionMacro, resolve_completed_session
from .research.repository import MacroResearchRepository, PostgresMacroResearchReadPort
from .research.service import (
    MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE,
    MACRO_RESEARCH_MAX_READ_REFS,
    FrozenMacroEvidenceScope,
    MacroEvidenceQuery,
    MacroEvidenceRecord,
    MacroResearchAgentResult,
    MacroResearchArtifactDraft,
    MacroResearchAudit,
    MacroResearchIntegrityError,
    MacroResearchReadPort,
    canonicalize_macro_research_artifact,
    require_artifact_integrity,
    require_catalog_in_scope,
    require_evidence_in_scope,
    require_prior_research_in_scope,
)
from .research.worker import MacroResearchWorker

__all__ = [
    "COVERAGE_MANIFEST",
    "DATASET_REGISTRY",
    "MACRO_MODULE_IDS",
    "MACRO_MODULE_LABELS",
    "MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE",
    "MACRO_RESEARCH_MAX_READ_REFS",
    "CompletedSessionMacro",
    "CoverageSpec",
    "DatasetSpec",
    "DocumentFact",
    "FedAnalysisEvidence",
    "FedDocumentAnalysisDraft",
    "FedOfficialRoleFact",
    "FetchBatch",
    "FrozenMacroEvidenceScope",
    "MacroAcquisitionWorker",
    "MacroBackfillPolicy",
    "MacroClockKind",
    "MacroDocumentAnalysisService",
    "MacroDocumentAnalysisWorker",
    "MacroEvidenceQuery",
    "MacroEvidenceRecord",
    "MacroFactFamily",
    "MacroJudgmentWorker",
    "MacroModuleId",
    "MacroProjectionWorker",
    "MacroRepository",
    "MacroResearchAgentResult",
    "MacroResearchArtifactDraft",
    "MacroResearchAudit",
    "MacroResearchIntegrityError",
    "MacroResearchReadPort",
    "MacroResearchRepository",
    "MacroResearchWorker",
    "MacroSourceClientProtocol",
    "MacroSourceError",
    "MacroSourceUnavailable",
    "MarketObservationFact",
    "MarketSettlementFact",
    "PostgresMacroResearchReadPort",
    "ReleaseFact",
    "SeriesFact",
    "build_typed_module_payload",
    "canonicalize_macro_research_artifact",
    "coverage_for_module",
    "datasets_for_clock",
    "datasets_for_module",
    "professional_backfill_policies",
    "require_artifact_integrity",
    "require_catalog_in_scope",
    "require_dataset",
    "require_evidence_in_scope",
    "require_prior_research_in_scope",
    "resolve_completed_session",
    "resolve_judgment_session",
    "schema_version_for_module",
]
