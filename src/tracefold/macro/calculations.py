from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Literal, cast

from tracefold.macro.assets import CROSS_ASSET_DATASETS
from tracefold.macro.domain import DatasetSpec, MacroModuleId
from tracefold.macro.registry import DATASET_REGISTRY

_CORRELATION_WINDOWS = (
    ("30_daily_returns", 30),
    ("90_daily_returns", 90),
    ("252_daily_returns", 252),
)
_DEFAULT_CORRELATION_WINDOW = "90_daily_returns"

GapPolicy = Literal["intersection", "latest_available"]


@dataclass(frozen=True, slots=True)
class CalculationSpec:
    feature_id: str
    module_id: MacroModuleId
    input_dataset_ids: tuple[str, ...]
    formula_version: str
    unit: str
    windows: tuple[str, ...]
    minimum_observations: int
    gap_policy: GapPolicy
    freshness_seconds: int
    baseline: str
    output_schema: str
    operation: str


@dataclass(frozen=True, slots=True)
class NaturalChangeCalculationSpec:
    dataset_id: str
    cadence: str
    comparison_windows: tuple[str, ...]
    minimum_observations: tuple[tuple[str, int], ...]
    formula_version: str
    output_unit: str
    revision_policy: str
    surprise_policy: str
    gap_policy: str
    max_gap_days: int | None
    output_schema: str = "macro_natural_change_v1"


def _natural_change_spec(spec: DatasetSpec) -> NaturalChangeCalculationSpec:
    basis_points = spec.unit == "percent"
    output_unit = "basis_points" if basis_points else "percent"
    windows: tuple[str, ...]
    minimums: tuple[tuple[str, int], ...]
    if spec.frequency in {"intraday", "daily"}:
        windows = ("1d", "1w", "1m")
        minimums = (("1d", 2), ("1w", 2), ("1m", 2))
        formula = "basis_point_difference_v1" if basis_points else "percent_return_v1"
        gap_policy = "bounded_previous_observation"
        max_gap_days = 4
    elif spec.frequency == "weekly":
        windows = ("wow", "4w")
        minimums = (("wow", 2), ("4w", 2))
        formula = "basis_point_difference_v1" if basis_points else "percent_change_v1"
        gap_policy = "bounded_previous_observation"
        max_gap_days = 7
    elif spec.frequency == "monthly":
        windows = ("mom", "3m_annualized", "yoy")
        minimums = (("mom", 2), ("3m_annualized", 4), ("yoy", 13))
        formula = "natural_monthly_change_v1"
        gap_policy = "exact_period_lag"
        max_gap_days = None
    elif spec.frequency == "quarterly":
        windows = ("qoq_annualized", "yoy")
        minimums = (("qoq_annualized", 2), ("yoy", 5))
        formula = "natural_quarterly_change_v1"
        gap_policy = "exact_period_lag"
        max_gap_days = None
    else:
        windows = ()
        minimums = ()
        formula = "not_applicable"
        gap_policy = "not_applicable"
        max_gap_days = None
    return NaturalChangeCalculationSpec(
        dataset_id=spec.dataset_id,
        cadence=spec.frequency,
        comparison_windows=windows,
        minimum_observations=minimums,
        formula_version=formula,
        output_unit=output_unit,
        revision_policy=(
            "explicit_revised_prior_only" if spec.fact_family == "release" else "latest_vintage_per_reference_period"
        ),
        surprise_policy=("explicit_consensus_only" if spec.fact_family == "release" else "not_applicable"),
        gap_policy=gap_policy,
        max_gap_days=max_gap_days,
    )


NATURAL_CHANGE_REGISTRY = MappingProxyType(
    {dataset_id: _natural_change_spec(spec) for dataset_id, spec in DATASET_REGISTRY.items()}
)


def natural_change_calculation(dataset_id: str) -> NaturalChangeCalculationSpec:
    try:
        return NATURAL_CHANGE_REGISTRY[dataset_id]
    except KeyError as exc:
        raise ValueError(f"macro_natural_change_unknown_dataset:{dataset_id}") from exc


_CALCULATIONS = (
    CalculationSpec(
        "rates.treasury_curve_cross_sections",
        "rates_fed",
        ("treasury.daily_nominal_curve", "treasury.daily_real_curve"),
        "treasury_curve_contract_v3",
        "percent",
        ("current", "previous", "1w", "mtd", "3m"),
        1,
        "latest_available",
        259_200,
        "official_completed_session_cross_sections",
        "macro_rates_curve_v3",
        "curve_contract",
    ),
    CalculationSpec(
        "rates.matched_breakeven_curve",
        "rates_fed",
        ("treasury.daily_nominal_curve", "treasury.daily_real_curve"),
        "matched_nominal_minus_real_v2",
        "percent",
        ("current", "previous", "1w", "mtd", "3m"),
        1,
        "intersection",
        259_200,
        "matching_tenor_and_reference_date",
        "macro_breakeven_snapshots_v2",
        "curve_contract",
    ),
    CalculationSpec(
        "rates.curve_shape",
        "rates_fed",
        ("treasury.daily_nominal_curve",),
        "level_slope_curvature_classification_v3",
        "basis_points",
        ("1d", "1w", "mtd", "3m"),
        2,
        "intersection",
        259_200,
        "two_ten_thirty_year_five_basis_point_materiality",
        "macro_curve_classification_v3",
        "curve_contract",
    ),
    CalculationSpec(
        "credit.rating_ladder",
        "credit",
        (
            "fred.bamlc0a0cm",
            "fred.bamlc0a4cbbb",
            "fred.bamlh0a1hybb",
            "fred.bamlh0a2hyb",
            "fred.bamlh0a3hyc",
        ),
        "series_statistics_v2",
        "percent",
        ("latest", "1w", "1m", "full_available_history"),
        1,
        "latest_available",
        259_200,
        "actual_observation_distribution",
        "macro_indicator_rows_v2",
        "series_statistics",
    ),
    CalculationSpec(
        "credit.funding_cost_comparisons",
        "credit",
        (
            "fred.bamlc0a0cmey",
            "fred.bamlh0a0hym2ey",
            "fred.effr",
            "fred.dgs10",
        ),
        "matched_rate_difference_v1",
        "basis_points",
        ("latest_common_date",),
        1,
        "intersection",
        259_200,
        "zero",
        "macro_funding_comparisons_v1",
        "funding_comparisons",
    ),
    CalculationSpec(
        "cross_asset.normalized_returns",
        "cross_asset",
        CROSS_ASSET_DATASETS.etf_daily_dataset_ids,
        "normalized_to_100_v1",
        "index",
        ("up_to_260_observations",),
        1,
        "latest_available",
        259_200,
        "first_observation_equals_100",
        "macro_normalized_asset_points_v1",
        "market_comparison",
    ),
    CalculationSpec(
        "cross_asset.return_correlations",
        "cross_asset",
        CROSS_ASSET_DATASETS.etf_daily_dataset_ids,
        "pearson_daily_returns_v2",
        "correlation",
        tuple(window_id for window_id, _size in _CORRELATION_WINDOWS),
        20,
        "intersection",
        259_200,
        "zero",
        "macro_asset_correlations_v2",
        "market_comparison",
    ),
)

CALCULATION_REGISTRY = MappingProxyType({spec.feature_id: spec for spec in _CALCULATIONS})
if len(CALCULATION_REGISTRY) != len(_CALCULATIONS):
    raise RuntimeError("macro_calculation_registry_duplicate_feature")

_TREASURY_TENOR_YEARS = {
    "1M": 1 / 12,
    "1.5M": 1.5 / 12,
    "2M": 2 / 12,
    "3M": 3 / 12,
    "4M": 4 / 12,
    "6M": 0.5,
    "1Y": 1,
    "2Y": 2,
    "3Y": 3,
    "5Y": 5,
    "7Y": 7,
    "10Y": 10,
    "20Y": 20,
    "30Y": 30,
}


def calculate_curve_contract(series_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the tenor-native rates decision and chart contracts."""

    _require_calculation("rates.treasury_curve_cross_sections")
    _require_calculation("rates.matched_breakeven_curve")
    shape_spec = _require_calculation("rates.curve_shape")
    nominal = _curve_rows(series_rows, "treasury.daily_nominal_curve")
    real = _curve_rows(series_rows, "treasury.daily_real_curve")
    spreads = {
        "2s10s": _curve_spread_history(nominal, "2Y", "10Y"),
        "10s30s": _curve_spread_history(nominal, "10Y", "30Y"),
        "3m10s": _curve_spread_history(nominal, "3M", "10Y"),
        "5s30s": _curve_spread_history(nominal, "5Y", "30Y"),
    }
    return {
        "decision": _curve_decision(
            nominal,
            real,
            formula_version=shape_spec.formula_version,
        ),
        "curve": {
            "nominal_snapshots": _curve_snapshots(nominal),
            "real_snapshots": _curve_snapshots(real),
            "breakeven_snapshots": _matched_breakeven_snapshots(nominal, real),
            "spreads": spreads,
        },
    }


def calculate_series_statistics(
    series_rows: list[dict[str, Any]],
    dataset_ids: tuple[str, ...],
    *,
    percentile_dataset_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Calculate reader-facing levels, changes, samples and actual-history percentiles."""

    by_dataset = _series_by_dataset(series_rows)
    output: list[dict[str, Any]] = []
    for dataset_id in dataset_ids:
        rows = by_dataset.get(dataset_id, [])
        if not rows:
            continue
        latest = rows[-1]
        values = [float(row["value_numeric"]) for row in rows]
        latest_value = float(latest["value_numeric"])
        item: dict[str, Any] = {
            "dataset_id": dataset_id,
            "latest_value": latest_value,
            "unit": str(latest["unit"]),
            "as_of": str(latest["reference_date"]),
            "change_1w": _series_change(rows, 7),
            "change_1m": _series_change(rows, 30),
            "sample_count": int(latest.get("semantic_sample_count") or len(values)),
            "history_start": str(latest.get("semantic_history_start") or rows[0]["reference_date"]),
            "history_end": str(rows[-1]["reference_date"]),
            "source_url": str(latest["source_url"]),
            "history": [
                {"date": str(row["reference_date"]), "value": float(row["value_numeric"])} for row in rows[-500:]
            ],
        }
        if dataset_id in percentile_dataset_ids:
            item["percentile"] = (
                float(latest["semantic_percentile"])
                if latest.get("semantic_percentile") is not None
                else _percentile_rank(values, latest_value)
            )
        output.append(item)
    return output


def calculate_funding_cost_comparisons(
    series_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    spec = _require_calculation("credit.funding_cost_comparisons")
    by_dataset = _series_by_dataset(series_rows)
    comparisons = (
        ("IG yield − EFFR", "fred.bamlc0a0cmey", "fred.effr"),
        ("IG yield − 10Y Treasury", "fred.bamlc0a0cmey", "fred.dgs10"),
        ("HY yield − EFFR", "fred.bamlh0a0hym2ey", "fred.effr"),
        ("HY yield − 10Y Treasury", "fred.bamlh0a0hym2ey", "fred.dgs10"),
    )
    output = []
    for label, corporate_id, reference_id in comparisons:
        corporate = {row["reference_date"]: row for row in by_dataset.get(corporate_id, [])}
        reference = {row["reference_date"]: row for row in by_dataset.get(reference_id, [])}
        common_dates = corporate.keys() & reference.keys()
        if not common_dates:
            continue
        as_of = max(common_dates)
        corporate_row = corporate[as_of]
        reference_row = reference[as_of]
        output.append(
            {
                "label": label,
                "corporate_dataset_id": corporate_id,
                "reference_dataset_id": reference_id,
                "as_of": str(as_of),
                "value_bp": round(
                    (float(corporate_row["value_numeric"]) - float(reference_row["value_numeric"])) * 100,
                    4,
                ),
                "formula_version": spec.formula_version,
                "input_fact_ids": [
                    str(corporate_row["fact_id"]),
                    str(reference_row["fact_id"]),
                ],
            }
        )
    return output


def calculate_market_statistics(
    market_rows: list[dict[str, Any]],
    dataset_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in market_rows:
        dataset_id = str(row["dataset_id"])
        if dataset_id in dataset_ids and row.get("value_numeric") is not None:
            by_dataset[dataset_id].append(row)
    output = []
    for dataset_id in dataset_ids:
        rows = sorted(by_dataset.get(dataset_id, []), key=_market_order)
        if not rows:
            continue
        latest = rows[-1]
        output.append(
            {
                "dataset_id": dataset_id,
                "latest_value": float(latest["value_numeric"]),
                "unit": str(latest["unit"]),
                "as_of": _market_reference(latest),
                "market_time_ms": int(latest["observed_at_ms"]),
                "change_1d_pct": _market_pct_change(rows, 1),
                "change_1w_pct": _market_pct_change(rows, 7),
                "change_1m_pct": _market_pct_change(rows, 30),
                "source_url": str(latest["source_url"]),
            }
        )
    return output


def calculate_market_comparison(
    market_rows: list[dict[str, Any]],
    dataset_ids: tuple[str, ...],
    symbol_by_dataset: Mapping[str, str],
) -> dict[str, Any]:
    _require_calculation("cross_asset.normalized_returns")
    correlation_spec = _require_calculation("cross_asset.return_correlations")
    closes: dict[str, dict[date, float]] = defaultdict(dict)
    for row in market_rows:
        dataset_id = str(row["dataset_id"])
        if dataset_id not in dataset_ids or row.get("value_numeric") is None:
            continue
        reference_date = _market_date(row)
        if reference_date is not None:
            closes[dataset_id][reference_date] = float(row["value_numeric"])

    normalized: list[dict[str, Any]] = []
    daily_returns: dict[str, dict[date, float]] = {}
    for dataset_id in dataset_ids:
        ordered = sorted(closes.get(dataset_id, {}).items())[-260:]
        if not ordered or not ordered[0][1]:
            continue
        base = ordered[0][1]
        normalized.extend(
            {
                "symbol": symbol_by_dataset[dataset_id],
                "date": reference_date.isoformat(),
                "normalized_value": round(value / base * 100, 4),
            }
            for reference_date, value in ordered
        )
        daily_returns[dataset_id] = {
            current_date: current_value / prior_value - 1
            for (prior_date, prior_value), (current_date, current_value) in pairwise(ordered)
            if prior_date < current_date and prior_value
        }

    correlations = []
    for index, left in enumerate(dataset_ids):
        for right in dataset_ids[index + 1 :]:
            common_dates = sorted(daily_returns.get(left, {}).keys() & daily_returns.get(right, {}).keys())
            if len(common_dates) < correlation_spec.minimum_observations:
                continue
            for window_id, window_size in _CORRELATION_WINDOWS:
                selected_dates = common_dates[-window_size:]
                correlations.append(
                    {
                        "left": symbol_by_dataset[left],
                        "right": symbol_by_dataset[right],
                        "correlation": _correlation(
                            [daily_returns[left][item] for item in selected_dates],
                            [daily_returns[right][item] for item in selected_dates],
                        ),
                        "sample_count": len(selected_dates),
                        "window": window_id,
                    }
                )
    return {
        "normalized": normalized,
        "correlations": correlations,
        "correlation_contract": {
            "default_window": _DEFAULT_CORRELATION_WINDOW,
            "supported_windows": list(correlation_spec.windows),
            "minimum_common_observations": correlation_spec.minimum_observations,
            "presentation_derivation": "undirected_pairs_mirrored_with_unit_diagonal",
        },
    }


def _require_calculation(feature_id: str) -> CalculationSpec:
    try:
        return CALCULATION_REGISTRY[feature_id]
    except KeyError as exc:
        raise ValueError(f"unregistered_calculation:{feature_id}") from exc


def _curve_rows(
    series_rows: list[dict[str, Any]],
    dataset_id: str,
) -> dict[date, dict[str, dict[str, Any]]]:
    rows: dict[date, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in series_rows:
        if row["dataset_id"] != dataset_id or row.get("value_numeric") is None:
            continue
        reference_date = row["reference_date"]
        tenor = str(row["series_id"])
        current = rows[reference_date].get(tenor)
        if current is None or _curve_row_order(row) > _curve_row_order(current):
            rows[reference_date][tenor] = row
    return dict(rows)


def _curve_snapshots(
    rows: dict[date, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    latest = max(rows)
    snapshots = []
    for window in ("current", "previous", "1w", "mtd", "3m"):
        selected = _curve_snapshot_date(rows.keys(), latest, window)
        if selected is None:
            continue
        snapshots.append(
            {
                "window": window,
                "as_of": selected.isoformat(),
                "points": [
                    {
                        "tenor": tenor,
                        "years": _TREASURY_TENOR_YEARS[tenor],
                        "yield_pct": float(row["value_numeric"]),
                        "dataset_id": str(row["dataset_id"]),
                        "source_role": DATASET_REGISTRY[str(row["dataset_id"])].source_role,
                        "fact_id": str(row["fact_id"]),
                        "source_url": str(row["source_url"]),
                    }
                    for tenor, row in sorted(
                        rows[selected].items(),
                        key=lambda item: _TREASURY_TENOR_YEARS.get(
                            item[0],
                            math.inf,
                        ),
                    )
                    if tenor in _TREASURY_TENOR_YEARS
                ],
            }
        )
    return snapshots


def _matched_breakeven_snapshots(
    nominal: dict[date, dict[str, dict[str, Any]]],
    real: dict[date, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    common_dates = nominal.keys() & real.keys()
    if not common_dates:
        return []
    latest = max(common_dates)
    snapshots = []
    for window in ("current", "previous", "1w", "mtd", "3m"):
        selected = _curve_snapshot_date(common_dates, latest, window)
        if selected is None:
            continue
        common_tenors = nominal[selected].keys() & real[selected].keys()
        snapshots.append(
            {
                "window": window,
                "as_of": selected.isoformat(),
                "points": [
                    {
                        "tenor": tenor,
                        "years": _TREASURY_TENOR_YEARS[tenor],
                        "breakeven_pct": round(
                            _curve_value(nominal[selected][tenor]) - _curve_value(real[selected][tenor]),
                            4,
                        ),
                        "input_fact_ids": [
                            str(nominal[selected][tenor]["fact_id"]),
                            str(real[selected][tenor]["fact_id"]),
                        ],
                    }
                    for tenor in sorted(
                        common_tenors,
                        key=lambda value: _TREASURY_TENOR_YEARS.get(value, math.inf),
                    )
                    if tenor in _TREASURY_TENOR_YEARS
                ],
            }
        )
    return snapshots


def _curve_spread_history(
    rows: dict[date, dict[str, dict[str, Any]]],
    short_tenor: str,
    long_tenor: str,
) -> list[dict[str, Any]]:
    return [
        {
            "date": reference_date.isoformat(),
            "value_bp": round(
                (_curve_value(values[long_tenor]) - _curve_value(values[short_tenor])) * 100,
                4,
            ),
            "input_fact_ids": [
                str(values[short_tenor]["fact_id"]),
                str(values[long_tenor]["fact_id"]),
            ],
        }
        for reference_date, values in sorted(rows.items())
        if short_tenor in values and long_tenor in values
    ][-2_500:]


_PRIMARY_CURVE_TENORS = ("2Y", "10Y", "30Y")
_DECISION_WINDOWS = ("1d", "1w", "mtd", "3m", "past_30d")
_WINDOW_SELECTION_POLICY = {
    "1d": "previous_treasury_observation",
    "1w": "bounded_previous_observation_4_calendar_days",
    "mtd": "first_available_treasury_observation_in_calendar_month",
    "3m": "bounded_previous_observation_4_calendar_days",
    "past_30d": "bounded_previous_observation_4_calendar_days",
}


def _curve_decision(
    nominal: dict[date, dict[str, dict[str, Any]]],
    real: dict[date, dict[str, dict[str, Any]]],
    *,
    formula_version: str,
) -> dict[str, Any]:
    reference_date: date | None
    latest_by_tenor = {
        tenor: max(
            (reference_date for reference_date, values in nominal.items() if tenor in values),
            default=None,
        )
        for tenor in _PRIMARY_CURVE_TENORS
    }
    available_dates = {value for value in latest_by_tenor.values() if value is not None}
    if len(available_dates) == 1 and all(latest_by_tenor.values()):
        completeness_state = "complete"
        reference_date = next(iter(available_dates))
        completeness_reason = None
    elif all(latest_by_tenor.values()):
        completeness_state = "unaligned"
        reference_date = None
        completeness_reason = "2Y、10Y、30Y 的最新财政部观测日不一致，关闭当日曲线结论。"
    else:
        completeness_state = "incomplete"
        reference_date = None
        completeness_reason = "2Y、10Y、30Y 至少一个期限缺少财政部观测，关闭当日曲线结论。"

    tenor_matrix = [_tenor_decision_row(nominal, tenor, latest_by_tenor[tenor]) for tenor in _PRIMARY_CURVE_TENORS]
    one_day_changes = {
        row["tenor"]: next((item for item in row["windows"] if item["window"] == "1d"), None) for row in tenor_matrix
    }
    aligned_one_day = completeness_state == "complete" and all(
        item is not None and item["state"] in {"available", "baseline"} for item in one_day_changes.values()
    )
    available_one_day_changes = {tenor: cast(dict[str, Any], one_day_changes[tenor]) for tenor in _PRIMARY_CURVE_TENORS}
    headline = (
        _curve_headline(
            reference_date,
            {tenor: float(available_one_day_changes[tenor]["change_bp"]) for tenor in _PRIMARY_CURVE_TENORS},
        )
        if aligned_one_day and reference_date is not None
        else None
    )
    decompositions = [_curve_decomposition(nominal, real, tenor, latest_by_tenor[tenor]) for tenor in ("10Y", "30Y")]
    bounded_assessments = [
        {
            "assessment_id": f"{item['tenor'].lower()}_one_day_decomposition",
            "statement": str(item["assessment"]),
            "input_fact_ids": list(item["input_fact_ids"]),
            "uncertainty": "仅描述名义收益率、实际收益率与 Breakeven 的同日机械分解。",
        }
        for item in decompositions
        if item["state"] == "available" and item["assessment"] is not None
    ]
    facts = (
        [
            {
                "statement": headline,
                "input_fact_ids": [
                    fact_id
                    for tenor in _PRIMARY_CURVE_TENORS
                    for fact_id in available_one_day_changes[tenor]["input_fact_ids"]
                ],
            }
        ]
        if headline is not None
        else []
    )
    latest_observations = []
    for tenor in _PRIMARY_CURVE_TENORS:
        observation_date = latest_by_tenor[tenor]
        latest_observations.append(
            {
                "tenor": tenor,
                "reference_date": (observation_date.isoformat() if observation_date is not None else None),
                "fact_id": (str(nominal[observation_date][tenor]["fact_id"]) if observation_date is not None else None),
            }
        )
    decision_state = (
        "available"
        if aligned_one_day
        else "insufficient_history"
        if completeness_state == "complete"
        else completeness_state
    )
    return {
        "state": decision_state,
        "reference_date": reference_date.isoformat() if reference_date is not None else None,
        "headline": headline,
        "session_completeness": {
            "state": completeness_state,
            "reference_date": reference_date.isoformat() if reference_date is not None else None,
            "required_tenors": list(_PRIMARY_CURVE_TENORS),
            "latest_observations": latest_observations,
            "reason": completeness_reason,
        },
        "tenor_matrix": tenor_matrix,
        "spread_summary": _curve_spread_summary(
            nominal,
            reference_date=reference_date,
            state=completeness_state,
        ),
        "decompositions": decompositions,
        "classifications": [
            _curve_window_classification(
                nominal,
                tenor_matrix,
                window=window,
                reference_date=reference_date,
                completeness_state=completeness_state,
                formula_version=formula_version,
            )
            for window in ("1d", "1w", "mtd", "3m")
        ],
        "explanation": {
            "facts": facts,
            "bounded_assessments": bounded_assessments,
            "hypotheses": [],
        },
        "source_policy": {
            "decision_primary_dataset_ids": [
                "treasury.daily_nominal_curve",
                "treasury.daily_real_curve",
            ],
            "history_only_dataset_ids": [
                "fred.dgs2",
                "fred.dgs10",
                "fred.dgs30",
                "fred.dfii10",
                "fred.t10yie",
            ],
            "selection_policy": "treasury_completed_session_primary_fred_history_only",
        },
    }


def _tenor_decision_row(
    rows: dict[date, dict[str, dict[str, Any]]],
    tenor: str,
    current_date: date | None,
) -> dict[str, Any]:
    current = rows.get(current_date, {}).get(tenor) if current_date is not None else None
    return {
        "tenor": tenor,
        "current": _curve_fact_point(current, tenor) if current is not None else None,
        "windows": [_curve_window_change(rows, tenor, current_date, window) for window in _DECISION_WINDOWS],
    }


def _curve_window_change(
    rows: dict[date, dict[str, dict[str, Any]]],
    tenor: str,
    current_date: date | None,
    window: str,
) -> dict[str, Any]:
    selection_policy = _WINDOW_SELECTION_POLICY[window]
    if current_date is None or tenor not in rows.get(current_date, {}):
        return {
            "window": window,
            "state": "unavailable",
            "current_date": current_date.isoformat() if current_date is not None else None,
            "baseline_date": None,
            "change_bp": None,
            "selection_policy": selection_policy,
            "input_fact_ids": [],
        }
    dates = sorted(reference_date for reference_date, values in rows.items() if tenor in values)
    baseline_date = _curve_baseline_date(dates, current_date, window)
    if baseline_date is None:
        return {
            "window": window,
            "state": "unavailable",
            "current_date": current_date.isoformat(),
            "baseline_date": None,
            "change_bp": None,
            "selection_policy": selection_policy,
            "input_fact_ids": [str(rows[current_date][tenor]["fact_id"])],
        }
    current = rows[current_date][tenor]
    baseline = rows[baseline_date][tenor]
    return {
        "window": window,
        "state": "baseline" if baseline_date == current_date else "available",
        "current_date": current_date.isoformat(),
        "baseline_date": baseline_date.isoformat(),
        "change_bp": round((_curve_value(current) - _curve_value(baseline)) * 100, 4),
        "selection_policy": selection_policy,
        "input_fact_ids": [str(baseline["fact_id"]), str(current["fact_id"])],
    }


def _curve_spread_summary(
    rows: dict[date, dict[str, dict[str, Any]]],
    *,
    reference_date: date | None,
    state: str,
) -> list[dict[str, Any]]:
    definitions = (
        ("2s10s", "2Y–10Y", "2Y", "10Y"),
        ("10s30s", "10Y–30Y", "10Y", "30Y"),
    )
    if state != "complete" or reference_date is None:
        return [
            {
                "spread_id": spread_id,
                "label": label,
                "state": state,
                "current_date": None,
                "prior_date": None,
                "value_bp": None,
                "change_1d_bp": None,
                "input_fact_ids": [],
            }
            for spread_id, label, _short, _long in definitions
        ]
    output: list[dict[str, Any]] = []
    for spread_id, label, short, long in definitions:
        current_values = rows.get(reference_date, {})
        common_prior_dates = [
            candidate
            for candidate, values in rows.items()
            if candidate < reference_date and short in values and long in values
        ]
        prior_date = max(common_prior_dates, default=None)
        if short not in current_values or long not in current_values or prior_date is None:
            output.append(
                {
                    "spread_id": spread_id,
                    "label": label,
                    "state": "insufficient_history",
                    "current_date": reference_date.isoformat(),
                    "prior_date": None,
                    "value_bp": None,
                    "change_1d_bp": None,
                    "input_fact_ids": [],
                }
            )
            continue
        current_spread = (_curve_value(current_values[long]) - _curve_value(current_values[short])) * 100
        prior_values = rows[prior_date]
        prior_spread = (_curve_value(prior_values[long]) - _curve_value(prior_values[short])) * 100
        output.append(
            {
                "spread_id": spread_id,
                "label": label,
                "state": "available",
                "current_date": reference_date.isoformat(),
                "prior_date": prior_date.isoformat(),
                "value_bp": round(current_spread, 4),
                "change_1d_bp": round(current_spread - prior_spread, 4),
                "input_fact_ids": [
                    str(prior_values[short]["fact_id"]),
                    str(prior_values[long]["fact_id"]),
                    str(current_values[short]["fact_id"]),
                    str(current_values[long]["fact_id"]),
                ],
            }
        )
    return output


def _curve_decomposition(
    nominal: dict[date, dict[str, dict[str, Any]]],
    real: dict[date, dict[str, dict[str, Any]]],
    tenor: str,
    current_date: date | None,
) -> dict[str, Any]:
    nominal_window = _curve_window_change(nominal, tenor, current_date, "1d")
    base = {
        "tenor": tenor,
        "state": "insufficient_history",
        "current_date": nominal_window["current_date"],
        "prior_date": nominal_window["baseline_date"],
        "nominal_change_bp": nominal_window["change_bp"],
        "real_change_bp": None,
        "breakeven_change_bp": None,
        "assessment_state": None,
        "assessment": None,
        "input_fact_ids": list(nominal_window["input_fact_ids"]),
        "gap": "名义曲线缺少前一财政部交易日观测。",
    }
    if nominal_window["state"] not in {"available", "baseline"}:
        return base
    if current_date is None:
        return base
    prior_date = date.fromisoformat(str(nominal_window["baseline_date"]))
    if tenor not in real.get(current_date, {}) or tenor not in real.get(prior_date, {}):
        return {
            **base,
            "state": "unaligned",
            "gap": "名义与实际财政部曲线未在当前日和前一交易日同时对齐，未进行跨日拼接。",
        }
    real_current = real[current_date][tenor]
    real_prior = real[prior_date][tenor]
    real_change = round((_curve_value(real_current) - _curve_value(real_prior)) * 100, 4)
    nominal_change = float(nominal_window["change_bp"])
    breakeven_change = round(nominal_change - real_change, 4)
    assessment_state, assessment = _decomposition_assessment(
        tenor,
        nominal_change=nominal_change,
        real_change=real_change,
        breakeven_change=breakeven_change,
    )
    return {
        **base,
        "state": "available",
        "real_change_bp": real_change,
        "breakeven_change_bp": breakeven_change,
        "assessment_state": assessment_state,
        "assessment": assessment,
        "input_fact_ids": [
            *nominal_window["input_fact_ids"],
            str(real_prior["fact_id"]),
            str(real_current["fact_id"]),
        ],
        "gap": None,
    }


def _decomposition_assessment(
    tenor: str,
    *,
    nominal_change: float,
    real_change: float,
    breakeven_change: float,
) -> tuple[str, str]:
    if abs(breakeven_change) > abs(real_change) + 1:
        return (
            "inflation_compensation_dominant",
            f"{tenor} 名义收益率{_direction_text(nominal_change)}主要由通胀补偿变化推动。",
        )
    if abs(real_change) > abs(breakeven_change) + 1:
        return (
            "real_yield_dominant",
            f"{tenor} 名义收益率{_direction_text(nominal_change)}主要由实际收益率变化推动。",
        )
    return (
        "mixed_real_and_inflation_compensation",
        f"{tenor} 名义收益率变化同时包含实际收益率与通胀补偿变化。",
    )


def _curve_window_classification(
    rows: dict[date, dict[str, dict[str, Any]]],
    tenor_matrix: list[dict[str, Any]],
    *,
    window: str,
    reference_date: date | None,
    completeness_state: str,
    formula_version: str,
) -> dict[str, Any]:
    empty_inputs = {
        "current_as_of": reference_date.isoformat() if reference_date is not None else None,
        "prior_as_of": None,
        "2y_change_bp": None,
        "10y_change_bp": None,
        "30y_change_bp": None,
        "level_change_bp": None,
        "slope_change_bp": None,
        "curvature_change_bp": None,
        "current_2s10s_bp": None,
        "current_10s30s_bp": None,
    }
    if completeness_state != "complete" or reference_date is None:
        return {
            "window": window,
            "state": "unaligned" if completeness_state == "unaligned" else "insufficient_tenors",
            "label": "期限观测日未对齐" if completeness_state == "unaligned" else "期限不足",
            "formula_version": formula_version,
            "inputs": empty_inputs,
        }
    changes = {
        row["tenor"]: next((item for item in row["windows"] if item["window"] == window), None) for row in tenor_matrix
    }
    if any(item is None or item["state"] not in {"available", "baseline"} for item in changes.values()):
        return {
            "window": window,
            "state": "insufficient_history",
            "label": "历史不足",
            "formula_version": formula_version,
            "inputs": empty_inputs,
        }
    available_changes = {tenor: cast(dict[str, Any], item) for tenor, item in changes.items()}
    prior_dates = {str(item["baseline_date"]) for item in available_changes.values()}
    if len(prior_dates) != 1:
        return {
            "window": window,
            "state": "unaligned",
            "label": "窗口基准日未对齐",
            "formula_version": formula_version,
            "inputs": empty_inputs,
        }
    prior_date = date.fromisoformat(next(iter(prior_dates)))
    current_values = rows[reference_date]
    prior_values = rows[prior_date]
    if any(tenor not in current_values or tenor not in prior_values for tenor in _PRIMARY_CURVE_TENORS):
        return {
            "window": window,
            "state": "insufficient_tenors",
            "label": "期限不足",
            "formula_version": formula_version,
            "inputs": empty_inputs,
        }
    current_points = {tenor: _curve_value(current_values[tenor]) for tenor in _PRIMARY_CURVE_TENORS}
    prior_points = {tenor: _curve_value(prior_values[tenor]) for tenor in _PRIMARY_CURVE_TENORS}
    tenor_changes = {tenor: (current_points[tenor] - prior_points[tenor]) * 100 for tenor in _PRIMARY_CURVE_TENORS}
    current_level = sum(current_points.values()) / len(current_points)
    prior_level = sum(prior_points.values()) / len(prior_points)
    current_slope = current_points["10Y"] - current_points["2Y"]
    prior_slope = prior_points["10Y"] - prior_points["2Y"]
    current_curvature = 2 * current_points["10Y"] - current_points["2Y"] - current_points["30Y"]
    prior_curvature = 2 * prior_points["10Y"] - prior_points["2Y"] - prior_points["30Y"]
    level_change = (current_level - prior_level) * 100
    slope_change = (current_slope - prior_slope) * 100
    curvature_change = (current_curvature - prior_curvature) * 100
    threshold = 5.0
    if tenor_changes["2Y"] < 0 < max(tenor_changes["10Y"], tenor_changes["30Y"]) and slope_change >= threshold:
        state, label = "twist_steepening", "扭转式陡峭化"
    elif abs(slope_change) < threshold and level_change >= threshold:
        state, label = "parallel_up", "近似平行上移"
    elif abs(slope_change) < threshold and level_change <= -threshold:
        state, label = "parallel_down", "近似平行下移"
    elif slope_change >= threshold and level_change >= 0:
        state, label = "bear_steepening", "熊市陡峭化"
    elif slope_change >= threshold:
        state, label = "bull_steepening", "牛市陡峭化"
    elif slope_change <= -threshold and level_change >= 0:
        state, label = "bear_flattening", "熊市平坦化"
    elif slope_change <= -threshold:
        state, label = "bull_flattening", "牛市平坦化"
    else:
        state, label = "stable", "形态大致稳定"
    return {
        "window": window,
        "state": state,
        "label": label,
        "formula_version": formula_version,
        "inputs": {
            "current_as_of": reference_date.isoformat(),
            "prior_as_of": prior_date.isoformat(),
            "2y_change_bp": round(tenor_changes["2Y"], 4),
            "10y_change_bp": round(tenor_changes["10Y"], 4),
            "30y_change_bp": round(tenor_changes["30Y"], 4),
            "level_change_bp": round(level_change, 4),
            "slope_change_bp": round(slope_change, 4),
            "curvature_change_bp": round(curvature_change, 4),
            "current_2s10s_bp": round((current_points["10Y"] - current_points["2Y"]) * 100, 4),
            "current_10s30s_bp": round((current_points["30Y"] - current_points["10Y"]) * 100, 4),
        },
    }


def _curve_snapshot_date(
    dates: Iterable[date],
    latest: date,
    window: str,
) -> date | None:
    ordered = sorted(set(dates))
    if window == "current":
        return latest
    if window == "previous":
        return max((value for value in ordered if value < latest), default=None)
    if window == "mtd":
        month_dates = [value for value in ordered if value.year == latest.year and value.month == latest.month]
        return min(month_dates, default=None)
    days = {"1w": 7, "3m": 90}[window]
    return _bounded_previous_date(ordered, latest - timedelta(days=days))


def _curve_baseline_date(
    dates: list[date],
    current_date: date,
    window: str,
) -> date | None:
    if window == "1d":
        return max((value for value in dates if value < current_date), default=None)
    if window == "mtd":
        month_dates = [
            value for value in dates if value.year == current_date.year and value.month == current_date.month
        ]
        return min(month_dates, default=None)
    days = {"1w": 7, "3m": 90, "past_30d": 30}[window]
    return _bounded_previous_date(dates, current_date - timedelta(days=days))


def _bounded_previous_date(dates: list[date], target: date) -> date | None:
    candidates = [value for value in dates if target - timedelta(days=4) <= value <= target]
    return max(candidates, default=None)


def _curve_fact_point(row: dict[str, Any], tenor: str) -> dict[str, Any]:
    dataset_id = str(row["dataset_id"])
    return {
        "tenor": tenor,
        "reference_date": str(row["reference_date"]),
        "yield_pct": _curve_value(row),
        "dataset_id": dataset_id,
        "source_role": DATASET_REGISTRY[dataset_id].source_role,
        "fact_id": str(row["fact_id"]),
        "source_url": str(row["source_url"]),
    }


def _curve_value(row: Mapping[str, Any]) -> float:
    return float(row["value_numeric"])


def _curve_row_order(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row.get("vintage_date") or ""),
        int(row.get("received_at_ms") or 0),
        str(row.get("fact_id") or ""),
    )


def _curve_headline(reference_date: date, changes: Mapping[str, float]) -> str:
    details = "，".join(f"{tenor} {_direction_text(changes[tenor])}" for tenor in _PRIMARY_CURVE_TENORS)
    return f"最近完整交易日：{details}（{reference_date.isoformat()}）"


def _direction_text(value: float) -> str:
    if value > 0:
        return f"上行{value:g}bp"
    if value < 0:
        return f"下行{abs(value):g}bp"
    return "持平"


def _series_by_dataset(
    series_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in series_rows:
        if row.get("value_numeric") is not None:
            by_dataset[str(row["dataset_id"])].append(row)
    for rows in by_dataset.values():
        rows.sort(
            key=lambda row: (
                row["reference_date"],
                row["vintage_date"],
                int(row["received_at_ms"]),
            )
        )
    return dict(by_dataset)


def _series_change(rows: list[dict[str, Any]], days: int) -> float | None:
    latest = rows[-1]
    target = latest["reference_date"] - timedelta(days=days)
    candidates = [row for row in rows[:-1] if row["reference_date"] <= target]
    if not candidates:
        return None
    return round(
        float(latest["value_numeric"]) - float(candidates[-1]["value_numeric"]),
        6,
    )


def _percentile_rank(values: list[float], latest: float) -> float | None:
    if not values:
        return None
    return round(sum(value <= latest for value in values) / len(values) * 100, 2)


def _market_pct_change(rows: list[dict[str, Any]], days: int) -> float | None:
    if len(rows) < 2:
        return None
    latest = rows[-1]
    latest_date = _market_date(latest)
    if latest_date is None:
        return None
    target = latest_date - timedelta(days=days)
    candidates = [row for row in rows[:-1] if (_market_date(row) or date.max) <= target]
    prior = candidates[-1] if candidates else rows[-2] if days == 1 else None
    if prior is None:
        return None
    previous_value = float(prior["value_numeric"])
    if not previous_value:
        return None
    return round(
        (float(latest["value_numeric"]) / previous_value - 1) * 100,
        6,
    )


def _market_date(row: dict[str, Any]) -> date | None:
    for key in ("reference_date", "session_date", "business_date"):
        value = row.get(key)
        if isinstance(value, date):
            return value
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                pass
    observed_at_ms = row.get("observed_at_ms")
    if observed_at_ms is None:
        return None
    return datetime.fromtimestamp(int(observed_at_ms) / 1_000, tz=UTC).date()


def _market_reference(row: dict[str, Any]) -> str:
    value = _market_date(row)
    return value.isoformat() if value is not None else ""


def _market_order(row: dict[str, Any]) -> tuple[str, int, int]:
    return (
        _market_reference(row),
        int(row.get("observed_at_ms") or 0),
        int(row["received_at_ms"]),
    )


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_variance = sum((x - left_mean) ** 2 for x in left)
    right_variance = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_variance * right_variance)
    return round(numerator / denominator, 4) if denominator else None


__all__ = [
    "CALCULATION_REGISTRY",
    "NATURAL_CHANGE_REGISTRY",
    "CalculationSpec",
    "NaturalChangeCalculationSpec",
    "calculate_curve_contract",
    "calculate_funding_cost_comparisons",
    "calculate_market_comparison",
    "calculate_market_statistics",
    "calculate_series_statistics",
    "natural_change_calculation",
]
