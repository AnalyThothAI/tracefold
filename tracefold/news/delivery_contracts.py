"""What one delivery attempt proved, in the transport's own vocabulary.

This is the whole contract between a News delivery adapter and whatever loop is holding the card:
the adapter says what its own failure proved about the message, and the loop decides what to do
about it. Adapters carry these values on their errors; nothing here knows about market tracks,
Events, cards or retry budgets, and nothing here is a state a row is ever stored in.

`not_sent` is a claim the adapter can defend -- the request never left, or the provider answered
with a refusal. `unknown` is everything else, including a read timeout and a provider 5xx: the
request was written and the answer was not read, so "it was not delivered" is not a fact. Both
delivery loops -- the market notification loop and the News Deliverer -- read that pair through
`classify_delivery_failure` below, so "may this be sent again" has one answer in this repository
rather than one per lane (#604 N1).

It lives here rather than in the market notification loop that first needed it (#562): a transport
adapter that has to import a business loop to name its own failure is a dependency pointing the
wrong way, and `tests/architecture/test_backend_boundaries.py` now holds that direction.
"""

from __future__ import annotations

from typing import Final

COMMIT_PHASE_NOT_SENT: Final = "not_sent"
COMMIT_PHASE_UNKNOWN: Final = "unknown"

# What a loop holding the card is allowed to do next, and the whole of it. Three answers, because a
# failure proves one of exactly three things and each costs a reader something different: sending
# again what was never sent costs nothing, sending again what was refused costs an attempt that will
# be refused the same way, and sending again what may already be on a screen costs the reader a
# duplicate. The budget itself -- how many attempts, how long apart -- belongs to whichever loop is
# holding the card, and is deliberately not named here.
DELIVERY_FAILURE_RETRIABLE: Final = "retriable"
DELIVERY_FAILURE_REFUSED: Final = "refused"
DELIVERY_FAILURE_UNKNOWN: Final = "unknown"


def classify_delivery_failure(exc: BaseException) -> str:
    """What one failed attempt proved, read from the adapter's own evidence and nothing else.

    `not_sent` is the adapter saying the request never reached the provider or the provider answered
    with a refusal, and `retryable` is it saying the cause is one that passes: a connect failure, a
    rate limit. Only that pair earns another attempt.

    Everything else is `unknown`, and that is the honest answer rather than a pessimistic one. A read
    timeout means the request was written and the answer was not read -- the provider may well have
    delivered it -- and a 5xx means the provider's own tier answered, not that it did nothing. Calling
    either "not sent" and retrying would put a second card on a reader's screen (#562 §5.2).

    An exception carrying neither attribute -- an operation overrun, a bug, anything raised by
    something that is not a delivery adapter -- is `unknown` by that same rule.
    """

    if str(getattr(exc, "commit_phase", "") or "") != COMMIT_PHASE_NOT_SENT:
        return DELIVERY_FAILURE_UNKNOWN
    if bool(getattr(exc, "retryable", False)):
        return DELIVERY_FAILURE_RETRIABLE
    return DELIVERY_FAILURE_REFUSED


__all__ = [
    "COMMIT_PHASE_NOT_SENT",
    "COMMIT_PHASE_UNKNOWN",
    "DELIVERY_FAILURE_REFUSED",
    "DELIVERY_FAILURE_RETRIABLE",
    "DELIVERY_FAILURE_UNKNOWN",
    "classify_delivery_failure",
]
