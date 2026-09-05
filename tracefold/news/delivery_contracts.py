"""What one delivery attempt proved, in the transport's own vocabulary.

This is the whole contract between a News delivery adapter and whatever loop is holding the card:
the adapter says what its own failure proved about the message, and the loop decides what to do
about it. Adapters carry these values on their errors; nothing here knows about market tracks,
Events, cards or retry budgets, and nothing here is a state a row is ever stored in.

`not_sent` is a claim the adapter can defend -- the request never left, or the provider answered
with a refusal. `unknown` is everything else, including a read timeout and a provider 5xx: the
request was written and the answer was not read, so "it was not delivered" is not a fact. The
ordinary News path keeps reading `code` alone and is unchanged by these.

It lives here rather than in the market notification loop that first needed it (#562): a transport
adapter that has to import a business loop to name its own failure is a dependency pointing the
wrong way, and `tests/architecture/test_backend_boundaries.py` now holds that direction.
"""

from __future__ import annotations

from typing import Final

COMMIT_PHASE_NOT_SENT: Final = "not_sent"
COMMIT_PHASE_UNKNOWN: Final = "unknown"

__all__ = [
    "COMMIT_PHASE_NOT_SENT",
    "COMMIT_PHASE_UNKNOWN",
]
