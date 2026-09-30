"""Typed three-way leg forecasts and pure, comparable Trading policies."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Literal

from .evaluation import content_id

Action = Literal["long", "short", "abstain"]
POLICY_VERSION = "policy_v1"
POLICY_IDS = ("forecast", "always_long", "always_short", "abstain", "momentum15m", "fade15m")


@dataclass(frozen=True, slots=True)
class LegProbabilities:
    p_tp: Decimal
    p_sl: Decimal
    p_timeout: Decimal

    def __post_init__(self) -> None:
        values = (self.p_tp, self.p_sl, self.p_timeout)
        if any(not value.is_finite() or value < 0 or value > 1 for value in values):
            raise ValueError("forecast_probability_invalid")
        if abs(sum(values, Decimal(0)) - Decimal(1)) > Decimal("0.001"):
            raise ValueError("forecast_probability_sum_invalid")

    def normalized(self) -> LegProbabilities:
        total = self.p_tp + self.p_sl + self.p_timeout
        return LegProbabilities(self.p_tp / total, self.p_sl / total, self.p_timeout / total)


@dataclass(frozen=True, slots=True)
class Driver:
    ref: str
    leans: Literal["long", "short", "neither"]
    note: str

    def __post_init__(self) -> None:
        if not self.ref or not self.note or len(self.note) > 160:
            raise ValueError("forecast_driver_invalid")


@dataclass(frozen=True, slots=True)
class Forecast:
    long: LegProbabilities
    short: LegProbabilities
    drivers: tuple[Driver, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    policy_id: str
    version: str
    action: Action
    reason: str
    expected_r: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    stop_bps: int
    tp_bps: int
    half_spread_bps: Decimal
    min_expected_r: Decimal = Decimal(0)
    taker_fee_bps: Decimal = Decimal(5)
    min_direction_gap_r: Decimal = Decimal(0)
    timeout_long_r: Decimal = Decimal(0)
    timeout_short_r: Decimal = Decimal(0)
    probability_temperature: Decimal = Decimal(1)
    calibrator_version: str = "identity_v1"
    cost_version: str = "paper_cost_v1"

    def snapshot(self) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}

    @property
    def version(self) -> str:
        parameters = self.snapshot()
        for key in ("stop_bps", "tp_bps", "half_spread_bps"):
            del parameters[key]
        return content_id({"contract": "policy_v2", **parameters})

    def __post_init__(self) -> None:
        if self.stop_bps <= 0 or self.tp_bps <= 0:
            raise ValueError("policy_geometry_invalid")
        if (
            any(not value.is_finite() or value < 0 for value in (self.half_spread_bps, self.taker_fee_bps))
            or any(
                not value.is_finite()
                for value in (
                    self.min_expected_r,
                    self.min_direction_gap_r,
                    self.timeout_long_r,
                    self.timeout_short_r,
                    self.probability_temperature,
                )
            )
            or self.min_direction_gap_r < 0
            or not Decimal("0.1") <= self.probability_temperature <= Decimal("10")
        ):
            raise ValueError("policy_cost_invalid")


def calibrated_probabilities(probabilities: LegProbabilities, temperature: Decimal) -> LegProbabilities:
    normalized = probabilities.normalized()
    if temperature == 1:
        return normalized
    logs = tuple(
        math.log(float(value)) / float(temperature) if value > 0 else -math.inf
        for value in (normalized.p_tp, normalized.p_sl, normalized.p_timeout)
    )
    maximum = max(logs)
    values = tuple(Decimal(str(math.exp(value - maximum))) for value in logs)
    total = sum(values, Decimal(0))
    return LegProbabilities(*(value / total for value in values))


def _expected_r(probabilities: LegProbabilities, config: PolicyConfig, *, timeout_r: Decimal) -> Decimal:
    calibrated = calibrated_probabilities(probabilities, config.probability_temperature)
    gross_bps = (
        calibrated.p_tp * config.tp_bps
        - calibrated.p_sl * config.stop_bps
        + calibrated.p_timeout * timeout_r * config.stop_bps
    )
    return (gross_bps - 2 * config.taker_fee_bps - config.half_spread_bps) / config.stop_bps


def decide(
    case_view: Mapping[str, object],
    forecast: Forecast | None,
    config: PolicyConfig,
    *,
    policy_id: str = "forecast",
) -> PolicyDecision:
    """Every policy maps the same frozen Case into long, short or abstain."""
    if policy_id not in POLICY_IDS:
        raise ValueError("policy_unknown")
    if policy_id == "always_long":
        return PolicyDecision(policy_id, POLICY_VERSION, "long", "baseline")
    if policy_id == "always_short":
        return PolicyDecision(policy_id, POLICY_VERSION, "short", "baseline")
    if policy_id == "abstain":
        return PolicyDecision(policy_id, POLICY_VERSION, "abstain", "baseline")
    if policy_id in ("momentum15m", "fade15m"):
        raw = case_view.get("perp_return_15m_bps")
        try:
            change = Decimal(str(raw)) if raw is not None else None
        except ArithmeticError:
            change = None
        if change is None or not change.is_finite() or change == 0:
            return PolicyDecision(policy_id, POLICY_VERSION, "abstain", "momentum_missing_or_flat")
        direction: Literal["long", "short"] = "long" if change > 0 else "short"
        if policy_id == "fade15m":
            direction = "short" if direction == "long" else "long"
        return PolicyDecision(policy_id, POLICY_VERSION, direction, "perp_return_15m")
    if forecast is None:
        return PolicyDecision(policy_id, config.version, "abstain", "forecast_missing")
    long_r = _expected_r(forecast.long, config, timeout_r=config.timeout_long_r)
    short_r = _expected_r(forecast.short, config, timeout_r=config.timeout_short_r)
    if max(long_r, short_r) <= config.min_expected_r:
        return PolicyDecision(policy_id, config.version, "abstain", "expected_r_below_threshold", max(long_r, short_r))
    if abs(long_r - short_r) <= config.min_direction_gap_r:
        return PolicyDecision(policy_id, config.version, "abstain", "direction_gap", max(long_r, short_r))
    action: Literal["long", "short"] = "long" if long_r > short_r else "short"
    return PolicyDecision(policy_id, config.version, action, "expected_r", max(long_r, short_r))


def all_policy_decisions(
    case_view: Mapping[str, object], forecast: Forecast | None, config: PolicyConfig
) -> tuple[PolicyDecision, ...]:
    return tuple(decide(case_view, forecast, config, policy_id=policy_id) for policy_id in POLICY_IDS)


def forecast_from_record(value: dict[str, Any] | None, drivers: list[dict[str, Any]]) -> Forecast | None:
    if value is None:
        return None

    def probabilities(side: str) -> LegProbabilities:
        return LegProbabilities(*(Decimal(str(value[side][key])) for key in ("p_tp", "p_sl", "p_timeout")))

    return Forecast(
        probabilities("long"),
        probabilities("short"),
        tuple(Driver(item["ref"], item["leans"], item["note"]) for item in drivers),
    )
