"""Trading reads plus the one authenticated operator-command append contract."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.app.http.routes import trading as trading_routes
from tracefold.platform.config.models import Settings

TOKEN = "trading-contract-token"
WRITE_TOKEN = "operator-write-" + "w" * 40
NOW = 1_900_000_000_000


class _Trading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.persisted: list[Any] = []

    def latest_case_created_at_ms(self) -> int:
        return NOW

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

    def execution_runtime_state(self, _account_slot: str) -> None:
        return None

    def execution_runtime_control_state(self, _account_slot: str) -> None:
        return None

    def console_cases(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_cases", kwargs))
        return [
            {
                "case_id": "case-sol",
                "underlying_key": "crypto:SOL",
                "trigger_kind": "oi",
                "primary_source_key": "oi:evt-sol:oi_signal_v1",
                "manifest": {
                    "manifest_version": "trading_manifest_v11",
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
                "account_slot": "binance_usdm_primary",
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

    def persist_operator_intent(self, prepared: Any) -> SimpleNamespace:
        self._trading.persisted.append(prepared)
        return SimpleNamespace(
            command_id=prepared.value.command_id,
            seq=7,
            disposition="awaiting_runtime",
            reason=None,
        )


@pytest.fixture
def client(tmp_path: Path) -> tuple[TestClient, _Trading]:
    token_path = tmp_path / "trading_console_write_token"
    token_path.write_text(WRITE_TOKEN + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    settings = Settings(
        ws_token=TOKEN,
        trading={"control": {"console_write_token_file": token_path.name}},
    )
    settings.set_config_dir(tmp_path)
    trading = _Trading()
    app = create_app(settings=settings)
    app.state.service = _Runtime(settings, trading)
    return TestClient(app), trading


def test_status_keeps_execution_truthfully_disabled(client: tuple[TestClient, _Trading]) -> None:
    api, _ = client
    data = api.get("/api/trading/status", params={"token": TOKEN}).json()["data"]

    assert data["decision"] == {"last_case_at_ms": NOW}
    expected = {
        "mode": "disabled",
        "account_slot": "binance_usdm_primary",
        "alive": False,
        "execution_safe": False,
        "entries_armed": False,
        "entry_block_reason": "disabled",
    }
    assert {key: data["execution"][key] for key in expected} == expected
    assert data["execution"]["runtime_release"] is None
    assert data["execution"]["singleton_ready"] is False
    assert data["execution"]["account_flat"] is False
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


def test_console_command_post_records_only_an_intent(
    client: tuple[TestClient, _Trading], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, trading = client
    monkeypatch.setattr(trading_routes.time, "time_ns", lambda: NOW * 1_000_000)
    response = api.post(
        "/api/trading/execution/commands",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
        json={
            "request_id": "11111111-1111-4111-8111-111111111111",
            "requested_at_ms": NOW,
            "text": "/resume operator review complete CONFIRM",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "command_id": trading.persisted[0].value.command_id,
        "seq": 7,
        "requested_at_ns": NOW * 1_000_000,
        "disposition": "awaiting_runtime",
        "reason": None,
        "truth": "intent_recorded_not_runtime_or_venue",
    }
    persisted = trading.persisted[0].value
    assert persisted.action == "resume_entries"
    assert persisted.scope == "entries"
    assert persisted.reason == "operator review complete"
    assert persisted.account_slot == "binance_usdm_primary"
    assert persisted.authentication_identity == "http-operator-write-token:v1"
    assert persisted.confirmation_identity is not None


def test_console_command_post_offloads_the_synchronous_database_append(
    client: tuple[TestClient, _Trading], monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _ = client
    offloaded: list[tuple[Any, tuple[Any, ...]]] = []

    async def run_in_worker(function: Any, *args: Any) -> Any:
        offloaded.append((function, args))
        return function(*args)

    monkeypatch.setattr(trading_routes.time, "time_ns", lambda: NOW * 1_000_000)
    monkeypatch.setattr(trading_routes, "run_in_threadpool", run_in_worker, raising=False)

    response = api.post(
        "/api/trading/execution/commands",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
        json={
            "request_id": "33333333-3333-4333-8333-333333333333",
            "requested_at_ms": NOW,
            "text": "/pause database maintenance",
        },
    )

    assert response.status_code == 200
    assert len(offloaded) == 1
    assert offloaded[0][0].__name__ == "persist_operator_intent"


def test_console_command_post_authenticates_before_body_and_rejects_query_tokens(
    client: tuple[TestClient, _Trading],
) -> None:
    api, trading = client
    path = f"/api/trading/execution/commands?token={TOKEN}"

    assert api.post(path, content=b"not-json", headers={"Content-Type": "application/json"}).status_code == 401
    assert (
        api.post(
            "/api/trading/execution/commands",
            content=b"{}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        ).status_code
        == 401
    )
    assert (
        api.post(
            "/api/trading/execution/commands",
            content=b"{}",
            headers={"Authorization": f"Bearer {WRITE_TOKEN}", "Content-Type": "text/plain"},
        ).json()["error"]
        == "content_type_json_required"
    )
    assert (
        api.post(
            "/api/trading/execution/commands",
            content=b"{}",
            headers=[(b"authorization", b"Bearer \xff"), (b"content-type", b"application/json")],
        ).status_code
        == 401
    )
    shared_token = "shared-bootstrap-write-token-" + "x" * 32
    api.app.state.service.settings.ws_token = shared_token
    token_path = api.app.state.service.settings.trading_console_write_token_file()
    assert token_path is not None
    token_path.write_text(shared_token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    assert (
        api.post(
            "/api/trading/execution/commands",
            content=b"{}",
            headers={"Authorization": f"Bearer {shared_token}", "Content-Type": "application/json"},
        ).status_code
        == 401
    )
    assert trading.persisted == []


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("/halt incident CONFIRM", "operator_console_action_unsupported"),
        ("/long crypto:perp:BTC:USDT 30", "operator_console_action_unsupported"),
        ("/flatten account 30", "operator_command_invalid"),
    ],
)
def test_console_command_post_keeps_the_closed_non_capital_grammar(
    client: tuple[TestClient, _Trading],
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    error: str,
) -> None:
    api, trading = client
    monkeypatch.setattr(trading_routes.time, "time_ns", lambda: NOW * 1_000_000)
    response = api.post(
        "/api/trading/execution/commands",
        headers={"Authorization": f"Bearer {WRITE_TOKEN}"},
        json={
            "request_id": "22222222-2222-4222-8222-222222222222",
            "requested_at_ms": NOW,
            "text": text,
        },
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": error, "field": "text"}
    assert trading.persisted == []


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
