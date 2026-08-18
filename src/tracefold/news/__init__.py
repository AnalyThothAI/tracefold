"""Public News module interface (V3: broker-driven Event pipeline)."""

from .control import apply_control, parse_control
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
from .triage_rules import DEFAULT_POLICY, DecidePolicy

__all__ = [
    "ANALYST_POLICY_VERSION",
    "ANALYST_PROMPT_VERSION",
    "DEFAULT_POLICY",
    "GATE_POLICY_VERSION",
    "OPENNEWS_SOURCE_ID",
    "TRIAGE_POLICY_VERSION",
    "TRIAGE_PROMPT_VERSION",
    "AnalystVerdict",
    "DecidePolicy",
    "NewsFeedEntry",
    "OpenNewsEvent",
    "OpenNewsExpectedError",
    "OpenNewsHistoryError",
    "OpenNewsStrategyHistory",
    "TriageVerdict",
    "apply_control",
    "parse_control",
    "parse_opennews_message",
]
