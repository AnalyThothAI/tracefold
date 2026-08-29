"""Production V3 source-native Intent contract."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.trading import INTENT_POLICY_SHA256, BlacklistSnapshotV1, IntentOutcome, TradeIntent

NOW = 1_900_000_000_000


def _intent(**overrides: Any) -> TradeIntent:
    values: dict[str, Any] = {
        "case_id": "case-1",
        "case_manifest_sha256": "1" * 64,
        "source_venue": "binance.usdm",
        "source_identity": "oi:event-1:oi_signal_v1",
        "canonical_asset": "SOL",
        "binding": "BINANCE_USDM",
        "account_generation": 1,
        "execution_binding_sha256": "2" * 64,
        "venue_catalog_snapshot_sha256": "3" * 64,
        "execution_capability_snapshot_sha256": "4" * 64,
        "capability_entry_id": "5" * 64,
        "provider_instrument_id": "SOLUSDT",
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "settlement_asset": "USDT",
        "capital_authorization_receipt_sha256": "6" * 64,
        "blacklist_snapshot": BlacklistSnapshotV1(revision=0, active_rows=()),
        "created_at_ms": NOW,
        "reference_price": Decimal("200"),
        "target_notional": Decimal("10"),
        "max_risk_amount": Decimal("0.25"),
        "risk_currency": "USDT",
    }
    create_overrides = {key: value for key, value in overrides.items() if key in values}
    intent = TradeIntent.create(**(values | create_overrides))
    model_overrides = {key: value for key, value in overrides.items() if key not in values}
    return TradeIntent.model_validate(intent.model_dump() | model_overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_notional", Decimal("10.01")),
        ("max_risk_amount", Decimal("10.01")),
        ("valid_until_ms", NOW + 59_999),
        ("stop_loss_bps", 199),
        ("max_holding_ms", 179_999),
        ("max_entry_drift_bps", 24),
        ("max_spread_bps", 29),
        ("leverage", 2),
    ],
)
def test_v3_rejects_values_outside_the_code_owned_ceiling(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _intent(**{field: value})


def test_v3_freezes_all_execution_and_capital_identities_without_q1_quantity() -> None:
    intent = _intent()

    assert intent.intent_version == "trade_intent_v3"
    assert intent.intent_policy_sha256 == INTENT_POLICY_SHA256
    assert intent.binding == "BINANCE_USDM"
    assert intent.source_venue == "binance.usdm"
    assert intent.account_generation == 1
    assert intent.execution_binding_sha256 == "2" * 64
    assert intent.venue_catalog_snapshot_sha256 == "3" * 64
    assert intent.execution_capability_snapshot_sha256 == "4" * 64
    assert intent.capability_entry_id == "5" * 64
    assert intent.capital_authorization_receipt_sha256 == "6" * 64
    assert intent.valid_until_ms == NOW + 60_000
    assert "submission_quantity" not in type(intent).model_fields


def test_source_venue_can_only_route_to_its_own_binding() -> None:
    with pytest.raises(ValidationError, match="trade_intent_source_binding_mismatch"):
        _intent(binding="HYPERLIQUID_PERP")

    hyperliquid = _intent(
        source_venue="hyperliquid.perp",
        binding="HYPERLIQUID_PERP",
        provider_instrument_id="main:SOL",
        instrument_id="SOL-PERP.HYPERLIQUID",
        settlement_asset="USDC",
        risk_currency="USDC",
    )
    assert hyperliquid.binding == "HYPERLIQUID_PERP"
    assert hyperliquid.source_venue == "hyperliquid.perp"


def test_economic_lifecycle_and_leg_identities_are_deterministic_and_venue_qualified() -> None:
    first = _intent()
    same = _intent()
    hyperliquid = _intent(
        source_venue="hyperliquid.perp",
        binding="HYPERLIQUID_PERP",
        provider_instrument_id="main:SOL",
        instrument_id="SOL-PERP.HYPERLIQUID",
        settlement_asset="USDC",
        risk_currency="USDC",
    )

    assert first.intent_id == same.intent_id
    assert first.economic_lifecycle_id == same.economic_lifecycle_id
    assert len({first.entry_leg_id, first.protection_leg_id, first.close_leg_id}) == 3
    assert hyperliquid.economic_lifecycle_id != first.economic_lifecycle_id
    assert hyperliquid.intent_id != first.intent_id


@pytest.mark.parametrize(
    "field",
    [
        "case_manifest_sha256",
        "execution_binding_sha256",
        "execution_capability_snapshot_sha256",
        "capital_authorization_receipt_sha256",
    ],
)
def test_material_identity_digests_are_lowercase_sha256(field: str) -> None:
    with pytest.raises(ValidationError):
        _intent(**{field: "not-a-sha"})


@pytest.mark.parametrize(
    "commissions",
    [
        {f"C{i}": "0.1" for i in range(17)},
        {"USDT": "NaN"},
        {"usd!": "0.1"},
        {"USDT": "1e-3"},
    ],
)
def test_outcome_commissions_are_a_bounded_decimal_string_map(commissions: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        IntentOutcome.model_validate(
            {
                "intent_id": "1" * 64,
                "execution_state": "PENDING",
                "commissions_by_currency": commissions,
                "updated_at_ms": 1,
            }
        )


def test_outcome_distinguishes_unknown_commissions_from_known_zero() -> None:
    unknown = IntentOutcome.model_validate(
        {
            "intent_id": "1" * 64,
            "execution_state": "PENDING",
            "commissions_by_currency": None,
            "updated_at_ms": 1,
        }
    )
    known_zero = unknown.model_copy(update={"commissions_by_currency": {}})

    assert unknown.commissions_by_currency is None
    assert known_zero.commissions_by_currency == {}
