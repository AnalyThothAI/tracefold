"""Immutable TradeIntent values handed from Tracefold to the execution authority."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import canonical_sha256

TRADE_INTENT_VERSION: Final[Literal["trade_intent_v1"]] = "trade_intent_v1"
BINANCE_USDM_DEMO: Final[Literal["BINANCE_USDM_DEMO"]] = "BINANCE_USDM_DEMO"
INTENT_POLICY_VERSION = "trade_intent_policy_v1"
INTENT_POLICY_PAYLOAD: Final = {
    "version": INTENT_POLICY_VERSION,
    "execution_environment": BINANCE_USDM_DEMO,
    "instrument_id": "SOLUSDT-PERP.BINANCE",
    "side": "long",
    "target_notional_usd_ceiling": "10",
    "ttl_ms": 60_000,
    "stop_loss_bps": 200,
    "max_holding_ms": 180_000,
    "max_entry_drift_bps": 25,
    "max_spread_bps": 30,
    "max_entries_per_utc_day": 1,
    "quantity_rule": "floor_to_venue_precision(target_notional_usd/fresh_price)",
}
INTENT_POLICY_SHA256 = canonical_sha256(INTENT_POLICY_PAYLOAD)
IntentLeg = Literal["entry", "stop", "close"]
IntentReasonCode = Literal[
    "intent_expired",
    "runtime_not_ready",
    "external_exposure",
    "market_unacceptable",
    "quantity_unexecutable",
    "risk_denied",
    "entry_outcome_unknown",
    "protection_unproven",
    "close_outcome_unknown",
    "operator_intervention",
]
ManualReviewReason = Literal[
    "entry_outcome_unknown",
    "protection_unproven",
    "close_outcome_unknown",
    "operator_intervention",
]
_CURRENCY_RE = re.compile(r"^[A-Z0-9]{1,12}$")
_DECIMAL_STRING_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
RejectedReason = Literal[
    "runtime_not_ready",
    "external_exposure",
    "market_unacceptable",
    "quantity_unexecutable",
    "risk_denied",
]


def deterministic_client_order_id(
    intent_id: str,
    leg: IntentLeg,
    *,
    previous_client_order_id: str | None = None,
) -> str:
    """A Binance-safe stable identity for one economic leg."""

    marker = {"entry": "e", "stop": "s", "close": "c"}[leg]
    if leg == "stop":
        seed = canonical_sha256(
            {
                "intent_id": intent_id,
                "leg": "stop",
                "previous_client_order_id": previous_client_order_id,
            }
        )
        return f"tf-{marker}-{seed[:28]}"
    if previous_client_order_id is not None:
        raise ValueError("previous_client_order_id_only_valid_for_stop")
    return f"tf-{marker}-{intent_id[:28]}"


class TradeIntent(BaseModel):
    """One content-addressed, immutable capital instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_version: Literal["trade_intent_v1"] = TRADE_INTENT_VERSION
    case_id: str
    case_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_environment: Literal["BINANCE_USDM_DEMO"] = BINANCE_USDM_DEMO
    instrument_id: Literal["SOLUSDT-PERP.BINANCE"]
    side: Literal["long"] = "long"
    created_at_ms: int = Field(gt=0)
    valid_until_ms: int = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    target_notional_usd: Decimal = Field(gt=0, le=Decimal("10"))
    stop_loss_bps: Literal[200]
    max_holding_ms: Literal[180_000]
    max_entry_drift_bps: Literal[25]
    max_spread_bps: Literal[30]

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        case_manifest_sha256: str,
        created_at_ms: int,
        reference_price: Decimal,
        target_notional_usd: Decimal,
    ) -> Self:
        values: dict[str, Any] = {
            "intent_version": TRADE_INTENT_VERSION,
            "case_id": case_id,
            "case_manifest_sha256": case_manifest_sha256,
            "intent_policy_sha256": INTENT_POLICY_SHA256,
            "execution_environment": BINANCE_USDM_DEMO,
            "instrument_id": INTENT_POLICY_PAYLOAD["instrument_id"],
            "side": "long",
            "created_at_ms": created_at_ms,
            "valid_until_ms": created_at_ms + 60_000,
            "reference_price": reference_price,
            "target_notional_usd": target_notional_usd,
            "stop_loss_bps": INTENT_POLICY_PAYLOAD["stop_loss_bps"],
            "max_holding_ms": INTENT_POLICY_PAYLOAD["max_holding_ms"],
            "max_entry_drift_bps": INTENT_POLICY_PAYLOAD["max_entry_drift_bps"],
            "max_spread_bps": INTENT_POLICY_PAYLOAD["max_spread_bps"],
        }
        return cls(intent_id=canonical_sha256(values), **values)

    @model_validator(mode="after")
    def validate_identity(self) -> TradeIntent:
        if self.valid_until_ms != self.created_at_ms + 60_000:
            raise ValueError("trade_intent_ttl_invalid")
        if self.intent_policy_sha256 != INTENT_POLICY_SHA256:
            raise ValueError("trade_intent_policy_identity_invalid")
        if self.intent_id != canonical_sha256(self.immutable_payload):
            raise ValueError("trade_intent_identity_invalid")
        return self

    @property
    def immutable_payload(self) -> dict[str, Any]:
        return {
            "intent_version": self.intent_version,
            "case_id": self.case_id,
            "case_manifest_sha256": self.case_manifest_sha256,
            "intent_policy_sha256": self.intent_policy_sha256,
            "execution_environment": self.execution_environment,
            "instrument_id": self.instrument_id,
            "side": self.side,
            "created_at_ms": self.created_at_ms,
            "valid_until_ms": self.valid_until_ms,
            "reference_price": self.reference_price,
            "target_notional_usd": self.target_notional_usd,
            "stop_loss_bps": self.stop_loss_bps,
            "max_holding_ms": self.max_holding_ms,
            "max_entry_drift_bps": self.max_entry_drift_bps,
            "max_spread_bps": self.max_spread_bps,
        }


class IntentOutcome(BaseModel):
    """The current execution projection owned by Nautilus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    engine_identity: str | None = None
    execution_state: Literal["PENDING", "IN_FLIGHT", "OPEN_PROTECTED", "MANUAL_REVIEW", "TERMINAL"]
    execution_phase: Literal["ENTRY", "PROTECTION", "EXIT"] | None = None
    terminal_outcome: Literal["EXPIRED", "REJECTED", "CLOSED_FLAT"] | None = None
    reason_code: IntentReasonCode | None = None
    entry_client_order_id: str | None = None
    entry_fenced_at_ms: int | None = None
    stop_client_order_id: str | None = None
    stop_generation: int | None = None
    stop_submitted_at_ms: int | None = None
    close_client_order_id: str | None = None
    close_submitted_at_ms: int | None = None
    actual_quantity: Decimal | None = None
    protected_quantity: Decimal | None = None
    avg_entry_price: Decimal | None = None
    avg_exit_price: Decimal | None = None
    position_id: str | None = None
    protection_order_id: str | None = None
    stop_price: Decimal | None = None
    opened_at_ms: int | None = None
    protected_at_ms: int | None = None
    closed_at_ms: int | None = None
    flat_verified_at_ms: int | None = None
    realized_pnl_amount: Decimal | None = None
    realized_pnl_currency: str | None = None
    commissions_by_currency: dict[str, str] | None = None
    updated_at_ms: int

    @field_validator("commissions_by_currency")
    @classmethod
    def validate_commissions(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 16:
            raise ValueError("intent_commissions_too_many_currencies")
        for currency, amount in value.items():
            if not _CURRENCY_RE.fullmatch(currency):
                raise ValueError("intent_commission_currency_invalid")
            if len(amount) > 64 or not _DECIMAL_STRING_RE.fullmatch(amount):
                raise ValueError("intent_commission_amount_invalid")
        return value


__all__ = [
    "BINANCE_USDM_DEMO",
    "INTENT_POLICY_PAYLOAD",
    "INTENT_POLICY_SHA256",
    "INTENT_POLICY_VERSION",
    "TRADE_INTENT_VERSION",
    "IntentOutcome",
    "IntentReasonCode",
    "ManualReviewReason",
    "RejectedReason",
    "TradeIntent",
    "deterministic_client_order_id",
]
