"""Public macro capability interface."""

from .observations.bundle_importer import import_macrodata_bundle
from .observations.catalog import MACRO_LIVE_CATALOG, MACRO_LIVE_VIEW_IDS, query_concepts_for_live_view
from .observations.constants import (
    MACRO_EVENT_PROVIDER_SERIES_TO_CONCEPT,
    MACRO_IMPORTABLE_PROVIDER_SERIES_TO_CONCEPT,
    MACRO_PROVIDER_SERIES_TO_CONCEPT,
)
from .observations.evidence import MACRO_LIVE_WINDOWS, MacroLiveWindow, build_macro_live_evidence
from .observations.identity import normalize_macro_date
from .observations.repository import MacroIntelRepository
from .observations.service import MacroSyncService
from .observations.types import MacrodataBundleRunResult, MacroSyncRunSummary
from .observations.worker import MacroSyncWorker
from .research.completed_session import CompletedSessionMacro, resolve_completed_session
from .research.repository import MacroResearchRepository, PostgresMacroResearchReadPort
from .research.service import (
    MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE,
    MACRO_RESEARCH_MAX_READ_REFS,
    FrozenMacroEvidenceScope,
    MacroEvidenceRecord,
    MacroObservationQuery,
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
    "MACRO_EVENT_PROVIDER_SERIES_TO_CONCEPT",
    "MACRO_IMPORTABLE_PROVIDER_SERIES_TO_CONCEPT",
    "MACRO_LIVE_CATALOG",
    "MACRO_LIVE_VIEW_IDS",
    "MACRO_LIVE_WINDOWS",
    "MACRO_PROVIDER_SERIES_TO_CONCEPT",
    "MACRO_RESEARCH_MAX_PRIOR_PUBLICATIONS_PER_PAGE",
    "MACRO_RESEARCH_MAX_READ_REFS",
    "CompletedSessionMacro",
    "FrozenMacroEvidenceScope",
    "MacroEvidenceRecord",
    "MacroIntelRepository",
    "MacroLiveWindow",
    "MacroObservationQuery",
    "MacroResearchAgentResult",
    "MacroResearchArtifactDraft",
    "MacroResearchAudit",
    "MacroResearchIntegrityError",
    "MacroResearchReadPort",
    "MacroResearchRepository",
    "MacroResearchWorker",
    "MacroSyncRunSummary",
    "MacroSyncService",
    "MacroSyncWorker",
    "MacrodataBundleRunResult",
    "PostgresMacroResearchReadPort",
    "build_macro_live_evidence",
    "canonicalize_macro_research_artifact",
    "import_macrodata_bundle",
    "normalize_macro_date",
    "query_concepts_for_live_view",
    "require_artifact_integrity",
    "require_catalog_in_scope",
    "require_evidence_in_scope",
    "require_prior_research_in_scope",
    "resolve_completed_session",
]
