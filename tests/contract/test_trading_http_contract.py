"""Read-only Case -> Intent -> Outcome HTTP contract for #283."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings

TOKEN = "trading-contract-token"
NOW = 1_790_000_000_000


def _intent(**overrides: Any) -> dict[str, Any]:
    row = {
        "intent_id": "intent-sol",
        "case_id": "case-sol",
        "case_manifest_sha256": "a" * 64,
        "execution_environment": "BINANCE_USDM_DEMO",
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "side": "long",
        "target_notional_usd": "10",
        "reference_price": "200",
        "valid_until_ms": NOW + 60_000,
        "execution_state": "PENDING",
        "execution_phase": None,
        "terminal_outcome": None,
        "reason_code": None,
        "actual_quantity": None,
        "protected_quantity": None,
        "avg_entry_price": None,
        "avg_exit_price": None,
        "stop_price": "196",
        "opened_at_ms": None,
        "protected_at_ms": None,
        "closed_at_ms": None,
        "flat_verified_at_ms": None,
        "realized_pnl_amount": None,
        "realized_pnl_currency": None,
        "commissions_by_currency": {},
        "created_at_ms": NOW - 180_000,
        "updated_at_ms": NOW - 60_000,
        "underlying_key": "crypto:SOL",
        "trigger_kind": "oi",
        "strategy_id": "oi_smart_money_momentum_v1",
        "strategy_version": "oi_smart_money_momentum_v1",
        "case_state": "INTENT_EMITTED",
        "regime": "buildup_up",
        "policy_decision": "long",
        "policy_reason": None,
        "pre_move_bps": 187,
        "strategy_config": {"allow_short": False},
        "regime_reason": "quadrant",
        "case_observed_at_ms": NOW - 200_000,
        "observed_at_ms": NOW - 200_000,
        "decided_at_ms": NOW - 181_000,
        "state": "INTENT_EMITTED",
        "primary_source_key": "oi:evt-oi-sol:oi_signal_v1",
    }
    row.update(overrides)
    return row


def _case(**overrides: Any) -> dict[str, Any]:
    row = {
        "case_id": "case-hype",
        "underlying_key": "crypto:HYPE",
        "trigger_kind": "oi",
        "strategy_id": "oi_smart_money_momentum_v1",
        "strategy_version": "oi_smart_money_momentum_v1",
        "state": "POLICY_REJECTED",
        "regime": "buildup_up",
        "policy_decision": "no_trade",
        "policy_reason": "whale_profit_below_floor",
        "pre_move_bps": 731,
        "strategy_config": {"max_price_move_bps": 1000},
        "regime_reason": "quadrant",
        "observed_at_ms": NOW - 401_000,
        "created_at_ms": NOW - 400_000,
        "decided_at_ms": NOW - 399_000,
        "primary_source_key": "oi:evt-oi-hype:oi_signal_v1",
    }
    row.update(overrides)
    return row


class _Trading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def runtime_state(self) -> dict[str, Any]:
        return {
            "control": "RUNNING",
            "day_key": "2026-08-25",
            "funnel": {"case_created": 1},
            "nautilus_ready": True,
            "nautilus_readiness_reason": "ready",
            "nautilus_unexpected_exposure": False,
            "nautilus_heartbeat_at_ms": NOW,
        }

    def status_counts(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("status_counts", kwargs))
        return {
            "cases_by_state": {"INTENT_EMITTED": 1},
            "cases_today_by_state": {"INTENT_EMITTED": 1},
            "intents_by_state": {"PENDING": 1},
            "outcomes_by_state": {},
            "policy_allowed_today": 1,
            "policy_allowed_24h": 1,
            "entries_today": 0,
            "closed_intents_today": 0,
            "active_intents": 1,
            "funnel_day_key": "2026-08-25",
        }

    def candidate_admission_report(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("candidate_admission_report", kwargs))
        return {"candidate_counts_24h": {"CASE_CREATED": 1}}

    def console_intents(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_intents", kwargs))
        return [_intent()]

    def console_cases_without_intents(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_cases_without_intents", kwargs))
        return [_case()]

    def gate_decisions_since(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("gate_decisions_since", kwargs))
        return []

    def console_case_for_source_key(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("console_case_for_source_key", kwargs))
        return _intent()

    def gate_decision_for_source_key(self, **kwargs: Any) -> None:
        self.calls.append(("gate_decision_for_source_key", kwargs))


class _Runtime:
    def __init__(self, settings: Settings, trading: _Trading) -> None:
        self.settings = settings
        self._trading = trading

    @contextmanager
    def repositories(self):
        yield type("Repositories", (), {"trading": self._trading})()


@pytest.fixture
def client() -> tuple[TestClient, _Trading]:
    settings = Settings(ws_token=TOKEN)
    trading = _Trading()
    app = create_app(settings=settings)
    app.state.service = _Runtime(settings, trading)
    return TestClient(app), trading


def test_status_publishes_one_frozen_execution_authority(client) -> None:
    api, _ = client
    response = api.get("/api/trading/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["budget"] == {"target_notional_usd": "10", "max_entries_per_utc_day": 1}
    assert (
        data["readiness"]
        | {
            "execution_authority": "nautilus",
            "execution_environment": "BINANCE_USDM_DEMO",
            "instrument_id": "SOLUSDT-PERP.BINANCE",
        }
        == data["readiness"]
    )
    assert data["readiness"]["engine_ready"] is True
    assert data["counts"]["active_intents"] == 1
    for retired in ("mode", "execution_backend", "live_ready", "accept_intents"):
        assert retired not in data["readiness"]


def test_intents_publish_native_lifecycle_and_exclude_legacy_payloads(client) -> None:
    api, _ = client
    response = api.get("/api/trading/intents", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["complete"] is True
    intent = data["intents"][0]
    assert (intent["case_state"], intent["execution_state"]) == ("INTENT_EMITTED", "PENDING")
    assert (intent["instrument_id"], intent["side"]) == ("SOLUSDT-PERP.BINANCE", "long")
    assert data["cases_without_intents"][0]["state"] == "POLICY_REJECTED"
    for retired in ("payload", "order_id", "remote_order_id", "account_ref", "mode"):
        assert retired not in intent


def test_event_projection_joins_case_to_its_intent(client) -> None:
    api, _ = client
    data = api.get(
        "/api/trading/events/evt-oi-sol",
        params={"token": TOKEN, "lane": "oi"},
    ).json()["data"]

    assert data["joinable"] is True
    assert data["case"]["case_id"] == "case-sol"
    assert data["intent"]["intent_id"] == "intent-sol"


def test_filters_use_intent_vocabulary(client) -> None:
    api, trading = client
    assert (
        api.get(
            "/api/trading/intents",
            params={"token": TOKEN, "state": "active", "underlying": "sol"},
        ).status_code
        == 200
    )
    call = next(kwargs for name, kwargs in trading.calls if name == "console_intents")
    assert call["underlying_key"] == "crypto:SOL"
    assert set(call["states"]) == {"PENDING", "IN_FLIGHT", "OPEN_PROTECTED", "MANUAL_REVIEW"}

    bad = api.get("/api/trading/intents", params={"token": TOKEN, "state": "OPEN"})
    assert bad.status_code == 400
    assert bad.json()["error"] == "trading_intents_state_invalid"


def test_surface_is_authenticated_and_read_only(client) -> None:
    api, _ = client
    assert api.get("/api/trading/intents").status_code == 401
    for method in (api.post, api.put, api.patch, api.delete):
        assert method("/api/trading/intents", params={"token": TOKEN}).status_code in {404, 405}
