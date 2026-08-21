"""Public News module interface (V3: broker-driven Event pipeline)."""

from .artifact_identity import canonical_sha
from .canary import apply_canary_control, parse_canary_control
from .candidate_evaluator import (
    TRUSTED_ROOT_SHA,
    ArmManifest,
    CandidateEvaluator,
    CandidateManifest,
    ClosedWindow,
    DatasetManifest,
    DatasetSpec,
    EvaluationReport,
    EvaluationRequest,
    LiveTriageModelAdapter,
    ModelInvocation,
    ModelObservation,
    ProposalReceipt,
    RecordReplayModelAdapter,
)
from .control import apply_control, parse_control
from .facts import FACT_UNIT_VERSION, FactUnit, extract_fact_units
from .health import status_health
from .instruments import grounding_rollup
from .models import (
    GATE_POLICY_VERSION,
    TRIAGE_POLICY_VERSION,
    TRIAGE_PROMPT_VERSION,
    NewsFeedEntry,
    ReaderReceipt,
    TriageVerdict,
)
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
    "DEFAULT_POLICY",
    "FACT_UNIT_VERSION",
    "GATE_POLICY_VERSION",
    "OPENNEWS_SOURCE_ID",
    "QUOTE_REQUEST_SYMBOL_MAX",
    "REACTION_METRIC_VERSION",
    "READER_CONTRACT_VERSION",
    "REVIEW_DEFAULT_HOURS",
    "REVIEW_MAX_HOURS",
    "REVIEW_RUBRIC_VERSION",
    "TRIAGE_POLICY_VERSION",
    "TRIAGE_PROMPT_VERSION",
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
    "EvaluationReport",
    "EvaluationRequest",
    "EventRubricSubmission",
    "ExternalMissSubmission",
    "FactUnit",
    "LiveTriageModelAdapter",
    "ModelInvocation",
    "ModelObservation",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "Outcome",
    "Principal",
    "ProposalReceipt",
    "ReaderReceipt",
    "RecordReplayModelAdapter",
    "ReviewDesk",
    "ReviewSubmission",
    "TaskRef",
    "TriageVerdict",
    "apply_canary_control",
    "apply_control",
    "canonical_sha",
    "event_outcome",
    "extract_fact_units",
    "grounding_rollup",
    "parse_canary_control",
    "parse_control",
    "parse_opennews_message",
    "status_health",
]
