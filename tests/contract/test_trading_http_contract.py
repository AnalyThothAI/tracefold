"""One route per durable aggregate, and no route that mixes two (#331).

The shape these tests pin is the product's: Source/Admission at `/gate`, Case/Decision at `/cases`,
Intent/Outcome at `/intents`, orthogonal runtime facts at `/status`. What they refuse is the mixed contract that came
before — `/intents` returning `cases_without_intents` beside its Intents, so a page could not tell "no
Intent" from "no Case", and a failed request fell through an empty array into a truthful-looking
"nothing happened".
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings
from tracefold.trading.catalog import VenueBindingRuntime

TOKEN = "trading-contract-token"
NOW = 1_790_000_000_000
POLICY_ID = "binance_oi_smart_money_long_v2"


def _intent(**overrides: Any) -> dict[str, Any]:
    row = {
        "intent_id": "intent-sol",
        "intent_version": "trade_intent_v2",
        "case_id": "case-sol",
        "case_manifest_sha256": "a" * 64,
        "execution_environment": "BINANCE_USDM_DEMO",
        "execution_capability_snapshot_sha256": "c" * 64,
        "blacklist_revision_at_emission": 3,
        "blacklist_snapshot_sha256_at_emission": "d" * 64,
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
        "entry_fenced_at_ms": None,
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
        "strategy_id": POLICY_ID,
        "strategy_version": POLICY_ID,
        "primary_source_key": "oi:evt-oi-sol:oi_signal_v1",
    }
    row.update(overrides)
    return row


def _case(**overrides: Any) -> dict[str, Any]:
    row = {
        "case_id": "case-hype",
        "underlying_key": "crypto:HYPE",
        "primary_source_key": "oi:evt-oi-hype:oi_signal_v1",
        "trigger_kind": "oi",
        "strategy_id": POLICY_ID,
        "strategy_version": POLICY_ID,
        "strategy_config_digest": "e" * 64,
        "state": "NO_TRADE",
        "policy_decision": "no_trade",
        "policy_reason": "smart_money_ratio_below_or_equal_floor",
        "capital_disposition": "not_applicable",
        "capital_reason": None,
        # The Case's own frozen thresholds, deliberately different from today's configuration.
        "policy_config": {"min_whale_oi_ratio_bps": 8_000, "max_price_move_bps": 600},
        "policy_checks": {
            "policy_id": POLICY_ID,
            "decision": "no_trade",
            "rule": "smart_money_ratio_below_or_equal_floor",
            "checks": [
                {
                    "check": "whale_oi_ratio_bps",
                    "operator": ">",
                    "threshold": "8000",
                    "measured": "5424",
                    "passed": False,
                }
            ],
        },
        "manifest_version": "trading_manifest_v8",
        "provider_symbol": "HYPEUSDT",
        "mark_price": "0.0950",
        "pre_move_bps": 731,
        "oi_change_bps": 1_548,
        "oi_value_usd": 23_010_000,
        "whale_oi_ratio_bps": 5_424,
        "whale_long_profit_bps": 9_074,
        "observed_at_ms": NOW - 401_000,
        "case_created_at_ms": NOW - 400_000,
        "decided_at_ms": NOW - 399_000,
        "intent_id": None,
    }
    row.update(overrides)
    return row


def _gate_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "source_key": "oi:evt-oi-hl:oi_signal_v1",
        "gate_version": "trading_admission_v3",
        "gate_config_digest": "f" * 64,
        "trigger_kind": "oi",
        "underlying_key": "crypto:HL",
        "source_observed_at_ms": NOW - 500_000,
        "status": "RESEARCH_ONLY",
        "stage": "venue",
        "reason": "research_only_venue",
        "retryable": False,
        "evidence": {"venue": "hyperliquid", "live_exchange_id": "binance"},
        "case_id": None,
        "first_evaluated_at_ms": NOW - 499_000,
        "last_evaluated_at_ms": NOW - 498_000,
        "attempt_count": 2,
    }
    row.update(overrides)
    return row


class _Trading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def runtime_state(self) -> dict[str, Any]:
        return {
            "control": "PAUSED",
            "blacklist_revision": 3,
        }

    def decision_runtime(self) -> dict[str, Any]:
        return {"state": "RUNNING", "heartbeat_at_ms": NOW, "reason": None}

    def binding_runtime_rows(self, *, now_ms: int) -> list[VenueBindingRuntime]:
        del now_ms
        return [
            VenueBindingRuntime(
                binding=binding,
                credential_state="unconfigured",
                credential_fingerprint=None,
                runtime_state="stopped",
                account_state="unknown",
                catalog_state="ready",
                catalog_snapshot_sha256=digest,
                catalog_captured_at_ms=NOW - 1_000,
                heartbeat_at_ms=None,
                reason="credentials_unconfigured",
                updated_at_ms=NOW,
            )
            for binding, digest in (("BINANCE_USDM", "b" * 64), ("HYPERLIQUID_PERP", "c" * 64))
        ]

    def runtime_summary(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("runtime_summary", kwargs))
        return {
            "day_key": "2026-08-25",
            "active_intents": 1,
            "entries_today": 0,
            "closed_intents_today": 0,
            "cases_24h": 2,
            "intents_24h": 1,
            "latest_case_created_at_ms": NOW - 400_000,
            "latest_intent_emitted_at_ms": NOW - 180_000,
            "latest_entry_fenced_at_ms": None,
            "latest_position_opened_at_ms": None,
            "latest_position_closed_at_ms": None,
        }

    def candidate_admission_report(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("candidate_admission_report", kwargs))
        return {
            "candidate_counts_24h": {"CASE_CREATED": 1, "RESEARCH_ONLY": 4},
            "candidate_counts_7d": {},
            "candidate_reasons_24h": {"venue:research_only_venue": 4},
            "candidate_reasons_7d": {},
            "latest_source_at_ms": NOW - 400_000,
            "latest_gate_eligible_at_ms": NOW - 401_000,
        }

    def console_intents(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_intents", kwargs))
        return [_intent()]

    def intent_counts(self, **kwargs: Any) -> dict[str, dict[str, int]]:
        self.calls.append(("intent_counts", kwargs))
        return {"by_state": {"PENDING": 1}, "by_outcome": {}, "by_reason": {}}

    def console_cases(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_cases", kwargs))
        return [_case()]

    def case_counts(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(("case_counts", kwargs))
        return {"NO_TRADE": 1, "POLICY_REJECTED": 225}

    def case_reason_counts(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(("case_reason_counts", kwargs))
        return {"smart_money_ratio_below_or_equal_floor": 1}

    def case_capital_reason_counts(self, **kwargs: Any) -> dict[str, int]:
        self.calls.append(("case_capital_reason_counts", kwargs))
        return {"credentials_unconfigured": 1}

    def gate_decisions_since(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("gate_decisions_since", kwargs))
        return [_gate_row()]

    def gate_decision_for_source_key(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("gate_decision_for_source_key", kwargs))
        return _gate_row(source_key=kwargs["source_key"])


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


def test_status_publishes_orthogonal_durable_runtime_facts_and_policy_identity(client) -> None:
    api, _ = client
    response = api.get("/api/trading/status", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["budget"] == {"target_notional_usd": "10"}
    assert data["decision"] == {"state": "RUNNING", "heartbeat_at_ms": NOW, "reason": None}
    assert data["capital"] == {"control": "PAUSED", "blacklist_revision": 3}
    assert [row["binding"] for row in data["bindings"]] == ["BINANCE_USDM", "HYPERLIQUID_PERP"]
    assert all(row["credential_state"] == "unconfigured" for row in data["bindings"])
    assert data["counts"]["active_intents"] == 1
    assert data["counts"]["cases_24h"] == 2
    # The identity of the policy a *new* Case would be frozen under. Never applied to an existing one.
    assert data["policy"]["policy_id"] == POLICY_ID
    assert data["policy"]["config"]["min_oi_change_bps"] == "500"
    for retired in ("floors", "strategies", "funnel_today", "gate"):
        assert retired not in data
    assert "readiness" not in data
    assert "funnel" not in data["counts"]


def test_status_never_reads_credentials_or_calls_a_provider(
    client: tuple[TestClient, _Trading],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tracefold.integrations import trading_catalog
    from tracefold.platform.config import secret_file

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("serve_side_effect_forbidden")

    monkeypatch.setattr(secret_file, "read_secure_secret_text", forbidden)
    monkeypatch.setattr(trading_catalog, "fetch_binance_usdm_catalog", forbidden)
    monkeypatch.setattr(trading_catalog, "fetch_hyperliquid_perp_catalog", forbidden)

    api, _ = client
    response = api.get("/api/trading/status", params={"token": TOKEN})

    assert response.status_code == 200
    assert response.json()["data"]["decision"]["state"] == "RUNNING"


def test_intents_publish_execution_lifecycle_and_never_a_case_list(client) -> None:
    api, _ = client
    data = api.get("/api/trading/intents", params={"token": TOKEN}).json()["data"]

    assert data["complete"] is True
    intent = data["intents"][0]
    assert intent["execution_state"] == "PENDING"
    assert intent["intent_version"] == "trade_intent_v2"
    assert (intent["instrument_id"], intent["side"]) == ("SOLUSDT-PERP.BINANCE", "long")
    assert intent["policy_id"] == POLICY_ID
    assert data["state_counts_24h"] == {"PENDING": 1}
    # The mixed shape is gone in the same change rather than kept as a second synonym.
    assert "cases_without_intents" not in data
    for retired in ("payload", "order_id", "remote_order_id", "account_ref", "mode", "case_state", "regime"):
        assert retired not in intent


def test_cases_publish_the_frozen_evidence_each_decision_was_taken_on(client) -> None:
    """#331 F2P 8: a Case's thresholds are the Case's, not the ones configured today."""

    api, _ = client
    data = api.get("/api/trading/cases", params={"token": TOKEN}).json()["data"]

    case = data["cases"][0]
    assert (case["case_id"], case["state"]) == ("case-hype", "NO_TRADE")
    assert case["policy_id"] == POLICY_ID
    assert case["policy_config"]["min_whale_oi_ratio_bps"] == "8000"
    assert case["policy_checks"] == [
        {
            "check": "whale_oi_ratio_bps",
            "operator": ">",
            "threshold": "8000",
            "measured": "5424",
            "passed": False,
        }
    ]
    assert case["oi_value_usd"] == 23_010_000
    assert case["intent_id"] is None
    assert data["state_counts_24h"]["POLICY_REJECTED"] == 225  # historical rows read as stored
    # No execution lifecycle on the Case aggregate.
    for retired in ("execution_state", "terminal_outcome", "reason_code", "regime", "mode"):
        assert retired not in case


def test_the_gate_surface_names_a_research_only_source_and_links_a_case_without_inferring_one(client) -> None:
    api, _ = client
    data = api.get("/api/trading/gate", params={"token": TOKEN}).json()["data"]

    decision = data["decisions"][0]
    assert (decision["gate_status"], decision["gate_stage"]) == ("RESEARCH_ONLY", "venue")
    assert decision["gate_reason"] == "research_only_venue"
    assert decision["research_only"] is True
    assert decision["case_id"] is None
    assert data["status_counts_24h"]["RESEARCH_ONLY"] == 4
    assert data["config"]["live_exchange_id"] == "binance"
    assert "venue_priority" not in data["config"]
    for retired in ("execution_state", "policy_reason", "state"):
        assert retired not in decision


def test_one_source_can_be_asked_about_by_event_id_and_an_unanswerable_lane_says_so(client) -> None:
    api, trading = client
    data = api.get("/api/trading/gate/evt-oi-hl", params={"token": TOKEN, "lane": "oi"}).json()["data"]
    assert data["joinable"] is True
    assert data["decision"]["gate_reason"] == "research_only_venue"
    call = next(kwargs for name, kwargs in trading.calls if name == "gate_decision_for_source_key")
    assert call["source_key"].startswith("oi:evt-oi-hl:")

    unasked = api.get("/api/trading/gate/evt-model", params={"token": TOKEN}).json()["data"]
    assert (unasked["joinable"], unasked["decision"]) == (False, None)


def test_filters_are_owned_by_the_aggregate_they_filter(client) -> None:
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

    assert api.get("/api/trading/cases", params={"token": TOKEN, "state": "blocked"}).status_code == 200
    cases_call = next(kwargs for name, kwargs in trading.calls if name == "console_cases")
    assert cases_call["states"] == ("BLOCKED",)

    bad_intent = api.get("/api/trading/intents", params={"token": TOKEN, "state": "OPEN"})
    assert bad_intent.status_code == 400
    assert bad_intent.json()["error"] == "trading_intents_state_invalid"
    bad_case = api.get("/api/trading/cases", params={"token": TOKEN, "state": "OPEN"})
    assert bad_case.status_code == 400
    assert bad_case.json()["error"] == "trading_cases_state_invalid"


def test_surface_is_authenticated_and_read_only(client) -> None:
    api, _ = client
    for path in ("/api/trading/intents", "/api/trading/cases", "/api/trading/gate"):
        assert api.get(path).status_code == 401
    for method in (api.post, api.put, api.patch, api.delete):
        assert method("/api/trading/cases", params={"token": TOKEN}).status_code in {404, 405}
