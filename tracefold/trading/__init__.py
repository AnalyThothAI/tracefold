"""Public Case, Intent and capability values for the Trading bounded context.

App-facing values and one business action. The lane itself is imported by the composition root from
`tracefold.trading.capital_lane`; it is not re-exported here, because a package root that exports a
runner invites a caller to build one somewhere other than the composition seam.
"""

from __future__ import annotations

from .adapter_contracts import (
    BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
    HYPERLIQUID_PERP_ADAPTER_CONTRACT_SHA256,
)
from .bindings import ExecutionBindingV1, ExecutionVenue, binding_for_source_venue, venue_for_binding
from .blacklist import BlacklistSnapshotV1
from .capabilities import (
    ExecutionCapabilityExclusionV2,
    ExecutionCapabilitySnapshotV2,
    ExecutionInstrumentCapabilityV2,
    ExecutionInstrumentEvidenceV1,
    build_execution_capability_snapshot,
)
from .capital_authority import (
    CapitalAuthoritySnapshot,
    CapitalAuthorizationReceiptV1,
    CapitalRiskReservationV1,
    DailyRiskPolicyV1,
    OperatorArmReceiptV1,
    ProductionPromotionGrantRevocationV1,
    ProductionPromotionGrantV1,
    SettlementRiskLimitV1,
)
from .catalog import (
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_venue_catalog_snapshot,
)
from .contracts import (
    Bar,
    CapitalRuntimeV1,
    CaseState,
    DecisionRuntimeV1,
    InstrumentRef,
    TradingCaseManifest,
    VenueBinding,
    VenueBindingRuntimeV1,
    canonical_sha256,
)
from .evidence_verification import NautilusRuntimeStartV1
from .execution_policy import PROTECTION_CONTRACT_SHA256
from .intent import (
    ACTIVE_INTENT_STATES,
    INTENT_POLICY_SHA256,
    ActiveIntentValues,
    EntryFence,
    EntryFenceDisposition,
    EntryFenceUnavailable,
    EntryFenceWrite,
    IntentOutcome,
    IntentReasonCode,
    RejectedReason,
    TradeIntent,
    deterministic_client_order_id,
    materialize_active_intent,
    materialize_entry_fence,
    materialize_intent_outcome,
    validate_close_submission_identity,
    validate_stop_submission_identity,
)
from .quote_authority import (
    MAX_RECEIVE_AGE_NS,
    QUOTE_CONTRACT_SHA256,
    ExecutionQuote,
    ExecutionQuoteAuditV1,
    ExecutionQuoteRejectionV1,
    ExecutionQuoteSnapshotV1,
    SubmissionFenceV1,
    validate_entry_quote,
)
from .replay import (
    BAR_FIDELITY_VERSION,
    ReplayArtifactV1,
    ReplayBarV1,
    ReplayExecutionIntentV1,
    ReplayReceiptV1,
    ReplayScenarioCapabilityV1,
    ReplaySpecV1,
    ReplayTerminalOutcomeV1,
)

__all__ = [
    "ACTIVE_INTENT_STATES",
    "BAR_FIDELITY_VERSION",
    "BINANCE_USDM_ADAPTER_CONTRACT_SHA256",
    "HYPERLIQUID_PERP_ADAPTER_CONTRACT_SHA256",
    "INTENT_POLICY_SHA256",
    "MAX_RECEIVE_AGE_NS",
    "PROTECTION_CONTRACT_SHA256",
    "QUOTE_CONTRACT_SHA256",
    "ActiveIntentValues",
    "Bar",
    "BlacklistSnapshotV1",
    "CapitalAuthoritySnapshot",
    "CapitalAuthorizationReceiptV1",
    "CapitalRiskReservationV1",
    "CapitalRuntimeV1",
    "CaseState",
    "DailyRiskPolicyV1",
    "DecisionRuntimeV1",
    "EntryFence",
    "EntryFenceDisposition",
    "EntryFenceUnavailable",
    "EntryFenceWrite",
    "ExecutionBindingV1",
    "ExecutionCapabilityExclusionV2",
    "ExecutionCapabilitySnapshotV2",
    "ExecutionInstrumentCapabilityV2",
    "ExecutionInstrumentEvidenceV1",
    "ExecutionQuote",
    "ExecutionQuoteAuditV1",
    "ExecutionQuoteRejectionV1",
    "ExecutionQuoteSnapshotV1",
    "ExecutionVenue",
    "InstrumentRef",
    "IntentOutcome",
    "IntentReasonCode",
    "NautilusRuntimeStartV1",
    "OperatorArmReceiptV1",
    "ProductionPromotionGrantRevocationV1",
    "ProductionPromotionGrantV1",
    "RejectedReason",
    "ReplayArtifactV1",
    "ReplayBarV1",
    "ReplayExecutionIntentV1",
    "ReplayReceiptV1",
    "ReplayScenarioCapabilityV1",
    "ReplaySpecV1",
    "ReplayTerminalOutcomeV1",
    "SettlementRiskLimitV1",
    "SubmissionFenceV1",
    "TradeIntent",
    "TradingCaseManifest",
    "VenueBinding",
    "VenueBindingRuntimeV1",
    "VenueInstrumentCatalogEntryV1",
    "VenueInstrumentCatalogSnapshotV1",
    "binding_for_source_venue",
    "build_execution_capability_snapshot",
    "build_venue_catalog_snapshot",
    "canonical_sha256",
    "deterministic_client_order_id",
    "materialize_active_intent",
    "materialize_entry_fence",
    "materialize_intent_outcome",
    "validate_close_submission_identity",
    "validate_entry_quote",
    "validate_stop_submission_identity",
    "venue_for_binding",
]
