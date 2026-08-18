"""Public News module interface (V3: broker-driven Event pipeline)."""

from .models import (
    ANALYST_POLICY_VERSION,
    ANALYST_PROMPT_VERSION,
    GATE_POLICY_VERSION,
    TRIAGE_POLICY_VERSION,
    TRIAGE_PROMPT_VERSION,
    AnalystVerdict,
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

__all__ = [
    "ANALYST_POLICY_VERSION",
    "ANALYST_PROMPT_VERSION",
    "GATE_POLICY_VERSION",
    "OPENNEWS_SOURCE_ID",
    "TRIAGE_POLICY_VERSION",
    "TRIAGE_PROMPT_VERSION",
    "AnalystVerdict",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "TriageVerdict",
    "parse_opennews_message",
]
