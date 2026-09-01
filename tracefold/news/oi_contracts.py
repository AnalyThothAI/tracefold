"""Lightweight public identity and rule vocabulary for the deterministic OI lane.

Deliberately importable from anywhere: the judge writes these rule names, the feed groups them into the
monitor's tabs, and the HTTP route validates `?oi=` against the same set. It lived in three places once
-- and when #458 cut two of the four values, the route's copy went on accepting `?oi=withheld`, which
validated and then narrowed nothing.
"""

from typing import Final

OI_METRIC_VERSION: Final = "oi_signal_v1"

# The judge's two rule names. A frame either parsed and was stored, or it did not parse.
OI_STORED_RULE: Final = "stored"
OI_PARSE_FAILED_RULE: Final = "oi_parse_failed"

# The monitor's narrowing tabs. `stored` is deliberately not one: it is every row `all` already shows,
# so a tab for it would partition the lane into everything and everything-minus-one.
OI_FILTERS: Final[dict[str, tuple[str, ...]]] = {"parse_failed": (OI_PARSE_FAILED_RULE,)}
# `all` narrows nothing, so it is not in `OI_FILTERS`. It still has to be a value the caller can send:
# it is how a request says "I am the 持仓异动 monitor", which is what lets the outcome-group count be
# skipped for the tab that is displayed most.
OI_OUTCOMES: Final[frozenset[str]] = frozenset({"all", *OI_FILTERS})

__all__ = [
    "OI_FILTERS",
    "OI_METRIC_VERSION",
    "OI_OUTCOMES",
    "OI_PARSE_FAILED_RULE",
    "OI_STORED_RULE",
]
