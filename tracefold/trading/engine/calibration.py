"""Offline temperature and timeout fitting with an explicit, non-overlapping future window."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .case_view import CaseView
from .evaluation import content_id
from .forecast import Forecast, PolicyConfig, all_policy_decisions, calibrated_probabilities
from .paper import PaperLeg
from .scoreboard import ScoredCase, forecast_score, paired_score, policy_scores

_TEMPERATURES = tuple(Decimal(value) for value in ("0.5", "0.75", "1", "1.25", "1.5", "2", "3"))
_MIN_TRAINING_LEGS = 30
_MIN_TIMEOUT_LEGS = 10


@dataclass(frozen=True, slots=True)
class CalibrationCase:
    view: CaseView
    forecast: Forecast
    legs: tuple[PaperLeg, PaperLeg]

    def scored(self, parameters: dict[str, Any]) -> ScoredCase:
        view = self.view
        config = PolicyConfig(
            view.geometry.stop_bps,
            view.geometry.tp_bps,
            view.half_spread_bps,
            probability_temperature=Decimal(parameters["temperature"]),
            timeout_long_r=Decimal(parameters["timeout_long_r"]),
            timeout_short_r=Decimal(parameters["timeout_short_r"]),
        )
        return ScoredCase(
            view.case_id,
            view.asset_id,
            datetime.fromtimestamp(view.decided_at_ms / 1000, tz=UTC).date().isoformat(),
            view.trigger_kind,
            all_policy_decisions(view.features, self.forecast, config),
            self.legs,
            Forecast(
                calibrated_probabilities(self.forecast.long, config.probability_temperature),
                calibrated_probabilities(self.forecast.short, config.probability_temperature),
            ),
            baseline_long=view.base_rates[0].probabilities,
            baseline_short=view.base_rates[1].probabilities,
            geometry_version=view.geometry.version,
            assessment_status="ok",
        )


def _fit(rows: tuple[CalibrationCase, ...]) -> dict[str, Any]:
    if len(rows) * 2 < _MIN_TRAINING_LEGS:
        raise ValueError("calibration_training_insufficient")
    identity = {"temperature": "1", "timeout_long_r": "0", "timeout_short_r": "0"}
    losses = []
    for temperature in _TEMPERATURES:
        parameters = {**identity, "temperature": str(temperature)}
        score = forecast_score(tuple(row.scored(parameters) for row in rows), min_legs=_MIN_TRAINING_LEGS)
        if score.log_loss is None:
            raise ValueError("calibration_training_insufficient")
        losses.append((score.log_loss, abs(temperature - 1), temperature))
    result = {**identity, "temperature": str(min(losses)[2]), "training_legs": len(rows) * 2}
    counts = {}
    for side in ("long", "short"):
        values = [
            leg.gross_bps / row.view.geometry.stop_bps
            for row in rows
            for leg in row.legs
            if leg.side == side and leg.outcome == "timeout" and leg.gross_bps is not None
        ]
        counts[side] = len(values)
        result[f"timeout_{side}_r"] = (
            str(sum(values, Decimal(0)) / len(values)) if len(values) >= _MIN_TIMEOUT_LEGS else "0"
        )
    result["timeout_counts"] = counts
    result["timeout_assumption"] = "conditional_sample_mean_if_n_ge_10_else_unfitted_zero"
    return result


def fit_and_validate(
    rows: tuple[CalibrationCase, ...],
    *,
    evaluator_id: str,
    source_run_id: str,
    train_since_ms: int,
    train_until_ms: int,
    validate_until_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not 0 <= train_since_ms < train_until_ms < validate_until_ms:
        raise ValueError("calibration_windows_invalid")
    # A label must have existed at the training boundary. Early Cases with later
    # exits cannot leak future outcomes into temperature or timeout estimates.
    mature = tuple(
        row
        for row in rows
        if all(leg.status == "complete" and leg.exit_at_ms is not None and leg.net_r is not None for leg in row.legs)
    )
    training = tuple(
        row
        for row in mature
        if train_since_ms <= row.view.decided_at_ms < train_until_ms
        and all(leg.exit_at_ms is not None and leg.exit_at_ms <= train_until_ms for leg in row.legs)
    )
    heldout = tuple(
        row
        for row in mature
        if train_until_ms <= row.view.decided_at_ms < validate_until_ms
        and all(leg.exit_at_ms is not None and leg.exit_at_ms <= validate_until_ms for leg in row.legs)
    )
    groups = {"all": _fit(training)}
    for kind in ("oi", "catalyst"):
        subset = tuple(row for row in training if row.view.trigger_kind == kind)
        if len(subset) * 2 >= _MIN_TRAINING_LEGS:
            groups[kind] = _fit(subset)
    manifest = {
        "source_run_id": source_run_id,
        "evaluator_id": evaluator_id,
        "train_since_ms": train_since_ms,
        "train_until_ms": train_until_ms,
        "validate_until_ms": validate_until_ms,
        "training_cases": sorted(row.view.case_id for row in training),
        "heldout_cases": sorted(row.view.case_id for row in heldout),
        "frozen_views_digest": content_id(
            [(row.view.case_id, asdict(row.view)) for row in sorted(rows, key=lambda r: r.view.case_id)]
        ),
        "temperature_grid": [str(value) for value in _TEMPERATURES],
        "policy_thresholds": {"min_expected_r": "0", "min_direction_gap_r": "0"},
        "paper_geometry_versions": sorted({row.view.geometry.version for row in rows}),
        "cost_contract": "paper_cost_v1",
    }
    artifact = {
        "contract": "calibration_v1",
        "evaluator_id": evaluator_id,
        "trained_until_ms": train_until_ms,
        "training_manifest": manifest,
        "groups": groups,
    }
    identity = {"temperature": "1", "timeout_long_r": "0", "timeout_short_r": "0"}
    candidate = tuple(row.scored(groups.get(row.view.trigger_kind, groups["all"])) for row in heldout)
    baseline = tuple(row.scored(identity) for row in heldout)
    left = next((d for row in candidate for d in row.decisions if d.policy_id == "forecast"), None)
    right = next((d for row in baseline for d in row.decisions if d.policy_id == "forecast"), None)
    # Per-trigger calibration shares one candidate algorithm; compare its per-Case
    # actions under a common reporting label without rewriting persisted actions.
    from dataclasses import replace

    def reporting(rows: tuple[ScoredCase, ...], version: str) -> tuple[ScoredCase, ...]:
        return tuple(replace(row, decisions=tuple(replace(d, version=version) for d in row.decisions)) for row in rows)

    candidate_report = reporting(candidate, "candidate")
    baseline_report = reporting(baseline, "identity")
    report = {
        "contract": "calibration_validation_v1",
        "manifest": manifest,
        "status": "exploratory_not_promoted",
        "training_cases": len(training),
        "heldout_cases": len(heldout),
        "excluded_cases": len(rows) - len(training) - len(heldout),
        "heldout_candidate": asdict(forecast_score(candidate)),
        "heldout_identity": asdict(forecast_score(baseline)),
        "candidate_policies": [asdict(score) for score in policy_scores(candidate_report)],
        "paired_forecast": None
        if left is None or right is None
        else asdict(
            paired_score(
                candidate_report,
                baseline_report,
                left_policy=("forecast", "candidate"),
                right_policy=("forecast", "identity"),
            )
        ),
        "automatic_promotion": False,
    }
    return artifact, report
