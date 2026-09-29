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


@dataclass(frozen=True, slots=True)
class ForecastScore:
    legs: int
    multiclass_brier: Decimal | None
    log_loss: Decimal | None
    brier_skill_score: Decimal | None
    reliability: tuple[dict[str, object], ...]
    status: Literal["ok", "insufficient_data"]


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


def policy_scores(cases: tuple[ScoredCase, ...], *, min_clusters: int = 10) -> tuple[PolicyScore, ...]:
    """A policy's return joins its action to the same Case's selected paper side."""
    identities = sorted(
        {(policy_id, POLICY_VERSION) for policy_id in POLICY_IDS}
        | {(action.policy_id, action.version) for case in cases for action in case.decisions}
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
        enough = len(clusters) >= min_clusters
        returns = [value for _, value in values]
        ci = _cluster_interval(values, seed=0) if enough else (None, None)
        scores.append(
            PolicyScore(
                policy_id,
                version,
                len(cases),
                actions,
                len(values),
                Decimal(actions) / len(cases) if cases else Decimal(0),
                _mean(returns) if enough else None,
                Decimal(sum(value > 0 for value in returns)) / len(returns) if enough else None,
                ci[0],
                ci[1],
                "ok" if enough else "insufficient_data",
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
            bins[min(9, int(p_tp * 10))].append(leg.outcome == "tp")
    if len(briers) < min_legs:
        return ForecastScore(len(briers), None, None, None, (), "insufficient_data")
    reliability = tuple(
        {"bin": index, "count": len(values), "observed_tp_rate": Decimal(sum(values)) / len(values)}
        for index, values in bins.items()
        if values
    )
    skill = None
    if len(baseline_briers) == len(briers) and _mean(baseline_briers) > 0:
        skill = Decimal(1) - _mean(briers) / _mean(baseline_briers)
    return ForecastScore(len(briers), _mean(briers), _mean(logs), skill, reliability, "ok")
