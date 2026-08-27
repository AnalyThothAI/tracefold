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
        # The three frozen case facts the join now carries for an order row too (#282).
        "pre_move_bps": 187,
        "strategy_config": {"max_price_move_bps": 1000, "min_price_move_bps": 0},
        # A traded Case with an `unclear` quadrant: the smart-money lane accepts a move above the shared
        # band, so `policy_reason` here says nothing at all about why the quadrant was unclear.
        "regime_reason": "move_above_band_chasing",
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

    def status_counts(
        self,
        *,
        since_ms: int,
        now_ms: int,
        day_key: str | None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "status_counts",
                {"since_ms": since_ms, "now_ms": now_ms, "day_key": day_key},
            )
        )
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
            "cases_today_by_state": {"ORDER_PREPARED": 3, "POLICY_REJECTED": 4},
            "policy_allowed_today": 3,
            "closed_orders_today": 2,
            "active_orders": 1,
            "funnel_day_key": "2026-08-25",
            "latest_case_created_at_ms": NOW - 400_000,
            "latest_order_prepared_at_ms": NOW - 399_000,
            "latest_position_opened_at_ms": NOW - 398_000,
            "latest_position_closed_at_ms": None,
        }

    def candidate_admission_report(self, *, now_ms: int, trigger_kind: str = "oi") -> dict[str, Any]:
        self.calls.append(("candidate_admission_report", {"now_ms": now_ms, "trigger_kind": trigger_kind}))
        return {
            "candidate_counts_24h": {"REJECTED": 71, "CASE_CREATED": 1, "DEFERRED": 2},
            "candidate_counts_7d": {"REJECTED": 398, "CASE_CREATED": 4, "EXPIRED": 3},
            "candidate_reasons_24h": {
                "eligibility:oi_value_below_floor": 20,
                "eligibility:rank_above_limit": 51,
                "routing:no_native_perp": 2,
                "freeze:case_created": 1,
            },
            "candidate_reasons_7d": {"eligibility:rank_above_limit": 300},
            "latest_source_at_ms": NOW - 60_000,
            "latest_gate_eligible_at_ms": NOW - 400_000,
        }

    def console_case_for_source_key(self, *, primary_source_key: str) -> dict[str, Any] | None:
        self.calls.append(("console_case_for_source_key", {"primary_source_key": primary_source_key}))
        return None

    def gate_decision_for_source_key(self, *, source_key: str) -> dict[str, Any] | None:
        self.calls.append(("gate_decision_for_source_key", {"source_key": source_key}))
        if source_key != "oi:evt-oi-storj:oi_signal_v1":
            return None
        return {
            "source_key": source_key,
            "gate_version": "trading_candidate_gate_v1",
            "gate_config_digest": "f" * 64,
            "trigger_kind": "oi",
            "underlying_key": "crypto:STORJ",
            "source_observed_at_ms": NOW - 120_000,
            "status": "REJECTED",
            "stage": "eligibility",
            "reason": "oi_value_below_floor",
            "retryable": False,
            "evidence": {
                "venue": "binance",
                "oi_value_usd": 3_190_000,
                "floor": 5_000_000,
                "whale_oi_ratio_bps": 6_593,
                "source_decision": "drop",
                "source_rule": "whale_ratio_below_threshold",
            },
            "case_id": None,
            "first_evaluated_at_ms": NOW - 119_000,
            "last_evaluated_at_ms": NOW - 60_000,
            "attempt_count": 30,
        }

    def gate_decisions_since(self, *, since_ms: int, trigger_kind: str = "oi", limit: int) -> list[dict[str, Any]]:
        self.calls.append(
            ("gate_decisions_since", {"since_ms": since_ms, "trigger_kind": trigger_kind, "limit": limit})
        )
        refused = self.gate_decision_for_source_key(source_key="oi:evt-oi-storj:oi_signal_v1")
        assert refused is not None
        admitted = {
            **refused,
            "source_key": "oi:evt-oi-hype:oi_signal_v1",
            "underlying_key": "crypto:HYPE",
            "status": "CASE_CREATED",
            "stage": "freeze",
            "reason": "case_created",
            "case_id": "case-hype",
        }
        # A source key that is not the deterministic OI contract. It is still one of the lane's answers.
        foreign = {**refused, "source_key": "news:6f2a", "underlying_key": None, "trigger_kind": "news"}
        return [refused, admitted, foreign]

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
                "pre_move_bps": 731,
                "strategy_config": {"max_price_move_bps": 1000, "min_price_move_bps": 0},
                "regime_reason": "quadrant",
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
    assert data["counts"]["cases_today_by_state"] == {
        "ORDER_PREPARED": 3,
        "POLICY_REJECTED": 4,
    }
    assert data["counts"]["closed_orders_today"] == 2
    assert data["counts"]["policy_allowed_today"] == 3
    assert data["counts"]["active_orders"] == 1
    assert "funnel_24h" not in data["counts"]
    # #264: the half that outlives the UTC day roll and answers a lane sitting at zero orders. The
    # reason keys are `stage:reason` from a closed vocabulary — never a symbol, never a source key.
    assert data["counts"]["candidate_counts_24h"] == {"REJECTED": 71, "CASE_CREATED": 1, "DEFERRED": 2}
    assert data["counts"]["candidate_reasons_24h"]["eligibility:oi_value_below_floor"] == 20
    assert data["counts"]["candidate_counts_7d"]["EXPIRED"] == 3
    # Two milestones on either side of admission: a recent source with no recent case is a gate
    # problem, a recent case with no order is a strategy or a risk problem.
    assert data["counts"]["latest_source_at_ms"] == NOW - 60_000
    assert data["counts"]["latest_gate_eligible_at_ms"] == NOW - 400_000
    assert data["counts"]["latest_position_closed_at_ms"] is None


def test_every_key_the_gate_can_put_in_evidence_is_declared_on_the_published_schema() -> None:
    """A key the schema does not name would 500 one event, and only that event.

    `TradingGateEvidenceData` forbids extras on purpose — the file's rule is that nothing reaches a
    browser unnamed — but that turns "someone added an evidence key" into a runtime failure on exactly
    the frames an operator is trying to diagnose. Driving the gate through every refusal it can produce
    and comparing the union of keys against the schema moves that to here.
    """

    from tracefold.app.http.schemas.trading import TradingGateEvidenceData
    from tracefold.trading.candidate.blacklist import Blacklist
    from tracefold.trading.candidate.eligibility import EligibilityPolicy, Rejected, oi_candidate
    from tracefold.trading.candidate.gate import (
        GateConfig,
        admit_route,
        admit_trigger,
        case_created,
        defer,
        reject,
        source_rejected,
    )
    from tracefold.trading.contracts import OiTradeCandidate

    then = 1_787_000_000_000

    def _row(**kwargs: Any) -> dict[str, Any]:
        row = {
            "event_id": "e1",
            "final_decision": "push",
            "source_rule": "opening_move_with_whale_concentration",
            "ingest_mode": "live",
            "program_version": "news_oi_signal_v1",
            "metric_version": "oi_signal_v1",
            "source_strategy_id": "1019",
            "source_contract_version": "opennews_oi_source_v1",
            "measurement_window_ms": 300_000,
            "symbol": "DOGE",
            "direction": "rise",
            "oi_change_bps": 1_548,
            "oi_value_usd": 73_010_000,
            "whale_long_profit_bps": 9_900,
            "whale_oi_ratio_bps": 21_097,
            "rank_in_window": 1,
            "observed_at_ms": then,
            "verdict_created_at_ms": then,
            "venue": "hyperliquid",
            "learning_epoch": "program_v7",
            "program_sha256": "a" * 64,
            "policy_version": "news_triage_policy_v10",
            "editorial_origin": "telemetry_deterministic",
            "editorial_sha256": "b" * 64,
            "scored_judgment_sha256": "c" * 64,
            "runtime_manifest_sha": "d" * 64,
        }
        row.update(kwargs)
        return row

    def _fact(**kwargs: Any) -> OiTradeCandidate:
        parsed = oi_candidate(_row(**kwargs))
        assert isinstance(parsed, OiTradeCandidate)
        return parsed

    config = GateConfig.from_policy(EligibilityPolicy(), venue_priority=("binance",))
    deny = Blacklist.from_rows([{"base_symbol": "BTC", "reason": "benchmark_large_cap"}])
    results = [
        case_created(_fact(), case_id="c1"),
        defer(_fact(), stage="market_context", reason="market_data_unavailable"),
        reject(_fact(), stage="market_context", reason="market_data_invalid"),
    ]
    for kwargs in (
        {"rank_in_window": 9},
        {"oi_value_usd": 1},
        {"symbol": "BTC"},
        {"observed_at_ms": then - 3_600_000},
    ):
        refusal = admit_trigger(_fact(**kwargs), now_ms=then, config=config, blacklist=deny)
        assert refusal is not None
        results.append(refusal)
    for venue in ("okx", "hyperliquid"):
        routing = admit_route(_fact(venue=venue), config=config)
        assert routing is not None
        results.append(routing)
    for rule_row in (_row(direction="sideways"), _row(ingest_mode="recovery")):
        parsed = oi_candidate(rule_row)
        assert isinstance(parsed, Rejected)
        results.append(source_rejected(parsed, source_key="oi:e1:oi_signal_v1", observed_at_ms=then))

    emitted = {key for result in results for key in result.evidence}
    declared = set(TradingGateEvidenceData.model_fields)
    assert emitted <= declared, f"undeclared gate evidence keys reach the browser: {sorted(emitted - declared)}"
    # And the schema is not carrying names nothing produces, which would read as a contract that exists.
    assert declared - emitted == set()


def test_an_event_with_no_case_says_why_rather_than_only_that_there_is_none() -> None:
    """#264: `case: null` used to be the whole answer, and it is the same shape for four situations."""

    settings = Settings(ws_token=TOKEN)
    app = create_app(settings=settings)
    app.state.service = _FakeRuntime(settings, _FakeTradingRepository())
    api = TestClient(app)
    response = api.get("/api/trading/events/evt-oi-storj", params={"token": TOKEN, "lane": "oi"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["joinable"] is True
    # `exclude_unset` keeps an absent case absent rather than shipping a null the page must branch on.
    assert "case" not in data
    assert (data["gate_status"], data["gate_stage"], data["gate_reason"]) == (
        "REJECTED",
        "eligibility",
        "oi_value_below_floor",
    )
    assert data["gate_retryable"] is False
    # The number it failed on and the number it failed against, so a threshold argument is settled here.
    assert data["gate_evidence"]["oi_value_usd"] == 3_190_000
    assert data["gate_evidence"]["floor"] == 5_000_000
    # The reader's own verdict rides along and stays a separate fact from the capital lane's refusal.
    assert data["gate_evidence"]["source_decision"] == "drop"
    # Re-reading the same source 30 times is one row, and the ledger says so rather than logging 30.
    assert data["gate_attempt_count"] == 30
    assert data["gate_config_digest"] == "f" * 64


def test_an_event_the_gate_has_never_evaluated_reports_an_absence_not_a_refusal() -> None:
    settings = Settings(ws_token=TOKEN)
    app = create_app(settings=settings)
    app.state.service = _FakeRuntime(settings, _FakeTradingRepository())
    api = TestClient(app)
    response = api.get("/api/trading/events/evt-unseen", params={"token": TOKEN, "lane": "oi"})

    data = response.json()["data"]
    assert data["joinable"] is True
    # An explicit null, not an omission: "the lane has not evaluated this source under any gate
    # version" is an answer, and a missing key would read as the console forgetting to ask.
    assert data["gate_status"] is None
    assert "gate_reason" not in data


def test_the_gate_batch_answers_a_whole_window_of_frames_at_once(client) -> None:
    """#269. A frame table renders a page of rows; asking `/events/{id}` per row is a hundred reads."""

    api, trading = client
    response = api.get("/api/trading/gate", params={"token": TOKEN})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["complete"] is True
    assert data["window_hours"] == 24
    by_event = {row["event_id"]: row for row in data["decisions"]}
    # Keyed on the Event id the source key round-trips to, exactly as the order projection recovers it.
    assert by_event["evt-oi-storj"]["gate_reason"] == "oi_value_below_floor"
    assert by_event["evt-oi-storj"]["gate_evidence"]["floor"] == 5_000_000
    assert by_event["evt-oi-storj"]["base_symbol"] == "STORJ"
    assert by_event["evt-oi-hype"]["gate_status"] == "CASE_CREATED"
    assert by_event["evt-oi-hype"]["case_id"] == "case-hype"
    # A source whose key is not the deterministic contract is listed with no Event link rather than
    # dropped: the distributions in `/status` count it, and the page's own total has to agree.
    assert by_event[None]["source_key"] == "news:6f2a"
    assert by_event[None]["trigger_kind"] == "news"
    window = next(kwargs for name, kwargs in trading.calls if name == "gate_decisions_since")
    assert window["limit"] > len(data["decisions"])


def test_the_status_publishes_the_rules_that_actually_decide_a_frame(client) -> None:
    """#269. `floors` is the settings document; these two are the rules the lane holds and files under."""

    api, _ = client
    data = api.get("/api/trading/status", params={"token": TOKEN}).json()["data"]

    gate = data["gate"]
    assert gate["version"] == "trading_candidate_gate_v1"
    assert len(gate["config_digest"]) == 64
    assert (gate["max_rank_in_window"], gate["min_oi_value_usd"]) == (2, 20_000_000)
    assert gate["venue_priority"] == ["binance", "hyperliquid"]

    strategies = {row["strategy_id"]: row for row in data["strategies"]}
    smart_money = strategies["oi_smart_money_momentum_v1"]
    assert smart_money["trigger_kinds"] == ["oi"]
    # The template's own numbers, not the 95 % whale-profit floor of the strategy beside it. A console
    # measuring a smart-money case against `floors.min_whale_long_profit_bps` was using the wrong rule.
    assert smart_money["config"]["min_whale_oi_ratio_bps"] == "5000"
    assert smart_money["config"]["min_oi_change_bps"] == "500"
    assert smart_money["config"]["min_whale_long_profit_bps"] == "0"
    assert strategies["news_oi_alignment_v1"]["config"]["min_whale_long_profit_bps"] == "9500"
    # The admission floor stays out of every strategy: one owner, and the digest says which (#264).
    assert "min_oi_value_usd" not in smart_money["config"]


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

    # Both halves answer the same three questions about the case (#282). An order row is still a case row,
    # and a console that could name the pre-move for a refusal but not for a fill had to explain the fill
    # against whatever is configured today — the one row on the page where that is least acceptable.
    for row in (order, rejected):
        assert row["pre_move_bps"] is not None, row["case_id"]
        assert row["strategy_config"]["max_price_move_bps"] == "1000", row["case_id"]
        # Stringified exactly as `/status` stringifies the running ones, so one parser reads both.
        assert all(isinstance(value, str) for value in row["strategy_config"].values())
        assert row["regime_reason"] is not None, row["case_id"]

    # `regime` alone cannot answer why: `assess()` reaches `unclear` four ways and only one of them is
    # "price and OI did not align". `policy_reason` is the *strategy's* later answer and is `None` on a
    # Case it went on to trade, so without this column the console had to invent a cause or claim the
    # ledger recorded none — over a manifest that recorded it.
    assert order["policy_reason"] is None
    assert order["regime_reason"] == "move_above_band_chasing"


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


def test_orders_can_bind_closed_rows_to_one_utc_budget_day(client) -> None:
    api, trading = client

    response = api.get(
        "/api/trading/orders",
        params={"token": TOKEN, "day": "2026-08-25"},
    )

    assert response.status_code == 200
    call = next(call for call in trading.calls if call[0] == "console_orders")
    assert call[1]["closed_from_ms"] == 1_787_616_000_000
    assert call[1]["closed_until_ms"] == 1_787_702_400_000


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

    bad_day = api.get("/api/trading/orders", params={"token": TOKEN, "day": "2026-02-30"})
    assert bad_day.status_code == 400
    assert bad_day.json()["error"] == "trading_orders_day_invalid"

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
