"""What the code decides *after* the model answers: the delivery decision and the semantic domain rules.

Split out of `graph._assemble` (#314 review) so that `identity.py` can render these rules and hash the
render. While they were expressions inside the executor, they were behavior-deciding bytes that no identity
covered: changing `"background": "drop"` to `"background": "push"` turns every background judgment into a
delivery, and `program_sha256` (instructions), `envelope_sha256` (request envelope), `policy_sha256`
(operator config) and `retrieval_sha256` would all have held still. The bundle would not have moved, no
epoch would have opened, and evidence from before and after the change would have pooled into one cohort —
the precise failure `identity.py` claims to make unrepresentable.

They are pure functions over their inputs, and take those inputs unpacked rather than as a
`TradeRelevanceV1`, so the identity render can enumerate the whole decision surface without constructing
objects a validator would refuse.
"""

from __future__ import annotations

from typing import Final

from .contracts import ReaderValue, TradeTradability

# The model's only delivery intent, and what the code does with each value of it. `reader_value` is what
# `EventSemantics` emits; `decision` is what a reader receives.
READER_VALUE_DECISION: Final[dict[ReaderValue, str]] = {
    "escalate": "escalate",
    "realtime": "push",
    "background": "drop",
    "none": "drop",
}

# The tradability values that can carry a current trade surface at all.
_ACTIONABLE_TRADABILITY: Final[frozenset[str]] = frozenset({"direct", "second_order"})


def decision_for(reader_value: ReaderValue) -> str:
    return READER_VALUE_DECISION[reader_value]


def is_actionable(*, tradability: TradeTradability, has_channels: bool, has_affected_markets: bool) -> bool:
    """Whether a judgment names a trade surface the reader could act on.

    All three conditions, because a channel with no market and a market with no channel each describe half
    a transmission: the code owns this conjunction so a model cannot assert `actionable` directly.
    """

    return tradability in _ACTIONABLE_TRADABILITY and has_channels and has_affected_markets


def restatement_index_error(*, novelty: str, restates: int, told_count: int) -> str | None:
    """The domain rule a structured-output constraint cannot express, and the code therefore must.

    `restates` is a visible `event_status.told` index if and only if novelty is restatement. A JSON schema
    can say "integer >= -1"; it cannot say "in range of a list you were shown, and only in one case". The
    named error is the failure class that dominated the primary route when this rule's documentation
    stopped reaching the model (#315).
    """

    if novelty == "restatement":
        if restates < 0 or restates >= told_count:
            return "news_program_restatement_index_invalid"
        return None
    if restates != -1:
        return "news_program_non_restatement_index_invalid"
    return None


__all__ = [
    "READER_VALUE_DECISION",
    "decision_for",
    "is_actionable",
    "restatement_index_error",
]
