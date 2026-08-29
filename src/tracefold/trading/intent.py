"""Immutable TradeIntent values handed from Tracefold to the execution authority."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .blacklist import BlacklistSnapshotV1
from .capabilities import (
    SUPPORTED_QUOTE_CURRENCIES,
    ExecutionCapabilitySnapshotV1,
    capability_instrument_id,
)
from .contracts import LIVE_EXCHANGE_ID, LIVE_VENUE, InstrumentRef, canonical_sha256, underlying_key
from .execution_policy import (
    ENTRY_TTL_MS,
    MAX_ENTRY_DRIFT_BPS,
    MAX_HOLDING_MS,
    MAX_SPREAD_BPS,
    STOP_LOSS_BPS,
    TARGET_NOTIONAL_CEILING_USD,
    deterministic_client_order_id,
)

TRADE_INTENT_VERSION: Final[Literal["trade_intent_v2"]] = "trade_intent_v2"
BINANCE_USDM_DEMO: Final[Literal["BINANCE_USDM_DEMO"]] = "BINANCE_USDM_DEMO"
INTENT_POLICY_VERSION = "trade_intent_policy_v3"
INTENT_POLICY_PAYLOAD: Final = {
    "version": INTENT_POLICY_VERSION,
    "execution_environment": BINANCE_USDM_DEMO,
    "side": "long",
    "target_notional_usd_ceiling": str(TARGET_NOTIONAL_CEILING_USD),
    "ttl_ms": ENTRY_TTL_MS,
    "stop_loss_bps": STOP_LOSS_BPS,
    "max_holding_ms": MAX_HOLDING_MS,
    "max_entry_drift_bps": MAX_ENTRY_DRIFT_BPS,
    "max_spread_bps": MAX_SPREAD_BPS,
    "quantity_rule": "floor_to_venue_precision(target_notional_usd/fresh_price)",
}
INTENT_POLICY_SHA256 = canonical_sha256(INTENT_POLICY_PAYLOAD)
IntentExecutionState = Literal["PENDING", "IN_FLIGHT", "OPEN_PROTECTED", "MANUAL_REVIEW", "TERMINAL"]
ACTIVE_INTENT_STATES: Final[tuple[IntentExecutionState, ...]] = (
    "PENDING",
    "IN_FLIGHT",
    "OPEN_PROTECTED",
    "MANUAL_REVIEW",
)
IntentReasonCode = Literal[
    "intent_expired",
    "runtime_not_ready",
    "external_exposure",
    "blacklisted",
    "capability_mismatch",
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
    "blacklisted",
    "capability_mismatch",
    "market_unacceptable",
    "quantity_unexecutable",
    "risk_denied",
]


def executable_instrument_id(instrument: InstrumentRef) -> str:
    """The Binance Demo instrument identity a frozen Case's contract maps to, or `""`.

    The construction itself has one owner in `capabilities.capability_instrument_id` (#331 §3); what
    this adds is the live-venue predicate, which is the reason a Hyperliquid contract can never
    produce an identity here even if something upstream let one through.
    """

    if (
        instrument.exchange_id != LIVE_EXCHANGE_ID
        or instrument.venue != LIVE_VENUE
        or instrument.instrument_class != "crypto"
        or instrument.quote_asset not in SUPPORTED_QUOTE_CURRENCIES
    ):
        return ""
    return capability_instrument_id(instrument.provider_symbol)


def is_executable_instrument(instrument: InstrumentRef, snapshot: ExecutionCapabilitySnapshotV1) -> bool:
    """Re-prove the frozen contract against the snapshot that is active *now*.

    The lane resolves an instrument from the active snapshot before it freezes a Case, so this is a
    second read of the same question at commit time: the pointer can move while the Case waits, and an
    Intent must never pin a capability the runtime has since replaced.
    """

    instrument_id = executable_instrument_id(instrument)
    capability = snapshot.included.get(instrument_id)
    return bool(
        capability is not None
        and capability.native_symbol == instrument.provider_symbol
        and capability.underlying_key == underlying_key(instrument.base_symbol)
        and capability.quote_currency == instrument.quote_asset
        and capability.executable
    )


class TradeIntent(BaseModel):
    """One content-addressed, immutable capital instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_version: Literal["trade_intent_v1", "trade_intent_v2"] = TRADE_INTENT_VERSION
    case_id: str
    case_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_environment: Literal["BINANCE_USDM_DEMO"] = BINANCE_USDM_DEMO
    execution_capability_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blacklist_revision_at_emission: int | None = Field(default=None, ge=0)
    blacklist_snapshot_sha256_at_emission: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blacklist_snapshot_payload_at_emission: BlacklistSnapshotV1 | None = None
    instrument_id: str
    underlying_key: str | None = None
    side: Literal["long"] = "long"
    created_at_ms: int = Field(gt=0)
    valid_until_ms: int = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    target_notional_usd: Decimal = Field(gt=0, le=TARGET_NOTIONAL_CEILING_USD)
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
        execution_capability_snapshot_sha256: str,
        blacklist_snapshot: BlacklistSnapshotV1,
        instrument_id: str,
        underlying_key: str,
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
            "execution_capability_snapshot_sha256": execution_capability_snapshot_sha256,
            "blacklist_revision_at_emission": blacklist_snapshot.revision,
            "blacklist_snapshot_sha256_at_emission": blacklist_snapshot.snapshot_sha256,
            "blacklist_snapshot_payload_at_emission": blacklist_snapshot.model_dump(mode="json"),
            "instrument_id": instrument_id,
            "underlying_key": underlying_key,
            "side": "long",
            "created_at_ms": created_at_ms,
            "valid_until_ms": created_at_ms + ENTRY_TTL_MS,
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
        if self.valid_until_ms != self.created_at_ms + ENTRY_TTL_MS:
            raise ValueError("trade_intent_ttl_invalid")
        # The stored digest is deliberately *not* compared to the current constant here. This
        # validator runs on every load, so pinning it meant that changing the execution policy made
        # every Intent written under the previous one unreadable — the row would raise instead of
        # saying which policy it was created under. #331 settled the same question for Case states:
        # history stays readable, the writer is what is constrained.
        #
        # `create()` cannot be that constraint either: it assigns `INTENT_POLICY_SHA256` and would be
        # comparing the constant to itself. The real one is the release pin in
        # `tests/contract/test_trading_intent_policy_identity.py`, which fails when the digest moves
        # without someone re-signing for it — the same evidence shape Program identity uses (#348).
        if self.intent_version == "trade_intent_v2":
            if (
                self.execution_capability_snapshot_sha256 is None
                or self.blacklist_revision_at_emission is None
                or self.blacklist_snapshot_sha256_at_emission is None
                or self.blacklist_snapshot_payload_at_emission is None
                or not self.underlying_key
            ):
                raise ValueError("trade_intent_v2_identity_incomplete")
            if self.blacklist_snapshot_payload_at_emission.revision != self.blacklist_revision_at_emission:
                raise ValueError("trade_intent_blacklist_revision_mismatch")
            if (
                self.blacklist_snapshot_payload_at_emission.snapshot_sha256
                != self.blacklist_snapshot_sha256_at_emission
            ):
                raise ValueError("trade_intent_blacklist_snapshot_mismatch")
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
            **(
                {
                    "execution_capability_snapshot_sha256": self.execution_capability_snapshot_sha256,
                    "blacklist_revision_at_emission": self.blacklist_revision_at_emission,
                    "blacklist_snapshot_sha256_at_emission": self.blacklist_snapshot_sha256_at_emission,
                    "blacklist_snapshot_payload_at_emission": (
                        self.blacklist_snapshot_payload_at_emission.model_dump(mode="json")
                        if self.blacklist_snapshot_payload_at_emission is not None
                        else None
                    ),
                    "underlying_key": self.underlying_key,
                }
                if self.intent_version == "trade_intent_v2"
                else {}
            ),
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
    execution_state: IntentExecutionState
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
    "ACTIVE_INTENT_STATES",
    "BINANCE_USDM_DEMO",
    "INTENT_POLICY_PAYLOAD",
    "INTENT_POLICY_SHA256",
    "INTENT_POLICY_VERSION",
    "TRADE_INTENT_VERSION",
    "IntentExecutionState",
    "IntentOutcome",
    "IntentReasonCode",
    "ManualReviewReason",
    "RejectedReason",
    "TradeIntent",
    "deterministic_client_order_id",
    "executable_instrument_id",
    "is_executable_instrument",
]
