"""Pure manual-trading contracts shared by Telegram, HTTP, and venue adapters.

No News value, provider payload, credential, database handle, or clock enters this module.  The App
composition root freezes those facts before calling this public Trading interface.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import canonical_sha256

ManualVenue = Literal["binance_usdm_demo"]
_MONEY = Decimal("0.01")
_PRICE = Decimal("0.01")
_BPS = Decimal(10_000)


class TradeSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class StrategyPreset(StrEnum):
    """Stable product identities; display labels belong to presentation adapters."""

    TIGHT_STOP = "tight_stop"
    WIDE_STOP = "wide_stop"


class ModificationGuardState(StrEnum):
    ACCEPTED = "accepted"
    HIGH_RISK_CONFIRMATION = "high_risk_confirmation"
    REJECTED = "rejected"


class ManualSessionState(StrEnum):
    AWAITING_STRATEGY = "AWAITING_STRATEGY"
    PREVIEW = "PREVIEW"
    MODIFYING = "MODIFYING"
    HIGH_RISK_CONFIRMATION = "HIGH_RISK_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    SUBMITTING = "SUBMITTING"
    OPEN = "OPEN"
    AMBIGUOUS = "AMBIGUOUS"
    EXPOSED = "EXPOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class _FrozenManualModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManualTradeParameters(_FrozenManualModel):
    notional_usd: Decimal
    leverage: int = Field(ge=1, le=125)
    stop_loss_bps: int = Field(gt=0, lt=10_000)
    take_profit_bps: int = Field(gt=0, le=100_000)

    @field_validator("notional_usd", mode="before")
    @classmethod
    def parse_positive_notional(cls, value: object) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError("manual_trade_notional_invalid") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("manual_trade_notional_invalid")
        return parsed


class ManualRiskConfig(_FrozenManualModel):
    notional_deviation_limit_bps: int = Field(ge=0, le=50_000)
    tight_stop_deviation_limit_bps: int = Field(ge=0, le=50_000)
    wide_stop_deviation_limit_bps: int = Field(ge=0, le=50_000)
    max_account_risk_bps: int = Field(gt=0, le=10_000)
    high_risk_loss_multiple_bps: int = Field(ge=10_000, le=100_000)
    min_leverage: int = Field(ge=1, le=125)
    max_leverage: int = Field(ge=1, le=125)

    @model_validator(mode="after")
    def validate_leverage_range(self) -> ManualRiskConfig:
        if self.max_leverage < self.min_leverage:
            raise ValueError("manual_trade_leverage_range_invalid")
        return self


class ManualStrategyPresetConfig(_FrozenManualModel):
    preset: StrategyPreset
    leverage: int = Field(ge=1, le=125)
    stop_loss_bps: int = Field(gt=0, lt=10_000)
    take_profit_bps: int = Field(gt=0, le=100_000)
    account_risk_bps: int = Field(gt=0, le=10_000)
    min_notional_usd: Decimal
    max_notional_usd: Decimal

    @field_validator("min_notional_usd", "max_notional_usd", mode="before")
    @classmethod
    def parse_notional_bound(cls, value: object) -> Decimal:
        return _positive_decimal(value, "manual_strategy_notional_bound_invalid")

    @model_validator(mode="after")
    def validate_notional_range(self) -> ManualStrategyPresetConfig:
        if self.max_notional_usd < self.min_notional_usd:
            raise ValueError("manual_strategy_notional_range_invalid")
        return self


class ManualTradeRecommendation(_FrozenManualModel):
    preset: StrategyPreset
    parameters: ManualTradeParameters
    estimated_max_loss_usd: Decimal
    account_risk_bps: int
    recommendation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManualTradeSource(_FrozenManualModel):
    """The frozen App-side projection that binds one manual trade to one delivered News event."""

    news_event_id: str = Field(min_length=1, max_length=128)
    delivery_target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    delivery_message_id: int = Field(gt=0)
    headline_zh: str = Field(min_length=1, max_length=240)
    base_symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
    side: TradeSide
    source_observed_at_ms: int = Field(gt=0)


class ManualAccountSnapshot(_FrozenManualModel):
    account_ref: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    venue: ManualVenue
    instrument_id: str = Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{1,63}$")
    account_equity_usd: Decimal
    reference_entry: Decimal
    observed_at_ms: int = Field(gt=0)
    liquidation_distance_bps: int | None = Field(default=None, gt=0)

    @field_validator("account_equity_usd", "reference_entry", mode="before")
    @classmethod
    def parse_positive_decimal(cls, value: object) -> Decimal:
        return _positive_decimal(value, "manual_account_snapshot_decimal_invalid")


class ManualTradeSession(_FrozenManualModel):
    session_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: ManualTradeSource
    actor_user_id: int = Field(gt=0)
    chat_id: int
    source_message_id: int = Field(gt=0)
    interaction_message_id: int | None = Field(default=None, gt=0)
    interaction_reply_attempted_at_ms: int | None = Field(default=None, gt=0)
    last_effect_update_id: int | None = Field(default=None, ge=0)
    last_effect_result_code: str | None = Field(default=None, min_length=1, max_length=80)
    state: ManualSessionState
    preset: StrategyPreset | None = None
    account_snapshot: ManualAccountSnapshot | None = None
    recommended: ManualTradeParameters | None = None
    selected: ManualTradeParameters | None = None
    preview: ManualTradePreview | None = None
    guard: ManualModificationGuard | None = None
    intent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    version: int = Field(gt=0)
    created_at_ms: int = Field(gt=0)
    updated_at_ms: int = Field(gt=0)


class ManualTradeIntent(_FrozenManualModel):
    intent_version: Literal["manual_trade_intent_v1"] = "manual_trade_intent_v1"
    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    source: ManualTradeSource
    actor_user_id: int = Field(gt=0)
    account_ref: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    venue: ManualVenue
    preset: StrategyPreset
    recommended: ManualTradeParameters
    selected: ManualTradeParameters
    reference_entry: Decimal
    account_equity_usd: Decimal
    guard: ManualModificationGuard
    confirmed_at_ms: int = Field(gt=0)
    high_risk_confirmed_at_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_confirmation_and_identity(self) -> ManualTradeIntent:
        if self.guard.state is ModificationGuardState.REJECTED:
            raise ValueError("manual_trade_rejected_intent_forbidden")
        if self.guard.state is ModificationGuardState.HIGH_RISK_CONFIRMATION:
            if self.high_risk_confirmed_at_ms is None or self.high_risk_confirmed_at_ms > self.confirmed_at_ms:
                raise ValueError("manual_trade_high_risk_confirmation_missing")
        elif self.high_risk_confirmed_at_ms is not None:
            raise ValueError("manual_trade_high_risk_confirmation_unexpected")
        if self.intent_id != canonical_sha256(self.immutable_payload):
            raise ValueError("manual_trade_intent_identity_invalid")
        return self

    @property
    def immutable_payload(self) -> dict[str, Any]:
        return {
            "intent_version": self.intent_version,
            "session_id": self.session_id,
            "source": self.source.model_dump(mode="json"),
            "actor_user_id": self.actor_user_id,
            "account_ref": self.account_ref,
            "venue": self.venue,
            "preset": self.preset,
            "recommended": self.recommended.model_dump(mode="json"),
            "selected": self.selected.model_dump(mode="json"),
            "reference_entry": str(self.reference_entry),
            "account_equity_usd": str(self.account_equity_usd),
            "guard": self.guard.model_dump(mode="json"),
            "confirmed_at_ms": self.confirmed_at_ms,
            "high_risk_confirmed_at_ms": self.high_risk_confirmed_at_ms,
        }


class ManualTradePreview(_FrozenManualModel):
    side: TradeSide
    venue: ManualVenue
    account_equity_usd: Decimal
    reference_entry: Decimal
    order_type: Literal["market"] = "market"
    parameters: ManualTradeParameters
    margin_usd: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    estimated_loss_usd: Decimal
    estimated_profit_usd: Decimal
    account_risk_bps: int
    potential_account_return_bps: int
    liquidation_distance_bps: int | None = None


class ManualModificationGuard(_FrozenManualModel):
    state: ModificationGuardState
    notional_deviation_bps: int
    stop_loss_deviation_bps: int
    take_profit_deviation_bps: int
    original_max_loss_usd: Decimal
    modified_max_loss_usd: Decimal
    max_loss_change_bps: int
    modified_account_risk_bps: int
    reason_codes: tuple[str, ...] = ()


def build_manual_trade_preview(
    *,
    side: TradeSide,
    venue: ManualVenue,
    account_equity: Decimal,
    reference_entry: Decimal,
    parameters: ManualTradeParameters,
    liquidation_distance_bps: int | None = None,
) -> ManualTradePreview:
    """Calculate the complete operator preview from venue-observed, secret-free facts."""

    equity = _positive_decimal(account_equity, "manual_trade_account_equity_invalid")
    entry = _positive_decimal(reference_entry, "manual_trade_reference_entry_invalid")
    margin = _money(parameters.notional_usd / Decimal(parameters.leverage))
    loss = _money(parameters.notional_usd * Decimal(parameters.stop_loss_bps) / _BPS)
    profit = _money(parameters.notional_usd * Decimal(parameters.take_profit_bps) / _BPS)
    if side is TradeSide.LONG:
        stop_price = entry * (_BPS - Decimal(parameters.stop_loss_bps)) / _BPS
        take_profit_price = entry * (_BPS + Decimal(parameters.take_profit_bps)) / _BPS
    else:
        stop_price = entry * (_BPS + Decimal(parameters.stop_loss_bps)) / _BPS
        take_profit_price = entry * (_BPS - Decimal(parameters.take_profit_bps)) / _BPS
        if take_profit_price <= 0:
            raise ValueError("manual_trade_take_profit_price_invalid")
    if liquidation_distance_bps is not None and liquidation_distance_bps <= 0:
        raise ValueError("manual_trade_liquidation_distance_invalid")
    return ManualTradePreview(
        side=side,
        venue=venue,
        account_equity_usd=_money(equity),
        reference_entry=_price(entry),
        parameters=parameters,
        margin_usd=margin,
        stop_loss_price=_price(stop_price),
        take_profit_price=_price(take_profit_price),
        estimated_loss_usd=loss,
        estimated_profit_usd=profit,
        account_risk_bps=_ratio_bps(loss, equity),
        potential_account_return_bps=_ratio_bps(profit, equity),
        liquidation_distance_bps=liquidation_distance_bps,
    )


def recommend_manual_trade(
    *,
    account_equity: Decimal,
    config: ManualStrategyPresetConfig,
) -> ManualTradeRecommendation:
    """Size a preset from its account-risk budget, then apply leverage and absolute caps."""

    equity = _positive_decimal(account_equity, "manual_trade_account_equity_invalid")
    risk_sized = equity * Decimal(config.account_risk_bps) / Decimal(config.stop_loss_bps)
    margin_capped = equity * Decimal(config.leverage)
    notional = min(risk_sized, margin_capped, config.max_notional_usd)
    if notional < config.min_notional_usd:
        raise ValueError("manual_trade_recommendation_below_minimum")
    parameters = ManualTradeParameters(
        notional_usd=_money(notional),
        leverage=config.leverage,
        stop_loss_bps=config.stop_loss_bps,
        take_profit_bps=config.take_profit_bps,
    )
    loss = _money(parameters.notional_usd * Decimal(parameters.stop_loss_bps) / _BPS)
    identity = canonical_sha256(
        {
            "version": "manual_trade_recommendation_v1",
            "account_equity_usd": str(_money(equity)),
            "preset_config": config.model_dump(mode="json"),
            "parameters": parameters.model_dump(mode="json"),
            "estimated_max_loss_usd": str(loss),
        }
    )
    return ManualTradeRecommendation(
        preset=config.preset,
        parameters=parameters,
        estimated_max_loss_usd=loss,
        account_risk_bps=_ratio_bps(loss, equity),
        recommendation_sha256=identity,
    )


def create_manual_trade_intent(
    *,
    session_id: str,
    source: ManualTradeSource,
    actor_user_id: int,
    account_ref: str,
    venue: ManualVenue,
    preset: StrategyPreset,
    recommended: ManualTradeParameters,
    selected: ManualTradeParameters,
    reference_entry: Decimal,
    account_equity: Decimal,
    guard: ManualModificationGuard,
    confirmed_at_ms: int,
    high_risk_confirmed_at_ms: int | None = None,
) -> ManualTradeIntent:
    values: dict[str, Any] = {
        "intent_version": "manual_trade_intent_v1",
        "session_id": session_id,
        "source": source,
        "actor_user_id": actor_user_id,
        "account_ref": account_ref,
        "venue": venue,
        "preset": preset,
        "recommended": recommended,
        "selected": selected,
        "reference_entry": _price(_positive_decimal(reference_entry, "manual_trade_reference_entry_invalid")),
        "account_equity_usd": _money(_positive_decimal(account_equity, "manual_trade_account_equity_invalid")),
        "guard": guard,
        "confirmed_at_ms": confirmed_at_ms,
        "high_risk_confirmed_at_ms": high_risk_confirmed_at_ms,
    }
    unvalidated = ManualTradeIntent.model_construct(intent_id="", **values)
    return ManualTradeIntent(intent_id=canonical_sha256(unvalidated.immutable_payload), **values)


def guard_manual_trade_modification(
    *,
    preset: StrategyPreset,
    account_equity: Decimal,
    recommended: ManualTradeParameters,
    modified: ManualTradeParameters,
    config: ManualRiskConfig,
) -> ManualModificationGuard:
    """Judge parameter drift and the combined loss/account effect in one decision."""

    equity = _positive_decimal(account_equity, "manual_trade_account_equity_invalid")
    original_loss = _money(recommended.notional_usd * Decimal(recommended.stop_loss_bps) / _BPS)
    modified_loss = _money(modified.notional_usd * Decimal(modified.stop_loss_bps) / _BPS)
    notional_deviation = _deviation_bps(modified.notional_usd, recommended.notional_usd)
    stop_deviation = _deviation_bps(Decimal(modified.stop_loss_bps), Decimal(recommended.stop_loss_bps))
    take_profit_deviation = _deviation_bps(Decimal(modified.take_profit_bps), Decimal(recommended.take_profit_bps))
    loss_change = _signed_change_bps(modified_loss, original_loss)
    loss_ratio = _ratio_bps(modified_loss, original_loss)
    account_risk = _ratio_bps(modified_loss, equity)
    stop_limit = (
        config.tight_stop_deviation_limit_bps
        if preset is StrategyPreset.TIGHT_STOP
        else config.wide_stop_deviation_limit_bps
    )

    rejected: list[str] = []
    if not config.min_leverage <= modified.leverage <= config.max_leverage:
        rejected.append("leverage_out_of_range")
    if modified.notional_usd / Decimal(modified.leverage) > equity:
        rejected.append("insufficient_margin")

    high_risk: list[str] = []
    if notional_deviation > config.notional_deviation_limit_bps:
        high_risk.append("notional_deviation")
    if stop_deviation > stop_limit:
        high_risk.append("stop_loss_deviation")
    if take_profit_deviation > stop_limit:
        high_risk.append("take_profit_deviation")
    if loss_ratio > config.high_risk_loss_multiple_bps:
        high_risk.append("combined_max_loss")
    if account_risk > config.max_account_risk_bps:
        high_risk.append("account_risk")

    if rejected:
        state = ModificationGuardState.REJECTED
        reasons = tuple(rejected + high_risk)
    elif high_risk:
        state = ModificationGuardState.HIGH_RISK_CONFIRMATION
        reasons = tuple(high_risk)
    else:
        state = ModificationGuardState.ACCEPTED
        reasons = ()
    return ManualModificationGuard(
        state=state,
        notional_deviation_bps=notional_deviation,
        stop_loss_deviation_bps=stop_deviation,
        take_profit_deviation_bps=take_profit_deviation,
        original_max_loss_usd=original_loss,
        modified_max_loss_usd=modified_loss,
        max_loss_change_bps=loss_change,
        modified_account_risk_bps=account_risk,
        reason_codes=reasons,
    )


def _positive_decimal(value: object, code: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ValueError(code) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(code)
    return parsed


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_HALF_UP)


def _price(value: Decimal) -> Decimal:
    return value.quantize(_PRICE, rounding=ROUND_HALF_UP)


def _ratio_bps(numerator: Decimal, denominator: Decimal) -> int:
    return int((numerator / denominator * _BPS).to_integral_value(rounding=ROUND_DOWN))


def _deviation_bps(value: Decimal, baseline: Decimal) -> int:
    return _ratio_bps(abs(value - baseline), baseline)


def _signed_change_bps(value: Decimal, baseline: Decimal) -> int:
    return int(((value - baseline) / baseline * _BPS).to_integral_value(rounding=ROUND_DOWN))


__all__ = [
    "ManualAccountSnapshot",
    "ManualModificationGuard",
    "ManualRiskConfig",
    "ManualSessionState",
    "ManualStrategyPresetConfig",
    "ManualTradeIntent",
    "ManualTradeParameters",
    "ManualTradePreview",
    "ManualTradeRecommendation",
    "ManualTradeSession",
    "ManualTradeSource",
    "ManualVenue",
    "ModificationGuardState",
    "StrategyPreset",
    "TradeSide",
    "build_manual_trade_preview",
    "create_manual_trade_intent",
    "guard_manual_trade_modification",
    "recommend_manual_trade",
]
