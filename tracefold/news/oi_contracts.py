"""The one public identity of the open-interest measurement.

Deliberately importable from anywhere: the parser stamps it on every ledger row, the market read model
builds a group key from it, and `tracefold.app` hands it to Trading as half the OI source identity. It
lived in three places once -- and when #458 cut two of the four rule values, the HTTP route's copy went
on accepting `?oi=withheld`, which validated and then narrowed nothing.

The rule vocabulary that used to live beside it went with the judge (#553). `stored` and
`oi_parse_failed` named the two outcomes of a verdict this lane no longer writes; whether a frame
parsed is now a column on its Item, which is a fact rather than a judgment about one.
"""

from typing import Final

OI_METRIC_VERSION: Final = "oi_signal_v1"

__all__ = ["OI_METRIC_VERSION"]
