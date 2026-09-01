"""Read-only Case, Signal, Observation, and disabled-runtime HTTP contract."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings
from tracefold.trading import DecisionRuntimeV1

TOKEN = "trading-contract-token"
NOW = 1_900_000_000_000


class _Trading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def decision_runtime(self) -> DecisionRuntimeV1:
        return DecisionRuntimeV1(state="RUNNING", heartbeat_at_ms=NOW, reason=None, updated_at_ms=NOW)

    def runtime_summary(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(("runtime_summary", kwargs))
        return {
            "cases_24h": 1,
            "signals_24h": 1,
            "no_trade_24h": 0,
            "blocked_24h": 0,
            "cases_open": 0,
            "signals_unexpired": 1,
        }

    def console_cases(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_cases", kwargs))
        return [
            {
                "case_id": "case-sol",
                "underlying_key": "crypto:SOL",
                "trigger_kind": "oi",
                "primary_source_key": "oi:evt-sol:oi_signal_v1",
                "manifest": {
                    "manifest_version": "trading_manifest_v10",
                    "market_key": "crypto:perp:SOL:USDT",
                    "primary_trigger": {"venue": "binance.usdm"},
                    "policy_config": {"min_oi_change_bps": 500},
                    "contexts": {
                        "oi": {"oi_change_bps": 720, "oi_value_usd": 32_000_000},
                        "market": {"mark_price": "200", "pre_move_bps": 25},
                    },
                },
                "manifest_sha256": "a" * 64,
                "state": "SIGNAL_EMITTED",
                "policy_decision": "long",
                "policy_reason": "smart_money_long",
                "policy_checks": {"checks": []},
                "observed_at_ms": NOW - 2_000,
                "case_created_at_ms": NOW - 1_000,
                "decided_at_ms": NOW,
                "strategy_id": "source_native_oi_smart_money_long_v4",
                "strategy_version": "source_native_oi_smart_money_long_v4",
                "strategy_config_digest": "b" * 64,
            }
        ]

    def case_counts(self, **kwargs: Any) -> dict[str, int]:
        return {"SIGNAL_EMITTED": 1}

    def case_reason_counts(self, **kwargs: Any) -> dict[str, int]:
        return {"smart_money_long": 1}

    def console_signals(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_signals", kwargs))
        return [
            {
                "seq": 1,
                "signal_id": "c" * 64,
                "case_id": "case-sol",
                "alpha_contract_sha256": "d" * 64,
                "market_key": "crypto:perp:SOL:USDT",
                "direction": "long",
                "observed_at_ns": NOW * 1_000_000,
                "expires_at_ns": (NOW + 180_000) * 1_000_000,
                "evidence_sha256": "e" * 64,
                "alpha_metadata": {"policy_rule": "smart_money_long"},
            }
        ]

    def console_execution_observations(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_execution_observations", kwargs))
        return []

    def console_operator_intents(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_operator_intents", kwargs))
        return [
            {
                "seq": 4,
                "command_id": "f" * 64,
                "target_profile_id": "binance_usdm_primary",
                "action": "pause_entries",
                "scope": "entries",
                "reason": "operator investigation",
                "operator_identity": "telegram:user:7001",
                "requested_at_ns": NOW * 1_000_000,
                "expires_at_ns": (NOW + 300_000) * 1_000_000,
                "confirmed": False,
                "market_key": None,
                "direction": None,
                "disposition": "accepted",
                "disposition_reason": "entries_paused",
            }
        ]

    def gate_decisions_since(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def candidate_admission_report(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "candidate_counts_24h": {},
            "candidate_reasons_24h": {},
            "latest_source_at_ms": None,
            "latest_gate_eligible_at_ms": None,
        }

    def gate_decision_for_source_key(self, **kwargs: Any) -> None:
        return None


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


def test_status_keeps_execution_truthfully_disabled(client: tuple[TestClient, _Trading]) -> None:
    api, _ = client
    data = api.get("/api/trading/status", params={"token": TOKEN}).json()["data"]

    assert data["decision"]["state"] == "RUNNING"
    assert data["execution"] == {
        "mode": "disabled",
        "profile_id": "binance_usdm_primary",
        "account_slot": "binance_usdm_primary",
        "ready": False,
        "reason": "disabled",
    }
    assert data["counts"]["signals_24h"] == 1
    assert data["alpha"]["policy_id"] == "source_native_oi_smart_money_long_v4"
    assert "capital" not in data and "bindings" not in data and "budget" not in data


def test_case_and_signal_are_separate_durable_aggregates(client: tuple[TestClient, _Trading]) -> None:
    api, _ = client
    case = api.get("/api/trading/cases", params={"token": TOKEN}).json()["data"]["cases"][0]
    signal = api.get("/api/trading/signals", params={"token": TOKEN}).json()["data"]["signals"][0]

    assert case["state"] == "SIGNAL_EMITTED"
    assert case["market_key"] == signal["market_key"] == "crypto:perp:SOL:USDT"
    assert signal["case_id"] == case["case_id"]
    for forbidden in ("quantity", "notional", "leverage", "account", "route", "order"):
        assert forbidden not in signal


def test_observations_are_empty_while_runtime_is_disabled(client: tuple[TestClient, _Trading]) -> None:
    api, _ = client
    data = api.get("/api/trading/execution/observations", params={"token": TOKEN}).json()["data"]
    assert data["observations"] == []
    assert data["complete"] is True


def test_commands_are_read_only_authenticated_intent_projections(client: tuple[TestClient, _Trading]) -> None:
    api, _ = client
    data = api.get("/api/trading/execution/commands", params={"token": TOKEN}).json()["data"]
    command = data["commands"][0]

    assert command["action"] == "pause_entries"
    assert command["disposition"] == "accepted"
    assert command["expired"] is False
    for forbidden in ("authentication_identity", "confirmation_identity", "quantity", "leverage"):
        assert forbidden not in command


def test_retired_execution_routes_are_absent_and_current_routes_are_authenticated(
    client: tuple[TestClient, _Trading],
) -> None:
    api, _ = client
    for path in ("/api/trading/intents", "/api/trading/capabilities", "/api/trading/evidence"):
        assert api.get(path, params={"token": TOKEN}).status_code == 404
    for path in (
        "/api/trading/cases",
        "/api/trading/signals",
        "/api/trading/execution/commands",
        "/api/trading/execution/observations",
    ):
        assert api.get(path).status_code == 401


def test_filters_and_cursors_fail_closed(client: tuple[TestClient, _Trading]) -> None:
    api, trading = client
    assert api.get("/api/trading/cases", params={"token": TOKEN, "state": "emitted"}).status_code == 200
    case_call = next(kwargs for name, kwargs in trading.calls if name == "console_cases")
    assert case_call["states"] == ("SIGNAL_EMITTED",)
    assert api.get("/api/trading/cases", params={"token": TOKEN, "state": "OPEN"}).status_code == 400
    assert api.get("/api/trading/signals", params={"token": TOKEN, "cursor": "broken"}).status_code == 400
