"""The deliberately narrow v1 Demo execution policy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tracefold.trading import INTENT_POLICY_SHA256, BlacklistSnapshotV1, IntentOutcome, TradeIntent


def _intent(**overrides: object) -> TradeIntent:
    intent = TradeIntent.create(
        case_id="case-1",
        case_manifest_sha256="1" * 64,
        execution_capability_snapshot_sha256="2" * 64,
        blacklist_snapshot=BlacklistSnapshotV1(revision=0, active_rows=()),
        instrument_id="SOLUSDT-PERP.BINANCE",
        underlying_key="crypto:SOL",
        created_at_ms=1_900_000_000_000,
        reference_price=Decimal("60000"),
        target_notional_usd=Decimal("10"),
    )
    values: dict[str, object] = intent.model_dump()
    values.update(overrides)
    return TradeIntent.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_notional_usd", Decimal("10.01")),
        ("valid_until_ms", 1_900_000_059_999),
        ("stop_loss_bps", 199),
        ("max_holding_ms", 1_799_999),
        ("max_entry_drift_bps", 24),
        ("max_spread_bps", 29),
    ],
)
def test_v2_policy_rejects_values_outside_the_frozen_risk_slice(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _intent(**{field: value})


def test_v2_policy_accepts_dynamic_demo_instruments() -> None:
    intent = _intent()
    assert intent.instrument_id == "SOLUSDT-PERP.BINANCE"
    eth = TradeIntent.create(
        case_id="case-2",
        case_manifest_sha256="1" * 64,
        execution_capability_snapshot_sha256="2" * 64,
        blacklist_snapshot=BlacklistSnapshotV1(revision=0, active_rows=()),
        instrument_id="ETHUSDT-PERP.BINANCE",
        underlying_key="crypto:ETH",
        created_at_ms=1_900_000_000_000,
        reference_price=Decimal("3000"),
        target_notional_usd=Decimal("10"),
    )
    assert eth.instrument_id == "ETHUSDT-PERP.BINANCE"
    assert intent.intent_policy_sha256 == INTENT_POLICY_SHA256
    assert intent.valid_until_ms == intent.created_at_ms + 60_000
    assert intent.stop_loss_bps == 200
    assert intent.max_holding_ms == 180_000
    assert intent.max_entry_drift_bps == 25
    assert intent.max_spread_bps == 30


def test_v2_policy_identity_is_code_owned() -> None:
    with pytest.raises(ValidationError):
        _intent(intent_policy_sha256="3" * 64)


@pytest.mark.parametrize("field", ["case_manifest_sha256", "intent_policy_sha256"])
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
