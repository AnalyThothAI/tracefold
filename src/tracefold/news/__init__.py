"""Public News module interface (V3: broker-driven Event pipeline)."""

from .control import apply_control, parse_control
from .health import status_health
from .instruments import grounding_rollup
from .models import (
    GATE_POLICY_VERSION,
    TRIAGE_POLICY_VERSION,
    TRIAGE_PROMPT_VERSION,
    NewsFeedEntry,
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
from .triage_rules import DEFAULT_POLICY, DecidePolicy

__all__ = [
    "DEFAULT_POLICY",
    "GATE_POLICY_VERSION",
    "OPENNEWS_SOURCE_ID",
    "QUOTE_REQUEST_SYMBOL_MAX",
    "REACTION_METRIC_VERSION",
    "REVIEW_DEFAULT_HOURS",
    "REVIEW_MAX_HOURS",
    "TRIAGE_POLICY_VERSION",
    "TRIAGE_PROMPT_VERSION",
    "DecidePolicy",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "Outcome",
    "TriageVerdict",
    "apply_control",
    "event_outcome",
    "grounding_rollup",
    "parse_control",
    "parse_opennews_message",
    "status_health",
]
