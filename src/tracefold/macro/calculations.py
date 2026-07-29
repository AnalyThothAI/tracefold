from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Literal

from tracefold.macro.domain import DatasetSpec, MacroModuleId
from tracefold.macro.registry import DATASET_REGISTRY

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
    materialize_feature: bool = True


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
        "rates.curve_10y2y",
        "rates_fed",
        ("fred.dgs10", "fred.dgs2"),
        "difference_v1",
        "basis_points",
        ("latest", "1w", "1m"),
        1,
        "intersection",
        259_200,
        "zero_and_5y_distribution",
        "feature_point_v1",
        "difference_x100",
    ),
    CalculationSpec(
        "rates.real_10y",
        "rates_fed",
        ("fred.dfii10",),
        "identity_v1",
        "percent",
        ("latest", "1w", "1m"),
        1,
        "latest_available",
        259_200,
        "5y_distribution",
        "feature_point_v1",
        "identity",
    ),
    CalculationSpec(
        "inflation.cpi_yoy",
        "economy_inflation",
        ("fred.cpiaucsl",),
        "year_over_year_pct_v1",
        "percent",
        ("latest", "3m", "1y"),
        13,
        "latest_available",
        3_974_400,
        "pre_2020_and_5y_distribution",
        "feature_point_v1",
        "yoy_pct",
    ),
    CalculationSpec(
        "inflation.core_pce_yoy",
        "economy_inflation",
        ("fred.pcepilfe",),
        "year_over_year_pct_v1",
        "percent",
        ("latest", "3m", "1y"),
        13,
        "latest_available",
        3_974_400,
        "pre_2020_and_5y_distribution",
        "feature_point_v1",
        "yoy_pct",
    ),
    CalculationSpec(
        "labor.payroll_monthly_change",
        "economy_inflation",
        ("fred.payems",),
        "first_difference_v1",
        "thousands_persons",
        ("latest", "3m"),
        2,
        "latest_available",
        3_974_400,
        "5y_distribution",
        "feature_point_v1",
        "difference",
    ),
    CalculationSpec(
        "liquidity.net_liquidity",
        "liquidity_funding",
        ("fred.walcl", "fred.wtregen", "fred.rrpontsyd"),
        "fed_assets_minus_tga_rrp_v1",
        "billions_usd",
        ("latest", "1m", "3m"),
        1,
        "latest_available",
        950_400,
        "5y_distribution",
        "feature_point_v1",
        "net_liquidity",
    ),
    CalculationSpec(
        "credit.hy_ig_oas_gap",
        "credit",
        ("fred.bamlh0a0hym2", "fred.bamlc0a0cm"),
        "difference_v1",
        "basis_points",
        ("latest", "1w", "1m"),
        1,
        "intersection",
        259_200,
        "5y_distribution",
        "feature_point_v1",
        "difference_x100",
    ),
    CalculationSpec(
        "volatility.vix_term_spread",
        "volatility",
        ("fred.vixcls", "fred.vxvcls"),
        "difference_v1",
        "index_points",
        ("latest", "1w", "1m"),
        1,
        "intersection",
        259_200,
        "5y_distribution",
        "feature_point_v1",
        "difference",
    ),
    CalculationSpec(
        "rates.treasury_curve_cross_sections",
        "rates_fed",
        ("treasury.daily_nominal_curve", "treasury.daily_real_curve"),
        "treasury_curve_contract_v2",
        "percent",
        ("current", "1w", "1m", "3m"),
        1,
        "latest_available",
        259_200,
        "prior_cross_sections",
        "macro_rates_curve_v2",
        "curve_contract",
        False,
    ),
    CalculationSpec(
        "rates.matched_breakeven_curve",
        "rates_fed",
        ("treasury.daily_nominal_curve", "treasury.daily_real_curve"),
        "matched_nominal_minus_real_v1",
        "percent",
        ("current", "1w", "1m", "3m"),
        1,
        "latest_available",
        259_200,
        "matching_tenor_and_window",
        "macro_breakeven_snapshots_v1",
        "curve_contract",
        False,
    ),
    CalculationSpec(
        "rates.curve_shape",
        "rates_fed",
        ("treasury.daily_nominal_curve",),
        "level_slope_curvature_classification_v2",
        "basis_points",
        ("current", "1w"),
        2,
        "latest_available",
        259_200,
        "five_basis_point_materiality",
        "macro_curve_classification_v2",
        "curve_contract",
        False,
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
        False,
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
        False,
    ),
    CalculationSpec(
        "cross_asset.normalized_returns",
        "cross_asset",
        (
            "nasdaq.spy.daily",
            "nasdaq.qqq.daily",
            "nasdaq.iwm.daily",
            "nasdaq.tlt.daily",
            "nasdaq.ief.daily",
            "nasdaq.lqd.daily",
            "nasdaq.hyg.daily",
            "nasdaq.dxy.daily",
            "nasdaq.gld.daily",
            "nasdaq.uso.daily",
        ),
        "normalized_to_100_v1",
        "index",
        ("up_to_260_observations",),
        1,
        "latest_available",
        259_200,
        "first_observation_equals_100",
        "macro_normalized_asset_points_v1",
        "market_comparison",
        False,
    ),
    CalculationSpec(
        "cross_asset.return_correlations",
        "cross_asset",
        (
            "nasdaq.spy.daily",
            "nasdaq.qqq.daily",
            "nasdaq.iwm.daily",
            "nasdaq.tlt.daily",
            "nasdaq.ief.daily",
            "nasdaq.lqd.daily",
            "nasdaq.hyg.daily",
            "nasdaq.dxy.daily",
            "nasdaq.gld.daily",
            "nasdaq.uso.daily",
        ),
        "pearson_daily_returns_v1",
        "correlation",
        ("up_to_120_daily_returns",),
        20,
        "intersection",
        259_200,
        "zero",
        "macro_asset_correlations_v1",
        "market_comparison",
        False,
    ),
)

CALCULATION_REGISTRY = MappingProxyType({spec.feature_id: spec for spec in _CALCULATIONS})

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


def calculate_features(
    series_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for row in series_rows:
        by_dataset.setdefault(str(row["dataset_id"]), []).append(row)
    for rows in by_dataset.values():
        rows.sort(key=lambda row: row["reference_date"])

    features: list[dict[str, Any]] = []
    for spec in _CALCULATIONS:
        if not spec.materialize_feature:
            continue
        computed = _calculate(spec, by_dataset)
        if computed is not None:
            features.append(computed)
    return features


def calculate_curve_contract(series_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the serving curve contract from registered deterministic calculations."""

    _require_calculation("rates.treasury_curve_cross_sections")
    _require_calculation("rates.matched_breakeven_curve")
    shape_spec = _require_calculation("rates.curve_shape")
    nominal = _curve_rows(series_rows, "treasury.daily_nominal_curve")
    real = _curve_rows(series_rows, "treasury.daily_real_curve")
    nominal_snapshots = _curve_snapshots(nominal)
    real_snapshots = _curve_snapshots(real)
    spreads = {
        "2s10s": _curve_spread_history(nominal, "2Y", "10Y"),
        "3m10s": _curve_spread_history(nominal, "3M", "10Y"),
        "5s30s": _curve_spread_history(nominal, "5Y", "30Y"),
    }
    return {
        "nominal_snapshots": nominal_snapshots,
        "real_snapshots": real_snapshots,
        "breakeven_snapshots": _matched_breakeven_snapshots(nominal, real),
        "spreads": spreads,
        "classification": _curve_classification(
            nominal_snapshots,
            spreads,
            formula_version=shape_spec.formula_version,
        ),
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
            "sample_count": len(values),
            "history_start": str(rows[0]["reference_date"]),
            "history_end": str(rows[-1]["reference_date"]),
            "source_url": str(latest["source_url"]),
            "history": [
                {"date": str(row["reference_date"]), "value": float(row["value_numeric"])} for row in rows[-500:]
            ],
        }
        if dataset_id in percentile_dataset_ids:
            item["percentile"] = _percentile_rank(values, latest_value)
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
) -> dict[str, list[dict[str, Any]]]:
    _require_calculation("cross_asset.normalized_returns")
    _require_calculation("cross_asset.return_correlations")
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
        return_window = ordered[-121:]
        daily_returns[dataset_id] = {
            current_date: current_value / prior_value - 1
            for (prior_date, prior_value), (current_date, current_value) in pairwise(return_window)
            if prior_date < current_date and prior_value
        }

    correlations = []
    for index, left in enumerate(dataset_ids):
        for right in dataset_ids[index + 1 :]:
            common_dates = sorted(daily_returns.get(left, {}).keys() & daily_returns.get(right, {}).keys())
            if len(common_dates) < 20:
                continue
            correlations.append(
                {
                    "left": symbol_by_dataset[left],
                    "right": symbol_by_dataset[right],
                    "correlation": _correlation(
                        [daily_returns[left][item] for item in common_dates],
                        [daily_returns[right][item] for item in common_dates],
                    ),
                    "sample_count": len(common_dates),
                    "window": "up_to_120_daily_returns",
                }
            )
    return {"normalized": normalized, "correlations": correlations}


def _calculate(
    spec: CalculationSpec,
    by_dataset: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    inputs = [by_dataset.get(dataset_id, []) for dataset_id in spec.input_dataset_ids]
    if any(len(rows) < spec.minimum_observations for rows in inputs):
        return None
    if spec.operation in {"identity", "difference", "yoy_pct"}:
        return _single_series(spec, inputs[0])
    if spec.operation in {"difference_x100", "net_liquidity"}:
        return _multi_series(spec, inputs)
    raise ValueError(f"unknown_calculation_operation:{spec.operation}")


def _single_series(
    spec: CalculationSpec,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    latest = rows[-1]
    latest_value = _numeric(latest)
    if latest_value is None:
        return None
    inputs = [_input_ref(latest)]
    value = latest_value
    if spec.operation == "difference":
        previous = _numeric(rows[-2])
        if previous is None:
            return None
        value = latest_value - previous
        inputs.append(_input_ref(rows[-2]))
    elif spec.operation == "yoy_pct":
        previous = _numeric(rows[-13])
        if previous is None or previous == 0.0:
            return None
        value = (latest_value / previous - 1.0) * 100.0
        inputs.append(_input_ref(rows[-13]))
    return _feature_payload(spec, latest["reference_date"], value, inputs)


def _multi_series(
    spec: CalculationSpec,
    inputs: list[list[dict[str, Any]]],
) -> dict[str, Any] | None:
    by_date = [{row["reference_date"]: row for row in rows if _numeric(row) is not None} for rows in inputs]
    if spec.gap_policy == "intersection":
        common_dates = set(by_date[0])
        for rows_by_date in by_date[1:]:
            common_dates &= set(rows_by_date)
        if not common_dates:
            return None
        as_of_date = max(common_dates)
        rows = [rows_by_date[as_of_date] for rows_by_date in by_date]
    else:
        rows = [max(rows_by_date.values(), key=lambda row: row["reference_date"]) for rows_by_date in by_date]
        as_of_date = min(row["reference_date"] for row in rows)
    values = [_numeric(row) for row in rows]
    if any(value is None for value in values):
        return None
    numeric_values = [float(value) for value in values if value is not None]
    if spec.operation == "difference_x100":
        value = (numeric_values[0] - numeric_values[1]) * 100.0
    elif spec.operation == "net_liquidity":
        value = numeric_values[0] / 1_000.0 - numeric_values[1] - numeric_values[2]
    else:
        raise ValueError(f"unknown_multi_series_operation:{spec.operation}")
    return _feature_payload(spec, as_of_date, value, [_input_ref(row) for row in rows])


def _feature_payload(
    spec: CalculationSpec,
    as_of_date: date,
    value: float,
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "feature_id": spec.feature_id,
        "module_id": spec.module_id,
        "as_of_date": as_of_date,
        "formula_version": spec.formula_version,
        "value_numeric": round(value, 6),
        "unit": spec.unit,
        "inputs": inputs,
        "baseline": spec.baseline,
        "windows": list(spec.windows),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return {
        **payload,
        "payload_hash": "sha256:" + hashlib.sha256(encoded.encode()).hexdigest(),
    }


def _require_calculation(feature_id: str) -> CalculationSpec:
    try:
        return CALCULATION_REGISTRY[feature_id]
    except KeyError as exc:
        raise ValueError(f"unregistered_calculation:{feature_id}") from exc


def _curve_rows(
    series_rows: list[dict[str, Any]],
    dataset_id: str,
) -> dict[date, dict[str, float]]:
    rows: dict[date, dict[str, float]] = defaultdict(dict)
    for row in series_rows:
        if row["dataset_id"] != dataset_id or row.get("value_numeric") is None:
            continue
        rows[row["reference_date"]][str(row["series_id"])] = float(row["value_numeric"])
    return dict(rows)


def _curve_snapshots(
    rows: dict[date, dict[str, float]],
) -> list[dict[str, Any]]:
    if not rows:
        return []
    latest = max(rows)
    snapshots = []
    for window, days in (("current", 0), ("1w", 7), ("1m", 30), ("3m", 90)):
        target = latest - timedelta(days=days)
        candidates = [reference_date for reference_date in rows if reference_date <= target]
        if not candidates:
            continue
        selected = max(candidates)
        snapshots.append(
            {
                "window": window,
                "as_of": selected.isoformat(),
                "points": [
                    {
                        "tenor": tenor,
                        "years": _TREASURY_TENOR_YEARS[tenor],
                        "yield_pct": value,
                    }
                    for tenor, value in sorted(
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
    nominal: dict[date, dict[str, float]],
    real: dict[date, dict[str, float]],
) -> list[dict[str, Any]]:
    common_dates = nominal.keys() & real.keys()
    if not common_dates:
        return []
    latest = max(common_dates)
    snapshots = []
    for window, days in (("current", 0), ("1w", 7), ("1m", 30), ("3m", 90)):
        target = latest - timedelta(days=days)
        candidates = [reference_date for reference_date in common_dates if reference_date <= target]
        if not candidates:
            continue
        selected = max(candidates)
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
                            nominal[selected][tenor] - real[selected][tenor],
                            4,
                        ),
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
    rows: dict[date, dict[str, float]],
    short_tenor: str,
    long_tenor: str,
) -> list[dict[str, Any]]:
    return [
        {
            "date": reference_date.isoformat(),
            "value_bp": round(
                (values[long_tenor] - values[short_tenor]) * 100,
                4,
            ),
        }
        for reference_date, values in sorted(rows.items())
        if short_tenor in values and long_tenor in values
    ][-2_500:]


def _curve_classification(
    snapshots: list[dict[str, Any]],
    spreads: dict[str, list[dict[str, Any]]],
    *,
    formula_version: str,
) -> dict[str, Any]:
    by_window = {snapshot["window"]: snapshot for snapshot in snapshots}
    current = by_window.get("current")
    prior = by_window.get("1w")
    if current is None or prior is None:
        return {
            "state": "insufficient_history",
            "label": "历史不足",
            "formula_version": formula_version,
            "inputs": {},
        }
    current_points = {point["tenor"]: float(point["yield_pct"]) for point in current["points"]}
    prior_points = {point["tenor"]: float(point["yield_pct"]) for point in prior["points"]}
    required_tenors = {"2Y", "5Y", "10Y"}
    if not required_tenors <= current_points.keys() or not required_tenors <= prior_points.keys():
        return {
            "state": "insufficient_tenors",
            "label": "期限不足",
            "formula_version": formula_version,
            "inputs": {},
        }

    current_level = sum(current_points[tenor] for tenor in required_tenors) / 3
    prior_level = sum(prior_points[tenor] for tenor in required_tenors) / 3
    current_slope = current_points["10Y"] - current_points["2Y"]
    prior_slope = prior_points["10Y"] - prior_points["2Y"]
    current_curvature = 2 * current_points["5Y"] - current_points["2Y"] - current_points["10Y"]
    prior_curvature = 2 * prior_points["5Y"] - prior_points["2Y"] - prior_points["10Y"]
    short_change = (current_points["2Y"] - prior_points["2Y"]) * 100
    long_change = (current_points["10Y"] - prior_points["10Y"]) * 100
    level_change = (current_level - prior_level) * 100
    slope_change = (current_slope - prior_slope) * 100
    curvature_change = (current_curvature - prior_curvature) * 100
    threshold = 5.0
    if abs(slope_change) < threshold and level_change >= threshold:
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
        "state": state,
        "label": label,
        "formula_version": formula_version,
        "inputs": {
            "current_as_of": current["as_of"],
            "prior_as_of": prior["as_of"],
            "2y_change_bp": round(short_change, 4),
            "10y_change_bp": round(long_change, 4),
            "level_change_bp": round(level_change, 4),
            "slope_change_bp": round(slope_change, 4),
            "curvature_change_bp": round(curvature_change, 4),
            "current_2s10s_bp": (spreads["2s10s"][-1]["value_bp"] if spreads["2s10s"] else None),
        },
    }


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


def _numeric(row: dict[str, Any]) -> float | None:
    value = row.get("value_numeric")
    return float(value) if value is not None else None


def _input_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": row["dataset_id"],
        "fact_id": row["fact_id"],
        "reference_date": str(row["reference_date"]),
        "vintage_date": str(row["vintage_date"]),
        "value_numeric": row["value_numeric"],
        "unit": row["unit"],
    }


__all__ = [
    "CALCULATION_REGISTRY",
    "NATURAL_CHANGE_REGISTRY",
    "CalculationSpec",
    "NaturalChangeCalculationSpec",
    "calculate_curve_contract",
    "calculate_features",
    "calculate_funding_cost_comparisons",
    "calculate_market_comparison",
    "calculate_market_statistics",
    "calculate_series_statistics",
    "natural_change_calculation",
]
