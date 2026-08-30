"""Immutable Production V3 promotion, arm, risk, reservation, and authorization facts.

The models in this module grant no authority by themselves.  PostgreSQL composes them with current
runtime, account, capability, blacklist, and exposure truth in one serial transaction before an
Intent can exist.  Content identity makes the exact human evidence independently verifiable without
putting credentials, provider payloads, or mutable status into an approval artifact.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bindings import ExecutionVenue, venue_for_binding
from .contracts import VenueBinding, canonical_sha256
from .execution_policy import TARGET_NOTIONAL_CEILING_USD

MILLISECONDS_PER_UTC_DAY: Final = 86_400_000
CAPITAL_SCOPE: Final[Literal["canary"]] = "canary"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def risk_day_bounds(at_ms: int) -> tuple[int, int]:
    """Return the fixed UTC risk day containing ``at_ms`` as a half-open interval."""

    if at_ms < 0:
        raise ValueError("capital_risk_clock_invalid")
    start = (int(at_ms) // MILLISECONDS_PER_UTC_DAY) * MILLISECONDS_PER_UTC_DAY
    return start, start + MILLISECONDS_PER_UTC_DAY


class SettlementRiskLimitV1(_Frozen):
    """One settlement asset's independent loss and planned-risk ceilings."""

    settlement_asset: Literal["USDT", "USDC"]
    max_planned_risk_amount: Decimal = Field(gt=0)
    max_realized_loss_amount: Decimal = Field(gt=0)
    fee_slippage_reserve_bps: int = Field(ge=0, le=1_000)


class DailyRiskPolicyV1(_Frozen):
    """Human-approved conservative UTC-day limits; missing assets are not unlimited."""

    risk_policy_version: Literal["daily_risk_policy_v1"] = "daily_risk_policy_v1"
    approved_release: str = Field(min_length=1, max_length=128)
    cost_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_committed_entry_attempts: int = Field(ge=1, le=100)
    max_target_notional: Decimal = Field(gt=0, le=TARGET_NOTIONAL_CEILING_USD)
    leverage_ceiling: Literal[1] = 1
    risk_day: Literal["UTC"] = "UTC"
    settlement_limits: tuple[SettlementRiskLimitV1, ...] = Field(min_length=1, max_length=2)
    issuer: str = Field(min_length=1, max_length=128)
    issued_at_ms: int = Field(gt=0)
    effective_from_ms: int = Field(gt=0)
    expires_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        assets = tuple(limit.settlement_asset for limit in self.settlement_limits)
        if assets != tuple(sorted(assets)) or len(set(assets)) != len(assets):
            raise ValueError("daily_risk_policy_assets_not_canonical")
        if not self.issued_at_ms <= self.effective_from_ms < self.expires_at_ms:
            raise ValueError("daily_risk_policy_clock_invalid")
        return self

    @property
    def risk_policy_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def limit_for(self, settlement_asset: str) -> SettlementRiskLimitV1 | None:
        return next((limit for limit in self.settlement_limits if limit.settlement_asset == settlement_asset), None)


class ProductionPromotionGrantV1(_Frozen):
    """Per-binding human promotion authority tied to one positive future result."""

    grant_version: Literal["production_promotion_grant_v1"] = "production_promotion_grant_v1"
    scope: Literal["canary"] = CAPITAL_SCOPE
    binding: VenueBinding
    venue: ExecutionVenue
    source_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1, max_length=128)
    policy_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cost_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locked_future_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locked_future_result: Literal["PROMOTE"] = "PROMOTE"
    risk_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_release: str = Field(min_length=1, max_length=128)
    allowed_capability_entry_ids: tuple[str, ...] = Field(min_length=1)
    max_target_notional: Decimal = Field(gt=0, le=TARGET_NOTIONAL_CEILING_USD)
    max_leverage: Literal[1] = 1
    approver: str = Field(min_length=1, max_length=128)
    issued_at_ms: int = Field(gt=0)
    review_at_ms: int = Field(gt=0)
    expires_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_grant(self) -> Self:
        if self.venue != venue_for_binding(self.binding):
            raise ValueError("production_grant_binding_venue_mismatch")
        entries = self.allowed_capability_entry_ids
        if entries != tuple(sorted(entries)) or len(set(entries)) != len(entries):
            raise ValueError("production_grant_partition_not_canonical")
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in entries):
            raise ValueError("production_grant_capability_entry_invalid")
        if not self.issued_at_ms <= self.review_at_ms < self.expires_at_ms:
            raise ValueError("production_grant_clock_invalid")
        return self

    @property
    def grant_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ProductionPromotionGrantRevocationV1(_Frozen):
    """Append-only revocation; the grant row itself remains immutable evidence."""

    revocation_version: Literal["production_promotion_grant_revocation_v1"] = "production_promotion_grant_revocation_v1"
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=256)
    revoker: str = Field(min_length=1, max_length=128)
    revoked_at_ms: int = Field(gt=0)

    @property
    def revocation_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class OperatorArmReceiptV1(_Frozen):
    """One binding's exact read-only reconciliation and human re-arm receipt."""

    arm_version: Literal["operator_arm_receipt_v1"] = "operator_arm_receipt_v1"
    arm_epoch: int = Field(ge=1)
    binding: VenueBinding
    venue: ExecutionVenue
    approved_release: str = Field(min_length=1, max_length=128)
    account_generation: int = Field(ge=1)
    credential_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_state: Literal["reconciled_flat"] = "reconciled_flat"
    reconciled_at_ms: int = Field(gt=0)
    operator: str = Field(min_length=1, max_length=128)
    armed_at_ms: int = Field(gt=0)
    expires_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_arm(self) -> Self:
        if self.venue != venue_for_binding(self.binding):
            raise ValueError("operator_arm_binding_venue_mismatch")
        if not self.reconciled_at_ms <= self.armed_at_ms < self.expires_at_ms:
            raise ValueError("operator_arm_clock_invalid")
        return self

    @property
    def arm_receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CapitalRiskReservationV1(_Frozen):
    """Initial planned-risk claim created atomically with one TradeIntentV3."""

    reservation_version: Literal["capital_risk_reservation_v1"] = "capital_risk_reservation_v1"
    case_id: str = Field(min_length=1)
    source_identity: str = Field(min_length=1, max_length=256)
    economic_lifecycle_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding: VenueBinding
    settlement_asset: Literal["USDT", "USDC"]
    risk_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_day_start_ms: int = Field(ge=0)
    risk_day_end_ms: int = Field(gt=0)
    target_notional: Decimal = Field(gt=0, le=TARGET_NOTIONAL_CEILING_USD)
    planned_stop_risk_amount: Decimal = Field(gt=0)
    fee_slippage_reserve_amount: Decimal = Field(ge=0)
    planned_risk_amount: Decimal = Field(gt=0)
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_reservation(self) -> Self:
        if (self.risk_day_start_ms, self.risk_day_end_ms) != risk_day_bounds(self.created_at_ms):
            raise ValueError("capital_reservation_risk_day_invalid")
        if self.planned_risk_amount != self.planned_stop_risk_amount + self.fee_slippage_reserve_amount:
            raise ValueError("capital_reservation_amount_invalid")
        if self.planned_risk_amount > self.target_notional:
            raise ValueError("capital_reservation_exceeds_notional")
        return self

    @property
    def reservation_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CapitalAuthorizationReceiptV1(_Frozen):
    """Opaque Intent authority plus the exact risk counters evaluated under the global lock."""

    authorization_version: Literal["capital_authorization_receipt_v1"] = "capital_authorization_receipt_v1"
    case_id: str = Field(min_length=1)
    reservation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding: VenueBinding
    account_generation: int = Field(ge=1)
    execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    arm_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk_day_start_ms: int = Field(ge=0)
    risk_day_end_ms: int = Field(gt=0)
    settlement_asset: Literal["USDT", "USDC"]
    committed_attempts_before: int = Field(ge=0)
    committed_attempts_limit: int = Field(ge=1)
    open_planned_risk_before: Decimal = Field(ge=0)
    open_planned_risk_after: Decimal = Field(gt=0)
    planned_risk_limit: Decimal = Field(gt=0)
    realized_loss_to_date: Decimal = Field(ge=0)
    realized_loss_limit: Decimal = Field(gt=0)
    approved_release: str = Field(min_length=1, max_length=128)
    evaluated_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_authorization(self) -> Self:
        if (self.risk_day_start_ms, self.risk_day_end_ms) != risk_day_bounds(self.evaluated_at_ms):
            raise ValueError("capital_authorization_risk_day_invalid")
        if self.committed_attempts_before >= self.committed_attempts_limit:
            raise ValueError("capital_authorization_attempt_limit_exhausted")
        if self.open_planned_risk_after > self.planned_risk_limit:
            raise ValueError("capital_authorization_planned_risk_exhausted")
        if self.realized_loss_to_date >= self.realized_loss_limit:
            raise ValueError("capital_authorization_realized_loss_exhausted")
        return self

    @property
    def authorization_receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CapitalAuthoritySnapshot(_Frozen):
    """The one aggregate answer used to authorize or name an exact refusal."""

    disposition: Literal["AUTHORIZED", "BLOCKED"]
    reason: str
    grant_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    arm_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    risk_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reservation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authorization_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    intent_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        identities = (
            self.grant_sha256,
            self.arm_receipt_sha256,
            self.risk_policy_sha256,
            self.reservation_sha256,
            self.authorization_receipt_sha256,
            self.intent_id,
        )
        if self.disposition == "AUTHORIZED" and (
            self.reason != "capital_authorized" or any(v is None for v in identities)
        ):
            raise ValueError("capital_authority_authorized_shape_invalid")
        if self.disposition == "BLOCKED" and any(v is not None for v in identities[3:]):
            raise ValueError("capital_authority_blocked_shape_invalid")
        return self


def planned_risk_components(
    *,
    target_notional: Decimal,
    stop_loss_bps: int,
    fee_slippage_reserve_bps: int,
) -> tuple[Decimal, Decimal, Decimal]:
    """Calculate the conservative stop and cost reserves without currency conversion."""

    if target_notional <= 0 or not target_notional.is_finite():
        raise ValueError("capital_target_notional_invalid")
    if stop_loss_bps <= 0 or fee_slippage_reserve_bps < 0:
        raise ValueError("capital_risk_bps_invalid")
    stop = target_notional * Decimal(stop_loss_bps) / Decimal(10_000)
    costs = target_notional * Decimal(fee_slippage_reserve_bps) / Decimal(10_000)
    return stop, costs, stop + costs


__all__ = [
    "CAPITAL_SCOPE",
    "CapitalAuthoritySnapshot",
    "CapitalAuthorizationReceiptV1",
    "CapitalRiskReservationV1",
    "DailyRiskPolicyV1",
    "OperatorArmReceiptV1",
    "ProductionPromotionGrantRevocationV1",
    "ProductionPromotionGrantV1",
    "SettlementRiskLimitV1",
    "planned_risk_components",
    "risk_day_bounds",
]
