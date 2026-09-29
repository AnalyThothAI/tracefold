"""Typed three-way leg forecasts and pure, comparable Trading policies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

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

    def __post_init__(self) -> None:
        if self.stop_bps <= 0 or self.tp_bps <= 0:
            raise ValueError("policy_geometry_invalid")
        if (
            any(not value.is_finite() or value < 0 for value in (self.half_spread_bps, self.taker_fee_bps))
            or not self.min_expected_r.is_finite()
        ):
            raise ValueError("policy_cost_invalid")


def _expected_r(probabilities: LegProbabilities, config: PolicyConfig) -> Decimal:
    calibrated = probabilities.normalized()  # Identity calibrator v1.
    gross_bps = calibrated.p_tp * config.tp_bps - calibrated.p_sl * config.stop_bps
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
        return PolicyDecision(policy_id, POLICY_VERSION, "abstain", "forecast_missing")
    long_r = _expected_r(forecast.long, config)
    short_r = _expected_r(forecast.short, config)
    if max(long_r, short_r) <= config.min_expected_r:
        return PolicyDecision(policy_id, POLICY_VERSION, "abstain", "expected_r_below_threshold", max(long_r, short_r))
    action: Literal["long", "short"] = "long" if long_r >= short_r else "short"
    return PolicyDecision(policy_id, POLICY_VERSION, action, "expected_r", max(long_r, short_r))


def all_policy_decisions(
    case_view: Mapping[str, object], forecast: Forecast | None, config: PolicyConfig
) -> tuple[PolicyDecision, ...]:
    return tuple(decide(case_view, forecast, config, policy_id=policy_id) for policy_id in POLICY_IDS)
