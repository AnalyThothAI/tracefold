"""The deliberately narrow v1 Demo execution policy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tracefold.trading import INTENT_POLICY_SHA256, IntentOutcome, TradeIntent


def _intent(**overrides: object) -> TradeIntent:
    values: dict[str, object] = {
        "case_id": "case-1",
        "case_manifest_sha256": "1" * 64,
        "intent_policy_sha256": INTENT_POLICY_SHA256,
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "created_at_ms": 1_900_000_000_000,
        "valid_until_ms": 1_900_000_060_000,
        "reference_price": Decimal("60000"),
        "target_notional_usd": Decimal("10"),
        "stop_loss_bps": 200,
        "max_holding_ms": 180_000,
        "max_entry_drift_bps": 25,
        "max_spread_bps": 30,
    }
    values.update(overrides)
    return TradeIntent.create(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instrument_id", "ETHUSDT-PERP.BINANCE"),
        ("target_notional_usd", Decimal("10.01")),
        ("valid_until_ms", 1_900_000_059_999),
        ("stop_loss_bps", 199),
        ("max_holding_ms", 1_799_999),
        ("max_entry_drift_bps", 24),
        ("max_spread_bps", 29),
    ],
)
def test_v1_policy_rejects_values_outside_the_single_frozen_demo_slice(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _intent(**{field: value})


def test_v1_policy_accepts_the_frozen_demo_slice() -> None:
    assert _intent().instrument_id == "SOLUSDT-PERP.BINANCE"


def test_v1_policy_identity_is_code_owned() -> None:
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
