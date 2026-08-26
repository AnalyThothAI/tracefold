"""The capital lane's read-only HTTP surface (#207 PR-W4, #104, #185).

Two GETs and no writes. The tests that matter here are not about shapes: they are about the surface staying
read-only, keeping the ledger's own state words, and never handing a browser a frozen provider payload.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings

TOKEN = "trading-contract-token"
NOW = 1_790_000_000_000


def _order(**overrides: Any) -> dict[str, Any]:
    row = {
        "average_price": None,
        "case_id": "case-wif",
        "strategy_id": "news_oi_alignment_v1",
        "strategy_version": "news_oi_alignment_v1",
        "trigger_kind": "news",
        "case_observed_at_ms": NOW - 200_000,
        "case_state": "ORDER_PREPARED",
        "created_at_ms": NOW - 180_000,
        "entry_reference": "0.8412",
        "exchange_id": "paper",
        "exit_attempt_total": 0,
        "exit_price": None,
        "exit_reason": None,
        "filled_quantity": "237.6",
        # Present in the row and deliberately absent from the response.
        "manifest": {"frozen": "inputs"},
        "mode": "paper",
        "must_close_at_ms": NOW + 3_600_000,
        "notional_usd": "200",
        "order_id": "order-wif",
        "payload": {"provider": "request body"},
        "policy_decision": "trade",
        "policy_reason": None,
        "primary_source_key": "oi:evt-oi-wif:oi_signal_v1",
        "position_closed_at_ms": None,
        "position_opened_at_ms": NOW - 120_000,
        "provider_attempt_count": 1,
        "provider_symbol": "WIFUSDT",
        "quantity": "237.6",
        "realized_bps": None,
        "regime": "buildup_up",
        "side": "buy",
        "state": "OPEN",
        "state_reason": None,
        "stop_price": "0.8244",
        "take_profit_price": None,
        "underlying_key": "crypto:WIF",
        "updated_at_ms": NOW - 60_000,
    }
    row.update(overrides)
    return row


class _FakeTradingRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def runtime_state(self) -> dict[str, Any]:
        return {
            "control": "RUNNING",
            "day_key": "2026-08-25",
            "funnel": {"cases": 9, "orders": 3, "bad": "x"},
            "orders_today": 3,
        }

    def status_counts(self, *, since_ms: int) -> dict[str, Any]:
        self.calls.append(("status_counts", {"since_ms": since_ms}))
        return {
            "cases_by_state": {"ORDER_PREPARED": 3, "POLICY_REJECTED": 4},
            "cases_by_trigger": {"news": 5, "oi": 4},
            "cases_by_strategy": {"news_oi_alignment_v1": 5, "oi_momentum_v1": 4},
            "shadow_by_strategy": {"liquidation_continuation_shadow_v1": 2},
            "shadow_by_rule": {"source_contract_incomplete": 2},
            "shadow_cohorts": {
                "liquidation_continuation_shadow_v1": {
                    "evaluated": 2,
                    "completed": 1,
                    "mean_return_bps": 12,
                }
            },
            "liquidation_promotion_ready": False,
            "liquidation_promotion_reason": "source_contract_incomplete",
            "orders_by_state": {"OPEN": 1, "CLOSED": 2},
            "closed_orders": 2,
            "closed_realized_bps": 12,
        }

    def console_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_orders", kwargs))
        return [_order()]

    def console_cases_without_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_cases_without_orders", kwargs))
        return [
            {
                "case_id": "case-hype",
                "strategy_id": "oi_momentum_v1",
                "strategy_version": "oi_momentum_v1",
                "trigger_kind": "oi",
                "created_at_ms": NOW - 400_000,
                "decided_at_ms": NOW - 399_000,
                "manifest": {"frozen": "inputs"},
                "mode": "paper",
                "observed_at_ms": NOW - 401_000,
                "policy_decision": "no_trade",
                "policy_reason": "whale_profit_below_floor",
                "primary_source_key": "oi:evt-oi-hype:oi_signal_v1",
                "regime": "buildup_up",
                "state": "POLICY_REJECTED",
                "underlying_key": "crypto:HYPE",
            }
        ]


class _FakeRepositories:
    def __init__(self, trading: _FakeTradingRepository) -> None:
        self.trading = trading


class _FakeRuntime:
    def __init__(self, settings: Settings, trading: _FakeTradingRepository) -> None:
        self.settings = settings
        self._trading = trading

    @contextmanager
    def repositories(self):
        yield _FakeRepositories(self._trading)


@pytest.fixture
def client() -> tuple[TestClient, _FakeTradingRepository]:
    settings = Settings(ws_token=TOKEN)
    app = create_app(settings=settings)
    trading = _FakeTradingRepository()
    app.state.service = _FakeRuntime(settings, trading)
    return TestClient(app), trading


def test_status_reports_the_mandate_and_never_claims_live_readiness(client) -> None:
    api, _ = client
    response = api.get("/api/trading/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    # Disabled is the shipped default, and it is an answer rather than an outage.
    assert data["readiness"]["enabled"] is False
    assert data["readiness"]["execution_backend"] == "disabled"
    assert data["readiness"]["live_ready"] is False
    assert data["readiness"]["live_readiness"] == "not_applicable"
    # Money is an exact decimal string end to end; a float here is how 0.1 + 0.2 reaches an operator.
    assert data["budget"]["notional_usd"] == "50"
    # 50 USDT x 200 bps x 4 orders. Exactly what the CLI prints, from the same `Decimal` property.
    assert data["budget"]["nominal_daily_stop_loss_usd"] == "4"
    assert data["budget"]["orders_today"] == 3
    # The two threshold sets stay apart: these are the capital floors, never the News gates.
    assert data["floors"]["min_whale_long_profit_bps"] == 9_500
    assert data["floors"]["min_oi_value_usd"] == "20000000"
    # A non-integer funnel value is dropped rather than crashing the read or reaching the browser as a string.
    # And it is named for the interval it actually covers: `merge_funnel` resets on `day_key`, so this is a
    # UTC calendar day, not the rolling 24 h the counts beside it are.
    assert data["counts"]["funnel_today"] == {"cases": 9, "orders": 3}
    assert data["counts"]["funnel_day_key"] == "2026-08-25"
    assert "funnel_24h" not in data["counts"]


def test_orders_carry_the_ledgers_own_state_and_no_frozen_payload(client) -> None:
    api, _ = client
    response = api.get("/api/trading/orders", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["complete"] is True
    order = data["orders"][0]
    # `OPEN` is the only state that has proven both a position and a native stop covering it (#185 P0-3);
    # it is returned verbatim, not translated into 已成交.
    assert order["state"] == "OPEN"
    assert order["base_symbol"] == "WIF" and order["underlying_key"] == "crypto:WIF"
    assert order["event_id"] == "evt-oi-wif"
    for leaked in ("payload", "manifest", "account_ref", "remote_order_id"):
        assert leaked not in order, leaked
    # The rejected population has no order to join through, and it is where the floors actually bite.
    rejected = data["cases_without_orders"][0]
    assert rejected["policy_reason"] == "whale_profit_below_floor"
    assert rejected["event_id"] == "evt-oi-hype"
    assert "manifest" not in rejected


def test_orders_never_invent_an_event_join_for_other_source_keys(client) -> None:
    api, trading = client
    trading.console_orders = lambda **_: [_order(primary_source_key="model:opaque-hash")]
    trading.console_cases_without_orders = lambda **_: [
        {
            **_FakeTradingRepository().console_cases_without_orders()[0],
            "primary_source_key": "oi:evt-wrong-version:oi_signal_v0",
        }
    ]

    data = api.get("/api/trading/orders", params={"token": TOKEN}).json()["data"]

    assert data["orders"][0]["event_id"] is None
    assert data["cases_without_orders"][0]["event_id"] is None


def test_orders_publish_when_the_batch_is_truncated(client) -> None:
    api, trading = client
    trading.console_orders = lambda **_: [_order(order_id=f"order-{index}") for index in range(101)]
    trading.console_cases_without_orders = lambda **_: []

    data = api.get("/api/trading/orders", params={"token": TOKEN}).json()["data"]
    assert len(data["orders"]) == 100
    assert data["complete"] is False


def test_orders_accepts_either_spelling_of_one_underlying(client) -> None:
    api, trading = client

    for value in ("WIF", "wif", "xyz-wif", "crypto:WIF"):
        api.get("/api/trading/orders", params={"token": TOKEN, "underlying": value})

    keys = [call[1]["underlying_key"] for call in trading.calls if call[0] == "console_orders"]
    assert keys == ["crypto:WIF"] * 4


def test_an_order_state_filter_asks_only_about_orders(client) -> None:
    api, trading = client

    api.get("/api/trading/orders", params={"token": TOKEN, "state": "active"})

    assert [call[0] for call in trading.calls] == ["console_orders"]
    states = trading.calls[0][1]["states"]
    # The active predicate deliberately includes the ambiguous states: an order whose provider write is
    # unresolved is *more* likely to be carrying a position than one that is merely open.
    assert {"AMBIGUOUS", "MANUAL_REVIEW_REQUIRED", "UNPROTECTED", "OPEN"} <= set(states)
    assert "CLOSED" not in states


def test_every_explicit_state_filter_asks_only_about_orders(client) -> None:
    """`all` included. It narrows to nothing, but it is still a question about orders."""

    api, trading = client

    api.get("/api/trading/orders", params={"token": TOKEN, "state": "all"})

    assert [call[0] for call in trading.calls] == ["console_orders"]
    assert trading.calls[0][1]["states"] == ()


def test_bad_query_values_are_refused_by_name(client) -> None:
    api, _ = client

    bad_state = api.get("/api/trading/orders", params={"token": TOKEN, "state": "OPEN"})
    assert bad_state.status_code == 400
    assert bad_state.json()["error"] == "trading_orders_state_invalid"

    bad_symbol = api.get("/api/trading/orders", params={"token": TOKEN, "underlying": "WIF USD"})
    assert bad_symbol.status_code == 400
    assert bad_symbol.json()["error"] == "trading_orders_underlying_invalid"

    unknown = api.get("/api/trading/orders", params={"token": TOKEN, "limit": "10"})
    assert unknown.status_code == 400


def test_the_surface_is_read_only(client) -> None:
    """There is no order write anywhere on HTTP. The three operator mutations stay on the CLI, where they
    run as `workers` — `tracefold_serve` carries `default_transaction_read_only = on` precisely so the
    internet-facing role cannot approve, reject or resolve an order."""

    api, _ = client
    for method in (api.post, api.put, api.patch, api.delete):
        for path in ("/api/trading/orders", "/api/trading/status", "/api/trading/orders/order-wif"):
            assert method(path, params={"token": TOKEN}).status_code in {404, 405}


def test_an_unauthenticated_read_is_refused(client) -> None:
    api, _ = client
    assert api.get("/api/trading/status").status_code == 401
    assert api.get("/api/trading/orders").status_code == 401
