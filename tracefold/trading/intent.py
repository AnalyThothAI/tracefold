"""Production V3 immutable TradeIntent and execution outcome values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .bindings import ExecutionVenue, binding_for_source_venue, venue_for_binding
from .blacklist import BlacklistSnapshotV1
from .capabilities import SUPPORTED_SETTLEMENT_ASSETS, ExecutionCapabilitySnapshotV2
from .contracts import VenueBinding, canonical_base_symbol, canonical_sha256, underlying_key
from .execution_policy import (
    ENTRY_TTL_MS,
    EXECUTION_POLICY_SHA256,
    MAX_ENTRY_DRIFT_BPS,
    MAX_HOLDING_MS,
    MAX_SPREAD_BPS,
    PROTECTION_CONTRACT_SHA256,
    STOP_LOSS_BPS,
    TARGET_NOTIONAL_CEILING_USD,
    deterministic_client_order_id,
)
from .quote_authority import QUOTE_CONTRACT_SHA256, ExecutionQuoteAuditV1, QuoteRejectionReason

TRADE_INTENT_VERSION: Final[Literal["trade_intent_v3"]] = "trade_intent_v3"
INTENT_POLICY_VERSION = "trade_intent_policy_v4"
INTENT_POLICY_PAYLOAD: Final = {
    "version": INTENT_POLICY_VERSION,
    "bindings": ["BINANCE_USDM", "HYPERLIQUID_PERP"],
    "source_native": True,
    "side": "long",
    "leverage_ceiling": "1",
    "global_active_lifecycle_ceiling": 1,
    "target_notional_ceiling": str(TARGET_NOTIONAL_CEILING_USD),
    "ttl_ms": ENTRY_TTL_MS,
    "stop_loss_bps": STOP_LOSS_BPS,
    "max_holding_ms": MAX_HOLDING_MS,
    "max_entry_drift_bps": MAX_ENTRY_DRIFT_BPS,
    "max_spread_bps": MAX_SPREAD_BPS,
    "quantity_rule": "floor_to_binding_increment(target_notional/fresh_side_price)",
}
INTENT_POLICY_SHA256 = canonical_sha256(INTENT_POLICY_PAYLOAD)
IntentExecutionState = Literal["PENDING", "IN_FLIGHT", "OPEN_PROTECTED", "MANUAL_REVIEW", "TERMINAL"]
ACTIVE_INTENT_STATES: Final[tuple[IntentExecutionState, ...]] = (
    "PENDING",
    "IN_FLIGHT",
    "OPEN_PROTECTED",
    "MANUAL_REVIEW",
)
IntentReasonCode = (
    Literal[
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
        "settlement_unproven",
        "operator_intervention",
    ]
    | QuoteRejectionReason
)
ManualReviewReason = Literal[
    "entry_outcome_unknown",
    "protection_unproven",
    "close_outcome_unknown",
    "settlement_unproven",
    "operator_intervention",
]
RejectedReason = (
    Literal[
        "runtime_not_ready",
        "external_exposure",
        "blacklisted",
        "capability_mismatch",
        "market_unacceptable",
        "quantity_unexecutable",
        "risk_denied",
    ]
    | QuoteRejectionReason
)
_CURRENCY_RE = re.compile(r"^[A-Z0-9]{1,12}$")
_DECIMAL_STRING_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def economic_lifecycle_id(
    *,
    case_id: str,
    source_identity: str,
    binding: VenueBinding,
    provider_instrument_id: str,
) -> str:
    return canonical_sha256(
        {
            "identity_version": "economic_lifecycle_v1",
            "case_id": case_id,
            "source_identity": source_identity,
            "binding": binding,
            "provider_instrument_id": provider_instrument_id,
        }
    )


def economic_leg_id(lifecycle_id: str, leg: Literal["entry", "protection", "close"]) -> str:
    return canonical_sha256({"identity_version": "economic_leg_v1", "economic_lifecycle_id": lifecycle_id, "leg": leg})


def is_executable_instrument(
    *,
    binding: VenueBinding,
    provider_instrument_id: str,
    instrument_id: str,
    canonical_asset: str,
    capability_entry_id: str,
    snapshot: ExecutionCapabilitySnapshotV2,
) -> bool:
    """Re-prove one exact source-native instrument against the exact V2 partition entry."""

    capability = snapshot.included.get(capability_entry_id)
    return bool(
        snapshot.binding == binding
        and snapshot.venue == venue_for_binding(binding)
        and capability is not None
        and capability.provider_instrument_id == provider_instrument_id
        and capability.instrument_id == instrument_id
        and capability.underlying_key == underlying_key(canonical_asset)
        and capability.execution_eligible
        and capability.protection_eligible
    )


class TradeIntent(BaseModel):
    """One content-addressed, source-native capital instruction.

    Q1 exact quantity is deliberately absent. SubmissionFenceV1 remains the only owner of stepped
    quantity, Q1 evidence, and provider client identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_version: Literal["trade_intent_v3"] = TRADE_INTENT_VERSION
    case_id: str = Field(min_length=1)
    case_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_venue: ExecutionVenue
    source_identity: str = Field(min_length=1, max_length=256)
    canonical_asset: str = Field(min_length=1, max_length=32)
    underlying_key: str = Field(pattern=r"^crypto:[A-Z0-9][A-Z0-9._-]{0,31}$")
    binding: VenueBinding
    account_generation: int = Field(ge=1)
    execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    venue_catalog_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_capability_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_entry_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_instrument_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    settlement_asset: str
    intent_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capital_authorization_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blacklist_revision_at_emission: int = Field(ge=0)
    blacklist_snapshot_sha256_at_emission: str = Field(pattern=r"^[0-9a-f]{64}$")
    blacklist_snapshot_payload_at_emission: BlacklistSnapshotV1
    economic_lifecycle_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_leg_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_leg_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    close_leg_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    side: Literal["long"] = "long"
    leverage: Literal[1] = 1
    created_at_ms: int = Field(gt=0)
    valid_until_ms: int = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    target_notional: Decimal = Field(gt=0, le=TARGET_NOTIONAL_CEILING_USD)
    max_risk_amount: Decimal = Field(gt=0)
    risk_currency: str
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
        source_venue: ExecutionVenue,
        source_identity: str,
        canonical_asset: str,
        binding: VenueBinding,
        account_generation: int,
        execution_binding_sha256: str,
        venue_catalog_snapshot_sha256: str,
        execution_capability_snapshot_sha256: str,
        capability_entry_id: str,
        provider_instrument_id: str,
        instrument_id: str,
        settlement_asset: str,
        capital_authorization_receipt_sha256: str,
        blacklist_snapshot: BlacklistSnapshotV1,
        created_at_ms: int,
        reference_price: Decimal,
        target_notional: Decimal,
        max_risk_amount: Decimal,
        risk_currency: str,
    ) -> Self:
        asset = canonical_base_symbol(canonical_asset)
        lifecycle = economic_lifecycle_id(
            case_id=case_id,
            source_identity=source_identity,
            binding=binding,
            provider_instrument_id=provider_instrument_id,
        )
        values: dict[str, Any] = {
            "intent_version": TRADE_INTENT_VERSION,
            "case_id": case_id,
            "case_manifest_sha256": case_manifest_sha256,
            "source_venue": source_venue,
            "source_identity": source_identity,
            "canonical_asset": asset,
            "underlying_key": underlying_key(asset),
            "binding": binding,
            "account_generation": account_generation,
            "execution_binding_sha256": execution_binding_sha256,
            "venue_catalog_snapshot_sha256": venue_catalog_snapshot_sha256,
            "execution_capability_snapshot_sha256": execution_capability_snapshot_sha256,
            "capability_entry_id": capability_entry_id,
            "provider_instrument_id": provider_instrument_id,
            "instrument_id": instrument_id,
            "settlement_asset": settlement_asset,
            "intent_policy_sha256": INTENT_POLICY_SHA256,
            "execution_policy_sha256": EXECUTION_POLICY_SHA256,
            "quote_contract_sha256": QUOTE_CONTRACT_SHA256,
            "protection_contract_sha256": PROTECTION_CONTRACT_SHA256,
            "capital_authorization_receipt_sha256": capital_authorization_receipt_sha256,
            "blacklist_revision_at_emission": blacklist_snapshot.revision,
            "blacklist_snapshot_sha256_at_emission": blacklist_snapshot.snapshot_sha256,
            "blacklist_snapshot_payload_at_emission": blacklist_snapshot.model_dump(mode="json"),
            "economic_lifecycle_id": lifecycle,
            "entry_leg_id": economic_leg_id(lifecycle, "entry"),
            "protection_leg_id": economic_leg_id(lifecycle, "protection"),
            "close_leg_id": economic_leg_id(lifecycle, "close"),
            "side": "long",
            "leverage": 1,
            "created_at_ms": created_at_ms,
            "valid_until_ms": created_at_ms + ENTRY_TTL_MS,
            "reference_price": reference_price,
            "target_notional": target_notional,
            "max_risk_amount": max_risk_amount,
            "risk_currency": risk_currency,
            "stop_loss_bps": STOP_LOSS_BPS,
            "max_holding_ms": MAX_HOLDING_MS,
            "max_entry_drift_bps": MAX_ENTRY_DRIFT_BPS,
            "max_spread_bps": MAX_SPREAD_BPS,
        }
        return cls(intent_id=canonical_sha256(values), **values)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if binding_for_source_venue(self.source_venue) != self.binding:
            raise ValueError("trade_intent_source_binding_mismatch")
        if self.underlying_key != underlying_key(self.canonical_asset):
            raise ValueError("trade_intent_underlying_mismatch")
        if self.settlement_asset not in SUPPORTED_SETTLEMENT_ASSETS:
            raise ValueError("trade_intent_settlement_asset_unsupported")
        if self.risk_currency != self.settlement_asset:
            raise ValueError("trade_intent_risk_currency_mismatch")
        if self.max_risk_amount > self.target_notional:
            raise ValueError("trade_intent_max_risk_exceeds_notional")
        if self.valid_until_ms != self.created_at_ms + ENTRY_TTL_MS:
            raise ValueError("trade_intent_ttl_invalid")
        if self.blacklist_snapshot_payload_at_emission.revision != self.blacklist_revision_at_emission:
            raise ValueError("trade_intent_blacklist_revision_mismatch")
        if self.blacklist_snapshot_payload_at_emission.snapshot_sha256 != self.blacklist_snapshot_sha256_at_emission:
            raise ValueError("trade_intent_blacklist_snapshot_mismatch")
        expected_lifecycle = economic_lifecycle_id(
            case_id=self.case_id,
            source_identity=self.source_identity,
            binding=self.binding,
            provider_instrument_id=self.provider_instrument_id,
        )
        if self.economic_lifecycle_id != expected_lifecycle:
            raise ValueError("trade_intent_lifecycle_identity_invalid")
        if (
            self.entry_leg_id != economic_leg_id(expected_lifecycle, "entry")
            or self.protection_leg_id != economic_leg_id(expected_lifecycle, "protection")
            or self.close_leg_id != economic_leg_id(expected_lifecycle, "close")
        ):
            raise ValueError("trade_intent_leg_identity_invalid")
        if self.intent_id != canonical_sha256(self.immutable_payload):
            raise ValueError("trade_intent_identity_invalid")
        return self

    @property
    def immutable_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="python", exclude={"intent_id"})


class IntentOutcome(BaseModel):
    """The current execution projection owned by the lifecycle coordinator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    engine_identity: str | None = None
    execution_state: IntentExecutionState
    execution_phase: Literal["ENTRY", "PROTECTION", "EXIT"] | None = None
    terminal_outcome: Literal["EXPIRED", "REJECTED", "CLOSED_FLAT"] | None = None
    reason_code: IntentReasonCode | None = None
    adopted_at_ms: int | None = None
    entry_fence_requested_at_ms: int | None = None
    submission_fence_version: Literal["submission_fence_v1"] | None = None
    entry_client_order_id: str | None = None
    entry_fenced_at_ms: int | None = None
    submission_quantity: Decimal | None = None
    entry_quote_q1: ExecutionQuoteAuditV1 | None = None
    entry_quote_q2: ExecutionQuoteAuditV1 | None = None
    entry_submitted_at_ms: int | None = None
    entry_accepted_at_ms: int | None = None
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
    # Signed provider cash flows: negative is paid, positive is received.  `None` means the
    # lifecycle cannot prove complete funding accounting; `{}` is authoritative known-zero.
    funding_by_currency: dict[str, str] | None = None
    updated_at_ms: int

    @field_validator("commissions_by_currency", "funding_by_currency")
    @classmethod
    def validate_currency_amounts(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 16:
            raise ValueError("intent_currency_amounts_too_many_currencies")
        for currency, amount in value.items():
            if not _CURRENCY_RE.fullmatch(currency):
                raise ValueError("intent_currency_amount_currency_invalid")
            if len(amount) > 64 or not _DECIMAL_STRING_RE.fullmatch(amount):
                raise ValueError("intent_currency_amount_invalid")
        return value


EntryFenceDisposition = Literal["GRANTED", "REFUSED", "UNAVAILABLE"]
EntryFenceUnavailable = Literal[
    "intent_not_claimable",
    "runtime_not_ready",
    "intent_expired",
]
type EntryFenceReason = EntryFenceUnavailable | Literal["entry_fence_granted"] | IntentReasonCode


@dataclass(frozen=True, slots=True)
class EntryFenceWrite:
    """Primitive transaction result; materialize the domain result after commit."""

    disposition: EntryFenceDisposition
    reason: EntryFenceReason
    outcome_values: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EntryFence:
    """The closed result of attempting to take the durable entry fence."""

    disposition: EntryFenceDisposition
    reason: EntryFenceReason
    outcome: IntentOutcome | None = None

    @property
    def granted(self) -> bool:
        return self.disposition == "GRANTED"


def materialize_entry_fence(value: EntryFenceWrite) -> EntryFence:
    """Build the domain result only after the caller's transaction has committed."""

    outcome = None if value.outcome_values is None else IntentOutcome.model_validate(value.outcome_values)
    return EntryFence(disposition=value.disposition, reason=value.reason, outcome=outcome)


type ActiveIntentValues = tuple[dict[str, Any], dict[str, Any]]


def materialize_active_intent(value: ActiveIntentValues) -> tuple[TradeIntent, IntentOutcome]:
    """Build the active handoff after its locking transaction has committed."""

    intent_values, outcome_values = value
    return TradeIntent.model_validate(intent_values), IntentOutcome.model_validate(outcome_values)


def materialize_intent_outcome(value: dict[str, Any]) -> IntentOutcome:
    """Build one execution projection after the caller's transaction has committed."""

    return IntentOutcome.model_validate(value)


def validate_stop_submission_identity(
    intent_id: str,
    *,
    client_order_id: str,
    generation: int,
    previous_client_order_id: str | None,
) -> None:
    """Validate the deterministic initial or replacement stop identity."""

    expected = deterministic_client_order_id(
        intent_id,
        "stop",
        previous_client_order_id=previous_client_order_id,
    )
    if generation < 0 or (generation == 0) is not (previous_client_order_id is None) or client_order_id != expected:
        code = "initial_stop_identity_invalid" if generation == 0 else "replacement_stop_identity_invalid"
        raise ValueError(code)


def validate_close_submission_identity(intent_id: str, *, client_order_id: str) -> None:
    """Validate the deterministic close identity before entering a write transaction."""

    if client_order_id != deterministic_client_order_id(intent_id, "close"):
        raise ValueError("close_identity_invalid")


__all__ = [
    "ACTIVE_INTENT_STATES",
    "INTENT_POLICY_PAYLOAD",
    "INTENT_POLICY_SHA256",
    "INTENT_POLICY_VERSION",
    "TRADE_INTENT_VERSION",
    "ActiveIntentValues",
    "EntryFence",
    "EntryFenceDisposition",
    "EntryFenceUnavailable",
    "EntryFenceWrite",
    "IntentExecutionState",
    "IntentOutcome",
    "IntentReasonCode",
    "ManualReviewReason",
    "RejectedReason",
    "TradeIntent",
    "deterministic_client_order_id",
    "economic_leg_id",
    "economic_lifecycle_id",
    "is_executable_instrument",
    "materialize_active_intent",
    "materialize_entry_fence",
    "materialize_intent_outcome",
    "validate_close_submission_identity",
    "validate_stop_submission_identity",
]
