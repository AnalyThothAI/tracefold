"""Pure Production V3 capital-authority facts and fixed UTC risk arithmetic."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tracefold.trading.capital_authority import (
    CapitalAuthorizationReceiptV1,
    CapitalRiskReservationV1,
    DailyRiskPolicyV1,
    OperatorArmReceiptV1,
    ProductionPromotionGrantV1,
    SettlementRiskLimitV1,
    planned_risk_components,
    risk_day_bounds,
)

NOW = 1_900_000_000_000
DAY_START, DAY_END = risk_day_bounds(NOW)


def risk_policy() -> DailyRiskPolicyV1:
    return DailyRiskPolicyV1(
        approved_release="release-sha",
        cost_model_sha256="1" * 64,
        max_committed_entry_attempts=2,
        max_target_notional=Decimal("10"),
        settlement_limits=(
            SettlementRiskLimitV1(
                settlement_asset="USDC",
                max_planned_risk_amount=Decimal("0.50"),
                max_realized_loss_amount=Decimal("1.00"),
                fee_slippage_reserve_bps=50,
            ),
            SettlementRiskLimitV1(
                settlement_asset="USDT",
                max_planned_risk_amount=Decimal("0.25"),
                max_realized_loss_amount=Decimal("0.50"),
                fee_slippage_reserve_bps=50,
            ),
        ),
        issuer="risk-owner",
        issued_at_ms=NOW - 10_000,
        effective_from_ms=NOW - 5_000,
        expires_at_ms=NOW + 86_400_000,
    )


def grant(policy: DailyRiskPolicyV1) -> ProductionPromotionGrantV1:
    return ProductionPromotionGrantV1(
        binding="BINANCE_USDM",
        venue="binance.usdm",
        source_contract_sha256="2" * 64,
        feature_contract_sha256="3" * 64,
        policy_id="source_native_oi_smart_money_long_v3",
        policy_config_sha256="4" * 64,
        cost_model_sha256=policy.cost_model_sha256,
        catalog_snapshot_sha256="5" * 64,
        capability_snapshot_sha256="6" * 64,
        execution_binding_sha256="7" * 64,
        adapter_contract_sha256="8" * 64,
        execution_policy_sha256="9" * 64,
        quote_contract_sha256="a" * 64,
        protection_contract_sha256="b" * 64,
        sealed_corpus_sha256="c" * 64,
        locked_future_report_sha256="d" * 64,
        risk_policy_sha256=policy.risk_policy_sha256,
        approved_release="release-sha",
        allowed_capability_entry_ids=("e" * 64,),
        max_target_notional=Decimal("10"),
        approver="human-reviewer",
        issued_at_ms=NOW - 4_000,
        review_at_ms=NOW + 1_000,
        expires_at_ms=NOW + 3_600_000,
    )


def arm(promotion: ProductionPromotionGrantV1, policy: DailyRiskPolicyV1) -> OperatorArmReceiptV1:
    return OperatorArmReceiptV1(
        arm_epoch=1,
        binding="BINANCE_USDM",
        venue="binance.usdm",
        approved_release="release-sha",
        account_generation=1,
        credential_fingerprint="f" * 64,
        catalog_snapshot_sha256=promotion.catalog_snapshot_sha256,
        capability_snapshot_sha256=promotion.capability_snapshot_sha256,
        execution_binding_sha256=promotion.execution_binding_sha256,
        grant_sha256=promotion.grant_sha256,
        risk_policy_sha256=policy.risk_policy_sha256,
        reconciliation_receipt_sha256="0" * 64,
        reconciled_at_ms=NOW - 1_000,
        operator="operator",
        armed_at_ms=NOW,
        expires_at_ms=NOW + 60_000,
    )


def test_identities_are_stable_and_every_approval_layer_is_explicit() -> None:
    policy = risk_policy()
    promotion = grant(policy)
    armed = arm(promotion, policy)

    assert policy == risk_policy()
    assert policy.risk_policy_sha256 == risk_policy().risk_policy_sha256
    assert promotion.grant_sha256 == grant(policy).grant_sha256
    assert armed.arm_receipt_sha256 == arm(promotion, policy).arm_receipt_sha256
    assert len({policy.risk_policy_sha256, promotion.grant_sha256, armed.arm_receipt_sha256}) == 3


def test_settlement_assets_have_independent_limits_and_missing_is_fail_closed() -> None:
    policy = risk_policy()

    assert policy.limit_for("USDT") is not None
    assert policy.limit_for("USDC") is not None
    assert policy.limit_for("USD") is None
    assert policy.limit_for("USDT") != policy.limit_for("USDC")


def test_policy_rejects_noncanonical_or_duplicated_settlement_limits() -> None:
    usdt = risk_policy().limit_for("USDT")
    assert usdt is not None
    with pytest.raises(ValidationError, match="daily_risk_policy_assets_not_canonical"):
        risk_policy().model_copy(update={"settlement_limits": (usdt, usdt)}).__class__.model_validate(
            risk_policy().model_dump() | {"settlement_limits": (usdt, usdt)}
        )


def test_planned_risk_is_stop_plus_cost_reserve_and_never_uses_fx() -> None:
    stop, costs, total = planned_risk_components(
        target_notional=Decimal("10"),
        stop_loss_bps=200,
        fee_slippage_reserve_bps=50,
    )

    assert (stop, costs, total) == (Decimal("0.2"), Decimal("0.05"), Decimal("0.25"))


def test_reservation_and_authorization_freeze_the_fixed_utc_day_and_counters() -> None:
    policy = risk_policy()
    promotion = grant(policy)
    armed = arm(promotion, policy)
    stop, costs, total = planned_risk_components(
        target_notional=Decimal("10"),
        stop_loss_bps=200,
        fee_slippage_reserve_bps=50,
    )
    reservation = CapitalRiskReservationV1(
        case_id="case-1",
        source_identity="oi:event:oi_signal_v1",
        economic_lifecycle_id="1" * 64,
        binding="BINANCE_USDM",
        settlement_asset="USDT",
        risk_policy_sha256=policy.risk_policy_sha256,
        grant_sha256=promotion.grant_sha256,
        arm_receipt_sha256=armed.arm_receipt_sha256,
        risk_day_start_ms=DAY_START,
        risk_day_end_ms=DAY_END,
        target_notional=Decimal("10"),
        planned_stop_risk_amount=stop,
        fee_slippage_reserve_amount=costs,
        planned_risk_amount=total,
        created_at_ms=NOW,
    )
    receipt = CapitalAuthorizationReceiptV1(
        case_id="case-1",
        reservation_sha256=reservation.reservation_sha256,
        binding="BINANCE_USDM",
        account_generation=1,
        execution_binding_sha256=promotion.execution_binding_sha256,
        grant_sha256=promotion.grant_sha256,
        arm_receipt_sha256=armed.arm_receipt_sha256,
        risk_policy_sha256=policy.risk_policy_sha256,
        risk_day_start_ms=DAY_START,
        risk_day_end_ms=DAY_END,
        settlement_asset="USDT",
        committed_attempts_before=0,
        committed_attempts_limit=2,
        open_planned_risk_before=Decimal("0"),
        open_planned_risk_after=total,
        planned_risk_limit=Decimal("0.25"),
        realized_loss_to_date=Decimal("0"),
        realized_loss_limit=Decimal("0.50"),
        approved_release="release-sha",
        evaluated_at_ms=NOW,
    )

    assert reservation.risk_day_end_ms - reservation.risk_day_start_ms == 86_400_000
    assert len(reservation.reservation_sha256) == 64
    assert len(receipt.authorization_receipt_sha256) == 64


def test_authorization_refuses_exhausted_attempt_planned_or_realized_limits() -> None:
    base = {
        "case_id": "case-1",
        "reservation_sha256": "1" * 64,
        "binding": "BINANCE_USDM",
        "account_generation": 1,
        "execution_binding_sha256": "2" * 64,
        "grant_sha256": "3" * 64,
        "arm_receipt_sha256": "4" * 64,
        "risk_policy_sha256": "5" * 64,
        "risk_day_start_ms": DAY_START,
        "risk_day_end_ms": DAY_END,
        "settlement_asset": "USDT",
        "committed_attempts_before": 0,
        "committed_attempts_limit": 1,
        "open_planned_risk_before": Decimal("0"),
        "open_planned_risk_after": Decimal("0.25"),
        "planned_risk_limit": Decimal("0.25"),
        "realized_loss_to_date": Decimal("0"),
        "realized_loss_limit": Decimal("0.50"),
        "approved_release": "release-sha",
        "evaluated_at_ms": NOW,
    }
    for update, error in (
        ({"committed_attempts_before": 1}, "attempt_limit_exhausted"),
        ({"open_planned_risk_after": Decimal("0.26")}, "planned_risk_exhausted"),
        ({"realized_loss_to_date": Decimal("0.50")}, "realized_loss_exhausted"),
    ):
        with pytest.raises(ValidationError, match=error):
            CapitalAuthorizationReceiptV1(**(base | update))
