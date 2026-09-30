"""Pure paired-policy and forecast scoring over complete LIVE paper legs."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .forecast import POLICY_IDS, POLICY_VERSION, Forecast, LegProbabilities, PolicyDecision
from .paper import PaperLeg

_OUTCOMES = ("tp", "sl", "timeout")


@dataclass(frozen=True, slots=True)
class ScoredCase:
    case_id: str
    asset_id: str
    day: str
    trigger_kind: str
    decisions: tuple[PolicyDecision, ...]
    legs: tuple[PaperLeg, PaperLeg]
    forecast: Forecast | None = None
    baseline_long: LegProbabilities | None = None
    baseline_short: LegProbabilities | None = None
    episode_id: str | None = None
    episode_role: str = "unknown"
    source_age_ms: int | None = None
    ingest_mode: str = "unknown"
    geometry_version: str = "unknown"
    assessment_status: str = "unknown"


@dataclass(frozen=True, slots=True)
class PolicyScore:
    policy_id: str
    policy_version: str
    cases: int
    actions: int
    scored: int
    coverage: Decimal
    average_r: Decimal | None
    win_rate: Decimal | None
    ci_low: Decimal | None
    ci_high: Decimal | None
    status: Literal["ok", "insufficient_data"]
    clusters: int = 0
    effective_days: int = 0


@dataclass(frozen=True, slots=True)
class ForecastScore:
    legs: int
    multiclass_brier: Decimal | None
    log_loss: Decimal | None
    brier_skill_score: Decimal | None
    reliability: tuple[dict[str, object], ...]
    status: Literal["ok", "insufficient_data"]
    matched_baseline_legs: int = 0
    baseline_coverage: Decimal = Decimal(0)


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def _cluster_interval(
    rows: list[tuple[str, Decimal]], *, seed: int, repetitions: int = 1_000
) -> tuple[Decimal, Decimal]:
    groups: dict[str, list[Decimal]] = {}
    for cluster, value in rows:
        groups.setdefault(cluster, []).append(value)
    keys = sorted(groups)
    rng = random.Random(seed)  # noqa: S311 -- deterministic statistical resampling, not security.
    samples: list[Decimal] = []
    for _ in range(repetitions):
        selected = [groups[keys[rng.randrange(len(keys))]] for _ in keys]
        flattened = [value for group in selected for value in group]
        samples.append(_mean(flattened))
    samples.sort()
    return samples[int(0.025 * repetitions)], samples[int(0.975 * repetitions) - 1]


def policy_scores(
    cases: tuple[ScoredCase, ...], *, min_clusters: int = 10, min_days: int = 7
) -> tuple[PolicyScore, ...]:
    """A policy's return joins its action to the same Case's selected paper side."""
    identities = sorted(
        {(action.policy_id, action.version) for case in cases for action in case.decisions}
        or {(policy_id, POLICY_VERSION) for policy_id in POLICY_IDS}
    )
    scores = []
    for policy_id, version in identities:
        actions = 0
        values: list[tuple[str, Decimal]] = []
        for case in cases:
            decision = next(
                (item for item in case.decisions if (item.policy_id, item.version) == (policy_id, version)), None
            )
            if decision is None or decision.action == "abstain":
                continue
            actions += 1
            leg = next((item for item in case.legs if item.side == decision.action), None)
            if leg is not None and leg.status == "complete" and leg.net_r is not None:
                values.append((f"{case.day}|{case.asset_id}", leg.net_r))
        clusters = {cluster for cluster, _ in values}
        days = {cluster.split("|", 1)[0] for cluster in clusters}
        enough = len(clusters) >= min_clusters and len(days) >= min_days
        returns = [value for _, value in values]
        ci = (
            _cluster_interval([(cluster.split("|", 1)[0], value) for cluster, value in values], seed=0)
            if enough
            else (None, None)
        )
        scores.append(
            PolicyScore(
                policy_id,
                version,
                len(cases),
                actions,
                len(values),
                Decimal(actions) / len(cases) if cases else Decimal(0),
                _mean(returns) if returns else None,
                Decimal(sum(value > 0 for value in returns)) / len(returns) if returns else None,
                ci[0],
                ci[1],
                "ok" if enough else "insufficient_data",
                len(clusters),
                len(days),
            )
        )
    return tuple(scores)


def _loss(probabilities: LegProbabilities, outcome: str) -> tuple[Decimal, Decimal, Decimal]:
    predicted = (probabilities.p_tp, probabilities.p_sl, probabilities.p_timeout)
    expected = tuple(Decimal(int(name == outcome)) for name in _OUTCOMES)
    brier = sum(((p - y) ** 2 for p, y in zip(predicted, expected, strict=True)), Decimal(0))
    true_probability = predicted[_OUTCOMES.index(outcome)]
    log_loss = Decimal(str(-math.log(float(max(true_probability, Decimal("1e-12"))))))
    return brier, log_loss, predicted[0]


def forecast_score(cases: tuple[ScoredCase, ...], *, min_legs: int = 30) -> ForecastScore:
    """Use the same Case, side and outcome for forecast and PIT-climatology losses."""
    briers: list[Decimal] = []
    baseline_briers: list[Decimal] = []
    matched_briers: list[Decimal] = []
    logs: list[Decimal] = []
    bins: dict[int, list[bool]] = {index: [] for index in range(10)}
    for case in cases:
        if case.forecast is None:
            continue
        for leg in case.legs:
            if leg.status != "complete" or leg.outcome is None:
                continue
            probabilities = case.forecast.long if leg.side == "long" else case.forecast.short
            baseline = case.baseline_long if leg.side == "long" else case.baseline_short
            brier, log_loss, p_tp = _loss(probabilities, leg.outcome)
            briers.append(brier)
            logs.append(log_loss)
            if baseline is not None:
                baseline_briers.append(_loss(baseline, leg.outcome)[0])
                matched_briers.append(brier)
            bins[min(9, int(p_tp * 10))].append(leg.outcome == "tp")
    if len(briers) < min_legs:
        return ForecastScore(
            len(briers),
            None,
            None,
            None,
            (),
            "insufficient_data",
            len(matched_briers),
            Decimal(len(matched_briers)) / len(briers) if briers else Decimal(0),
        )
    reliability = tuple(
        {"bin": index, "count": len(values), "observed_tp_rate": Decimal(sum(values)) / len(values)}
        for index, values in bins.items()
        if values
    )
    skill = None
    if len(baseline_briers) >= min_legs and _mean(baseline_briers) > 0:
        skill = Decimal(1) - _mean(matched_briers) / _mean(baseline_briers)
    return ForecastScore(
        len(briers),
        _mean(briers),
        _mean(logs),
        skill,
        reliability,
        "ok",
        len(matched_briers),
        Decimal(len(matched_briers)) / len(briers),
    )


@dataclass(frozen=True, slots=True)
class PairedScore:
    left_policy: str
    left_version: str
    right_policy: str
    right_version: str
    common_cases: int
    scored: int
    missing_labels: int
    missing_decisions: int
    average_r_delta: Decimal | None
    ci_low: Decimal | None
    ci_high: Decimal | None
    clusters: int
    effective_days: int
    status: Literal["ok", "insufficient_data"]


def paired_score(
    left: tuple[ScoredCase, ...],
    right: tuple[ScoredCase, ...],
    *,
    left_policy: tuple[str, str],
    right_policy: tuple[str, str],
    min_clusters: int = 10,
    min_days: int = 7,
) -> PairedScore:
    """Compare net R on identical frozen Cases; idle capital is zero only for an explicit abstain."""
    right_by = {case.case_id: case for case in right}
    values: list[tuple[str, str, Decimal]] = []
    common = missing_labels = missing_decisions = 0
    for case in left:
        other = right_by.get(case.case_id)
        if other is None or case.geometry_version != other.geometry_version:
            continue
        common += 1
        first = next((d for d in case.decisions if (d.policy_id, d.version) == left_policy), None)
        second = next((d for d in other.decisions if (d.policy_id, d.version) == right_policy), None)
        # A failed forecast abstention is a missing prediction, not a successful zero-return candidate.
        if (
            first is None
            or second is None
            or (first.reason == "forecast_missing" or second.reason == "forecast_missing")
        ):
            missing_decisions += 1
            continue
        if any(leg.status != "complete" or leg.net_r is None for leg in (*case.legs, *other.legs)):
            missing_labels += 1
            continue

        def value(row: ScoredCase, decision: PolicyDecision) -> Decimal:
            if decision.action == "abstain":
                return Decimal(0)
            result = next(leg.net_r for leg in row.legs if leg.side == decision.action)
            if result is None:
                raise AssertionError("paired_label_missing")
            return result

        values.append((case.day, case.asset_id, value(case, first) - value(other, second)))
    clusters = {(day, asset) for day, asset, _ in values}
    days = {day for day, _, _ in values}
    enough = len(clusters) >= min_clusters and len(days) >= min_days
    interval = _cluster_interval([(day, result) for day, _, result in values], seed=760) if enough else (None, None)
    return PairedScore(
        left_policy=left_policy[0],
        left_version=left_policy[1],
        right_policy=right_policy[0],
        right_version=right_policy[1],
        common_cases=common,
        scored=len(values),
        missing_labels=missing_labels,
        missing_decisions=missing_decisions,
        average_r_delta=_mean([result for _, _, result in values]) if values else None,
        ci_low=interval[0],
        ci_high=interval[1],
        clusters=len(clusters),
        effective_days=len(days),
        status="ok" if enough else "insufficient_data",
    )


def cohort_scores(cases: tuple[ScoredCase, ...]) -> tuple[dict[str, object], ...]:
    """Bounded descriptive cuts keep correlated episodes and source freshness visible."""
    from dataclasses import asdict

    groups: dict[tuple[str, str], list[ScoredCase]] = {}
    for case in cases:
        age = (
            "unknown"
            if case.source_age_ms is None
            else "future_clock"
            if case.source_age_ms < 0
            else "0_5m"
            if case.source_age_ms <= 300_000
            else "5_15m"
            if case.source_age_ms <= 900_000
            else "over_15m"
        )
        for dimension, group in (
            ("trigger", case.trigger_kind),
            ("asset", case.asset_id),
            ("source_age", age),
            (
                "capture_cohort",
                "unknown"
                if case.source_age_ms is None
                else "delayed_over_10m"
                if case.source_age_ms > 600000
                else "current_under_10m",
            ),
            ("episode_role", case.episode_role),
            ("ingest_mode", case.ingest_mode),
            ("geometry", case.geometry_version),
        ):
            groups.setdefault((dimension, group), []).append(case)
    return tuple(
        {
            "dimension": dimension,
            "group": group,
            "cases": len(rows),
            "episodes": len({row.episode_id for row in rows if row.episode_id is not None}),
            "unknown_episodes": sum(row.episode_id is None for row in rows),
            "complete_pairs": sum(all(leg.status == "complete" for leg in row.legs) for row in rows),
            "missing_legs": sum(leg.status != "complete" for row in rows for leg in row.legs),
            "failures": sum(row.assessment_status not in ("ok", "unknown") for row in rows),
            "policies": [asdict(score) for score in policy_scores(tuple(rows))],
            "forecast": asdict(forecast_score(tuple(rows))),
        }
        for (dimension, group), rows in sorted(groups.items())
    )
