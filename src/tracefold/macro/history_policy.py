from __future__ import annotations

from collections.abc import Iterable

from tracefold.macro.calculations import (
    CALCULATION_REGISTRY,
    NATURAL_CHANGE_REGISTRY,
)
from tracefold.macro.registry import DATASET_REGISTRY

_PRESENTATION_HISTORY_ROWS = 500
_CURVE_PRESENTATION_HISTORY_ROWS = 130
_FULL_AVAILABLE_HISTORY_ROWS = 10_000
# Projection storage reduces intraday facts to one end-of-UTC-day point. One
# current point, the 30-day comparison window, its four-day bounded lag, and
# one boundary point are sufficient for every intraday payload calculation.
_INTRADAY_COMPARISON_ROWS = 36
_DAILY_COMPARISON_ROWS = 260

# These payloads expose actual-available-history percentiles. Truncating them
# below the existing semantic bound changes reader-visible values.
_FULL_HISTORY_PERCENTILE_DATASETS = frozenset(
    {
        "fred.bamlc0a0cm",
        "fred.bamlc0a4cbbb",
        "fred.bamlh0a1hybb",
        "fred.bamlh0a2hyb",
        "fred.bamlh0a3hyc",
        "fred.bamlc0a0cmey",
        "fred.bamlh0a0hym2ey",
        "fred.drblacbs",
        "fred.drcrelexfacbs",
        "fred.drcclacbs",
        "fred.corblacbs",
        "fred.corccacbs",
    }
)


def series_history_limits(dataset_ids: Iterable[str]) -> dict[str, int]:
    limits = {str(dataset_id): _PRESENTATION_HISTORY_ROWS for dataset_id in dict.fromkeys(dataset_ids)}
    for dataset_id in (
        "treasury.daily_nominal_curve",
        "treasury.daily_real_curve",
    ):
        if dataset_id in limits:
            limits[dataset_id] = _CURVE_PRESENTATION_HISTORY_ROWS
    for calculation in CALCULATION_REGISTRY.values():
        for dataset_id in calculation.input_dataset_ids:
            if dataset_id in limits:
                limits[dataset_id] = max(
                    limits[dataset_id],
                    int(calculation.minimum_observations),
                )
    for dataset_id, natural_change in NATURAL_CHANGE_REGISTRY.items():
        if dataset_id in limits:
            minimums = [minimum for _window, minimum in natural_change.minimum_observations]
            if minimums:
                limits[dataset_id] = max(limits[dataset_id], *minimums)
    for dataset_id in _FULL_HISTORY_PERCENTILE_DATASETS & limits.keys():
        limits[dataset_id] = _FULL_AVAILABLE_HISTORY_ROWS
    return limits


def market_history_limits(dataset_ids: Iterable[str]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for dataset_id in dict.fromkeys(str(item) for item in dataset_ids):
        spec = DATASET_REGISTRY[dataset_id]
        if spec.frequency == "intraday":
            limits[dataset_id] = _INTRADAY_COMPARISON_ROWS
        elif spec.frequency == "daily":
            limits[dataset_id] = _DAILY_COMPARISON_ROWS
        else:
            limits[dataset_id] = _PRESENTATION_HISTORY_ROWS
    return limits


__all__ = ["market_history_limits", "series_history_limits"]
