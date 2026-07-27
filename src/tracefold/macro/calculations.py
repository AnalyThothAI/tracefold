from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Literal

from tracefold.macro.domain import MacroModuleId

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
)

CALCULATION_REGISTRY = MappingProxyType({spec.feature_id: spec for spec in _CALCULATIONS})


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
        computed = _calculate(spec, by_dataset)
        if computed is not None:
            features.append(computed)
    return features


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


__all__ = ["CALCULATION_REGISTRY", "CalculationSpec", "calculate_features"]
