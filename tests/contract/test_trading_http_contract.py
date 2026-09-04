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
NOW = 1_900_000_000_000


# #532: one stored admission row whose `evidence` carries `market_key`, the key #510 PR-2 added to the
# `instrument_unmapped` rejection. The ledger's jsonb is the truth; the read contract renders whatever a
# writer stored under it rather than re-declaring its shape.
_GATE_ROW: dict[str, Any] = {
    "source_key": "oi:evt-oi-dell:oi_signal_v1",
    "underlying_key": "crypto:DELL",
    "trigger_kind": "oi",
    "source_observed_at_ms": NOW - 120_000,
    "case_id": None,
    "status": "REJECTED",
    "stage": "venue",
    "reason": "instrument_unmapped",
    "retryable": False,
    "evidence": {
        "market_key": "crypto:perp:DELL:USDT",
        "venue": "binance.usdm",
        "gate_version": "trading_admission_v9",
        "gate_config_digest": "d" * 64,
    },
    "first_evaluated_at_ms": NOW - 119_000,
    "last_evaluated_at_ms": NOW - 60_000,
    "attempt_count": 1,
}


class _Trading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.persisted: list[Any] = []

    def latest_case_created_at_ms(self) -> int:
        return NOW

    def runtime_summary(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(("runtime_summary", kwargs))
        return {"cases_24h": 1, "signals_24h": 1}

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
                    "policy_id": "source_native_oi_smart_money_long_v5",
                    "policy_version": "source_native_oi_smart_money_long_v5",
                    "policy_config_digest": "b" * 64,
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
                "market_key": "crypto:perp:SOL:USDT",
                "direction": "long",
                "observed_at_ns": NOW * 1_000_000,
                "expires_at_ns": (NOW + 180_000) * 1_000_000,
            }
        ]

    def console_executions(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_executions", kwargs))
        return [
            {
                "source": "signal",
                "entry_id": "c" * 64,
                "case_id": "case-sol",
                "market_key": "crypto:perp:SOL:USDT",
                "direction": "long",
                "observed_at_ns": NOW * 1_000_000,
                "disposition_reason": "accepted",
                "order_status": "submitted_or_unknown",
                "fill_quantity": "0.049",
                "fill_avg_price": "10000",
                "stop_trigger_price": "9800",
                "position_status": "closed",
                "exit_price": "9805.5",
                "realized_pnl_usd": "-9.53",
                "exit_reason": "stop_filled",
                "last_observed_at_ns": (NOW + 60_000) * 1_000_000,
            },
            {
                "source": "signal",
                "entry_id": "d" * 64,
                "case_id": "case-btc",
                "market_key": "crypto:perp:BTC:USDT",
                "direction": "long",
                "observed_at_ns": NOW * 1_000_000,
                "disposition_reason": "entries_paused",
                "order_status": None,
                "fill_quantity": None,
                "fill_avg_price": None,
                "stop_trigger_price": None,
                "position_status": None,
                "exit_price": None,
                "realized_pnl_usd": None,
                "exit_reason": None,
                "last_observed_at_ns": NOW * 1_000_000,
            },
            # #528 PR-3. A manual entry is the same fold under the Command's own id, and it has no
            # Case: the desk renders the row without a Case identity rather than inventing one.
            {
                "source": "manual",
                "entry_id": "e" * 64,
                "case_id": None,
                "market_key": "crypto:perp:BTC:USDT",
                "direction": "short",
                "observed_at_ns": NOW * 1_000_000,
                "disposition_reason": "accepted",
                "order_status": "filled",
                "fill_quantity": "0.011",
                "fill_avg_price": "81126.9",
                "stop_trigger_price": "81938.2",
                "position_status": "closed",
                "exit_price": "81100.0",
                "realized_pnl_usd": "-1.11984726",
                "exit_reason": "flatten",
                "last_observed_at_ns": (NOW + 90_000) * 1_000_000,
            },
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
                "market_key": None,
                "direction": None,
                "disposition": "accepted",
                "disposition_reason": "entries_paused",
            }
        ]

    def gate_decisions_since(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("gate_decisions_since", kwargs))
        return [_GATE_ROW]

    def candidate_admission_report(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "candidate_counts_24h": {},
            "candidate_reasons_24h": {},
            "latest_source_at_ms": None,
            "latest_gate_eligible_at_ms": None,
        }

    def gate_decision_for_source_key(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("gate_decision_for_source_key", kwargs))
        return _GATE_ROW if kwargs.get("source_key") == _GATE_ROW["source_key"] else None


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
    settings = Settings(ws_token=TOKEN)
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
    assert data["execution"]["startup_reconciled"] is False
    assert data["execution"]["account_flat"] is False
    assert {"singleton_ready", "portfolio_ready", "control_plane_ready", "audit_ready", "day_start_ready"}.isdisjoint(
        data["execution"]
    )
    # #537 PR-4: the six identity facts nothing rendered are gone too.
    assert {
        "runtime_release",
        "config_sha256",
        "runtime_revision",
        "image_digest",
        "credential_fingerprint",
        "lifecycle_state",
    }.isdisjoint(data["execution"])
    assert data["execution"]["routes_count"] == 0
    assert data["execution"]["facts_expire_at_ms"] is None
    assert data["counts"] == {"cases_24h": 1, "signals_24h": 1}
    # #528: the four counts nothing rendered are gone, and so is the whole `alpha` block -- the
    # policy identity is on every Case row that used it.
    assert "alpha" not in data
    assert "capital" not in data and "bindings" not in data and "budget" not in data


def test_case_and_signal_are_separate_durable_aggregates(client: tuple[TestClient, _Trading]) -> None:
    api, _ = client
    case = api.get("/api/trading/cases", params={"token": TOKEN}).json()["data"]["cases"][0]
    signal = api.get("/api/trading/signals", params={"token": TOKEN}).json()["data"]["signals"][0]

    assert case["state"] == "SIGNAL_EMITTED"
    assert case["market_key"] == signal["market_key"] == "crypto:perp:SOL:USDT"
    assert signal["case_id"] == case["case_id"]
    # #537 PR-3: the desk's policy identity is read from the manifest the lane froze, which is the copy
    # `_decide_one` compares before it decides anything. The three columns beside it are gone.
    assert case["policy_id"] == case["policy_version"] == "source_native_oi_smart_money_long_v5"
    assert case["policy_config_digest"] == "b" * 64
    for forbidden in ("quantity", "notional", "leverage", "account", "route", "order"):
        assert forbidden not in signal
    assert "alpha_metadata" not in signal


def test_gate_renders_a_stored_evidence_key_no_schema_enumerates(client: tuple[TestClient, _Trading]) -> None:
    """#532. `market_key` reached the ledger in #510 PR-2 and turned the whole read into a 500."""

    api, _ = client
    decisions = api.get("/api/trading/gate", params={"token": TOKEN}).json()["data"]["decisions"]
    single = api.get("/api/trading/gate/evt-oi-dell", params={"token": TOKEN, "lane": "oi"}).json()["data"]

    assert [decision["gate_evidence"] for decision in decisions] == [
        {
            "market_key": "crypto:perp:DELL:USDT",
            "venue": "binance.usdm",
            "gate_version": "trading_admission_v9",
            "gate_config_digest": "d" * 64,
        }
    ]
    assert single["decision"]["gate_evidence"]["market_key"] == "crypto:perp:DELL:USDT"
    # The rulebook that decided the row is one of those keys now, not two fields of its own (#537 PR-3).
    assert "gate_version" not in decisions[0]
    assert "gate_config_digest" not in decisions[0]


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
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "request_id": "11111111-1111-4111-8111-111111111111",
            "requested_at_ms": NOW,
            "text": "/resume operator review complete",
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
    assert not hasattr(persisted, "confirmation_identity")


def test_console_command_post_authenticates_with_the_session_read_token(
    client: tuple[TestClient, _Trading], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#520 PR-B: one bearer for reads and the one write; the separate 0600 file is gone."""

    api, trading = client
    monkeypatch.setattr(trading_routes.time, "time_ns", lambda: NOW * 1_000_000)

    response = api.post(
        "/api/trading/execution/commands",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "request_id": "44444444-4444-4444-8444-444444444444",
            "requested_at_ms": NOW,
            "text": "/resume operator review complete",
        },
    )

    assert response.status_code == 200
    persisted = trading.persisted[0].value
    assert persisted.action == "resume_entries"
    assert persisted.reason == "operator review complete"
    assert persisted.authentication_identity == "http-operator-write-token:v1"
    # The query parameter every read route accepts is still not a write credential.
    assert (
        api.post(
            f"/api/trading/execution/commands?token={TOKEN}",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 401
    )
    assert len(trading.persisted) == 1


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
        headers={"Authorization": f"Bearer {TOKEN}"},
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
            headers={"Authorization": "Bearer wrong-token", "Content-Type": "application/json"},
        ).status_code
        == 401
    )
    assert (
        api.post(
            "/api/trading/execution/commands",
            content=b"{}",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "text/plain"},
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
    assert trading.persisted == []


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("/halt incident", "operator_console_action_unsupported"),
        ("/long crypto:perp:BTC:USDT 30", "operator_console_action_unsupported"),
        ("/flatten account 30 CONFIRM", "operator_command_invalid"),
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
        headers={"Authorization": f"Bearer {TOKEN}"},
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
        "/api/trading/executions",
    ):
        assert api.get(path).status_code == 401


def test_filters_and_cursors_fail_closed(client: tuple[TestClient, _Trading]) -> None:
    api, trading = client
    assert api.get("/api/trading/cases", params={"token": TOKEN, "state": "emitted"}).status_code == 200
    case_call = next(kwargs for name, kwargs in trading.calls if name == "console_cases")
    assert case_call["states"] == ("SIGNAL_EMITTED",)
    assert api.get("/api/trading/cases", params={"token": TOKEN, "state": "OPEN"}).status_code == 400
    assert api.get("/api/trading/signals", params={"token": TOKEN, "cursor": "broken"}).status_code == 400


def test_executions_is_one_row_per_entry_identity_with_a_backend_derived_stage(
    client: tuple[TestClient, _Trading],
) -> None:
    """#528 PR-1/PR-3. The desk table reads a stage word, never a correlation the browser rebuilds."""

    api, trading = client
    data = api.get("/api/trading/executions", params={"token": TOKEN}).json()["data"]

    closed, refused, manual = data["executions"]
    assert closed["source"] == "signal"
    assert closed["entry_id"] == "c" * 64
    assert closed["case_id"] == "case-sol"
    assert closed["stage"] == "closed"
    assert closed["disposition"] == "accepted"
    assert closed["disposition_reason"] == "accepted"
    assert closed["exit_price"] == "9805.5"
    assert closed["realized_pnl_usd"] == "-9.53"
    assert closed["exit_reason"] == "stop_filled"
    assert closed["stop_trigger_price"] == "9800"
    assert refused["stage"] == "rejected"
    assert refused["disposition"] == "rejected"
    assert refused["disposition_reason"] == "entries_paused"
    assert refused["realized_pnl_usd"] is None

    # A manual entry is its own row, keyed on the Command that opened it and holding no Case.
    assert manual["source"] == "manual"
    assert manual["entry_id"] == "e" * 64
    assert manual["case_id"] is None
    assert manual["stage"] == "closed"
    assert manual["disposition"] == "accepted"
    assert manual["exit_reason"] == "flatten"
    assert manual["realized_pnl_usd"] == "-1.11984726"

    # The Command rows come from the same window, and their stage reads the disposition alone.
    assert data["commands"] == [
        {
            "command_id": "f" * 64,
            "action": "pause_entries",
            "reason": "operator investigation",
            "requested_at_ns": NOW * 1_000_000,
            "operator_identity": "telegram:user:7001",
            "stage": "accepted",
        }
    ]
    assert data["complete"] is True
    assert data["window_hours"] == 24
    executions_call = next(kwargs for name, kwargs in trading.calls if name == "console_executions")
    assert executions_call["limit"] == 101
