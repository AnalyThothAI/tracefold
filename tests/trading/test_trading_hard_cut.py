"""The retired execution authority cannot re-enter through config or public surfaces."""

from __future__ import annotations

import importlib.util
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tracefold.app.http.schemas import trading as schemas
from tracefold.platform.config.models import Settings
from tracefold.trading import TradeIntent


@pytest.mark.parametrize(
    "module",
    (
        "tracefold.integrations.opentrade",
        "tracefold.trading.execution.order",
        "tracefold.trading.execution.paper",
        "tracefold.trading.execution.submission",
        "tracefold.trading.pipeline.reconcile",
        "tracefold.trading.storage.orders",
    ),
)
def test_legacy_execution_writers_are_not_importable(module: str) -> None:
    assert importlib.util.find_spec(module) is None


@pytest.mark.parametrize(
    "retired",
    (
        {"mode": "paper"},
        {"mode": "live_reviewed"},
        {"live_symbol": "SOL"},
        {"account_ref": "canary"},
        {"venues": {"binance_enabled": True}},
        {"opentrade": {"base_url": "https://example.invalid"}},
        {"nautilus": {"accept_intents": True}},
    ),
)
def test_retired_backend_switches_are_rejected_by_settings(retired: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"trading": retired})


@pytest.mark.parametrize("value", ("0", "-1", "10.01"))
def test_target_notional_is_the_only_bounded_execution_value(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"trading": {"order": {"fixed_notional_usd": value}}})


def test_intent_policy_owns_every_other_execution_value() -> None:
    intent = TradeIntent.create(
        case_id="case-1",
        case_manifest_sha256="a" * 64,
        created_at_ms=1_900_000_000_000,
        reference_price=Decimal("100"),
        target_notional_usd=Decimal("7.5"),
    )
    assert intent.execution_environment == "BINANCE_USDM_DEMO"
    assert intent.instrument_id == "SOLUSDT-PERP.BINANCE"
    assert intent.side == "long"
    assert intent.valid_until_ms - intent.created_at_ms == 60_000
    assert (intent.stop_loss_bps, intent.max_holding_ms) == (200, 180_000)
    assert (intent.max_entry_drift_bps, intent.max_spread_bps) == (25, 30)


def test_http_contract_is_case_intent_outcome_only() -> None:
    assert hasattr(schemas, "TradingIntentData")
    assert hasattr(schemas, "TradingIntentsData")
    assert not hasattr(schemas, "TradingOrderData")
    assert not hasattr(schemas, "TradingOrdersData")
    public_fields = set(schemas.TradingIntentData.model_fields)
    for retired in ("payload", "account_ref", "remote_order_id", "mode", "quantity", "order_id"):
        assert retired not in public_fields
