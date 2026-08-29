"""Public Case, Intent and capability values for the Trading bounded context.

App-facing values and one business action. The lane itself is imported by the composition root from
`tracefold.trading.capital_lane`; it is not re-exported here, because a package root that exports a
runner invites a caller to build one somewhere other than the composition seam.
"""

from __future__ import annotations

from .blacklist import BlacklistSnapshotV1
from .capabilities import (
    ExecutionCapabilitySnapshotV1,
    ExecutionInstrumentCapabilityV1,
    ExecutionUniverseCandidateRow,
    ProviderInstrumentCandidateV1,
    StableCapabilityExclusionV1,
)
from .catalog import (
    VenueBinding,
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_venue_catalog_snapshot,
)
from .contracts import (
    Bar,
    CaseState,
    InstrumentRef,
    TradingCaseManifest,
)
from .intent import (
    ACTIVE_INTENT_STATES,
    INTENT_POLICY_SHA256,
    IntentOutcome,
    IntentReasonCode,
    TradeIntent,
    deterministic_client_order_id,
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
    "INTENT_POLICY_SHA256",
    "Bar",
    "BlacklistSnapshotV1",
    "CaseState",
    "ExecutionCapabilitySnapshotV1",
    "ExecutionInstrumentCapabilityV1",
    "ExecutionUniverseCandidateRow",
    "InstrumentRef",
    "IntentOutcome",
    "IntentReasonCode",
    "ProviderInstrumentCandidateV1",
    "ReplayArtifactV1",
    "ReplayBarV1",
    "ReplayExecutionIntentV1",
    "ReplayReceiptV1",
    "ReplayScenarioCapabilityV1",
    "ReplaySpecV1",
    "ReplayTerminalOutcomeV1",
    "StableCapabilityExclusionV1",
    "TradeIntent",
    "TradingCaseManifest",
    "VenueBinding",
    "VenueInstrumentCatalogEntryV1",
    "VenueInstrumentCatalogSnapshotV1",
    "build_venue_catalog_snapshot",
    "deterministic_client_order_id",
]
