"""Public News module interface (V3: broker-driven Event pipeline)."""

from .semantic_contract import (
    TOLD_MAX,
    TOLD_SELECTOR_ID,
    TOLD_SELECTOR_SHA256,
    TOLD_SOURCE_MAX,
    TOLD_STORYLINE_TIER_MAX,
    TOLD_WINDOW_MS,
    EditorialEnvelope,
    ProgramTrace,
    ProgramUsage,
    ReaderCardSemanticView,
    ScoredJudgment,
    SemanticJudge,
    SemanticJudgeError,
    SemanticJudgment,
    TradeRelevanceV1,
    TriageContext,
)

# isort: split
from .artifact_identity import canonical_json, canonical_sha
from .canary import apply_canary_control, parse_canary_control
from .candidate_evaluator import (
    LEARNING_EPOCH,
    TRUSTED_ROOT_SHA,
    ArmManifest,
    CandidateEvaluator,
    CandidateManifest,
    ClosedWindow,
    DatasetManifest,
    DatasetSpec,
    EvaluationReport,
    EvaluationRequest,
    ProposalReceipt,
    evaluation_run_sha,
)
from .facts import FACT_UNIT_VERSION, FactUnit, extract_fact_units
from .health import status_health
from .instruments import grounding_rollup
from .models import (
    GATE_POLICY_VERSION,
    TRIAGE_POLICY_VERSION,
    NewsFeedEntry,
    ReaderReceipt,
    TriageVerdict,
)
from .oi_signals import DEFAULT_OI_POLICY, OiPolicy
from .oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from .oi_signals import PROGRAM_VERSION as OI_PROGRAM_VERSION
from .opennews import (
    OPENNEWS_SOURCE_ID,
    OpenNewsEvent,
    OpenNewsExpectedError,
    OpenNewsHistoryError,
    OpenNewsStrategyHistory,
    parse_opennews_message,
)
from .outcome import Outcome, event_outcome

# #88: the public bounds of the price surfaces. The HTTP layer validates `/api/news/quotes` and
# `/api/news/review` against them and must not restate the numbers.
from .pricing import QUOTE_REQUEST_SYMBOL_MAX, REACTION_METRIC_VERSION, REVIEW_DEFAULT_HOURS, REVIEW_MAX_HOURS
from .recording_replay import (
    RecordingReplayCapability,
    RecordingReplayError,
    ReplayArmSpec,
    load_recording_replay_capability,
)
from .review import (
    READER_CONTRACT_VERSION,
    REVIEW_RUBRIC_VERSION,
    BlindPairwiseSubmission,
    DeskQuery,
    EventRubricSubmission,
    ExternalMissSubmission,
    Principal,
    ReviewDesk,
    ReviewSubmission,
    TaskRef,
)
from .triage_rules import DEFAULT_POLICY, DecidePolicy

__all__ = [
    "DEFAULT_OI_POLICY",
    "DEFAULT_POLICY",
    "FACT_UNIT_VERSION",
    "GATE_POLICY_VERSION",
    "LEARNING_EPOCH",
    "OI_METRIC_VERSION",
    "OI_PROGRAM_VERSION",
    "OPENNEWS_SOURCE_ID",
    "QUOTE_REQUEST_SYMBOL_MAX",
    "REACTION_METRIC_VERSION",
    "READER_CONTRACT_VERSION",
    "REVIEW_DEFAULT_HOURS",
    "REVIEW_MAX_HOURS",
    "REVIEW_RUBRIC_VERSION",
    "TOLD_MAX",
    "TOLD_SELECTOR_ID",
    "TOLD_SELECTOR_SHA256",
    "TOLD_SOURCE_MAX",
    "TOLD_STORYLINE_TIER_MAX",
    "TOLD_WINDOW_MS",
    "TRIAGE_POLICY_VERSION",
    "TRUSTED_ROOT_SHA",
    "ArmManifest",
    "BlindPairwiseSubmission",
    "CandidateEvaluator",
    "CandidateManifest",
    "ClosedWindow",
    "DatasetManifest",
    "DatasetSpec",
    "DecidePolicy",
    "DeskQuery",
    "EditorialEnvelope",
    "EvaluationReport",
    "EvaluationRequest",
    "EventRubricSubmission",
    "ExternalMissSubmission",
    "FactUnit",
    "NewsFeedEntry",
    "OiPolicy",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "Outcome",
    "Principal",
    "ProgramTrace",
    "ProgramUsage",
    "ProposalReceipt",
    "ReaderCardSemanticView",
    "ReaderReceipt",
    "RecordingReplayCapability",
    "RecordingReplayError",
    "ReplayArmSpec",
    "ReviewDesk",
    "ReviewSubmission",
    "ScoredJudgment",
    "SemanticJudge",
    "SemanticJudgeError",
    "SemanticJudgment",
    "TaskRef",
    "TradeRelevanceV1",
    "TriageContext",
    "TriageVerdict",
    "apply_canary_control",
    "canonical_json",
    "canonical_sha",
    "evaluation_run_sha",
    "event_outcome",
    "extract_fact_units",
    "grounding_rollup",
    "load_recording_replay_capability",
    "parse_canary_control",
    "parse_opennews_message",
    "status_health",
]
