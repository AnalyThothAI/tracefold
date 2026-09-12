"""Read-only trading monitoring contract."""

from __future__ import annotations

import time
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
# 2020-09-13, so a Signal carrying it has expired against any clock this test can run under.
_LONG_EXPIRED_MS = 1_600_000_000_000


class _Trading:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.persisted: list[Any] = []

    def latest_case_created_at_ms(self) -> int:
        return NOW

    def execution_runtime_state(self, _account_slot: str) -> None:
        return None

    def execution_runtime_control_state(self, _account_slot: str) -> None:
        return None

    def console_case(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("console_case", kwargs))
        if kwargs.get("case_id") != "case-sol":
            return None
        return {
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

    def case_counts(self, **kwargs: Any) -> dict[str, int]:
        return {"SIGNAL_EMITTED": 1}

    def case_reason_counts(self, **kwargs: Any) -> dict[str, int]:
        return {"smart_money_long": 1}

    def gate_counts(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("gate_counts", kwargs))
        return [
            {"status": "REJECTED", "reason": "oi_value_below_floor", "count": 553},
            {"status": "CASE_CREATED", "reason": "case_created", "count": 31},
            {"status": "DEFERRED", "reason": None, "count": 2},
        ]

    def console_realized_totals(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("console_realized_totals", kwargs))
        return {
            "realized_known_today_usd": "-9.53",
            "realized_known_total_usd": "56.40",
            "closed_today": 1,
            "closed_total": 12,
            "pnl_known_today": 1,
            "pnl_known_total": 11,
            "pnl_missing_today": 0,
            "pnl_missing_total": 1,
            "pnl_complete_today": True,
            "pnl_complete_total": False,
        }

    def console_executions(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("console_executions", kwargs))
        rows: list[dict[str, Any]] = [
            {
                "source": "signal",
                "entry_id": "c" * 64,
                "case_id": "case-sol",
                "market_key": "crypto:perp:SOL:USDT",
                "direction": "long",
                "observed_at_ns": NOW * 1_000_000,
                "expires_at_ns": (NOW + 300_000) * 1_000_000,
                "disposition_reason": "accepted",
                "order_status": "submitted_or_unknown",
                "order_reject_reason": None,
                "fill_quantity": "0.049",
                "fill_avg_price": "10000",
                "stop_trigger_price": "9800",
                "entry_filled_at_ns": (NOW + 20_000) * 1_000_000,
                "position_closed_at_ns": (NOW + 60_000) * 1_000_000,
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
                "expires_at_ns": (NOW + 300_000) * 1_000_000,
                "disposition_reason": "entries_paused",
                "order_status": None,
                "order_reject_reason": None,
                "fill_quantity": None,
                "fill_avg_price": None,
                "stop_trigger_price": None,
                "entry_filled_at_ns": None,
                "position_closed_at_ns": None,
                "position_status": None,
                "exit_price": None,
                "realized_pnl_usd": None,
                "exit_reason": None,
                "last_observed_at_ns": NOW * 1_000_000,
            },
            # #604 T3 (audit A4). A Signal only a retryable refusal ever touched: no disposition row,
            # no order, and a TTL the clock has already passed.
            {
                "source": "signal",
                "entry_id": "a" * 64,
                "case_id": "case-ttl",
                "market_key": "crypto:perp:ETH:USDT",
                "direction": "long",
                # A real past instant, not one relative to this module's fixed future `NOW`: the
                # route compares the TTL against its own wall clock.
                "observed_at_ns": (_LONG_EXPIRED_MS - 300_000) * 1_000_000,
                "expires_at_ns": _LONG_EXPIRED_MS * 1_000_000,
                "disposition_reason": None,
                "order_status": None,
                "order_reject_reason": None,
                "fill_quantity": None,
                "fill_avg_price": None,
                "stop_trigger_price": None,
                "entry_filled_at_ns": None,
                "position_closed_at_ns": None,
                "position_status": None,
                "exit_price": None,
                "realized_pnl_usd": None,
                "exit_reason": None,
                "last_observed_at_ns": (_LONG_EXPIRED_MS - 300_000) * 1_000_000,
            },
            # A rejected entry order, carrying the venue's own words (#604 T1).
            {
                "source": "signal",
                "entry_id": "b" * 64,
                "case_id": "case-rej",
                "market_key": "crypto:perp:SOL:USDT",
                "direction": "long",
                "observed_at_ns": NOW * 1_000_000,
                "expires_at_ns": (NOW + 300_000) * 1_000_000,
                "disposition_reason": "accepted",
                "order_status": "rejected",
                "order_reject_reason": "Order's notional must be no smaller than 5.0",
                "fill_quantity": None,
                "fill_avg_price": None,
                "stop_trigger_price": None,
                "entry_filled_at_ns": None,
                "position_closed_at_ns": None,
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
                "expires_at_ns": None,
                "disposition_reason": "accepted",
                "order_status": "filled",
                "order_reject_reason": None,
                "fill_quantity": "0.011",
                "fill_avg_price": "81126.9",
                "stop_trigger_price": "81938.2",
                "entry_filled_at_ns": (NOW + 30_000) * 1_000_000,
                "position_closed_at_ns": (NOW + 90_000) * 1_000_000,
                "position_status": "closed",
                "exit_price": "81100.0",
                "realized_pnl_usd": "-1.11984726",
                "exit_reason": "flatten",
                "last_observed_at_ns": (NOW + 90_000) * 1_000_000,
            },
        ]
        return [{**row, "pnl_known": row.get("realized_pnl_usd") is not None, "history_complete": True} for row in rows]


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

    assert set(data) == {"decision", "execution"}
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
    # #528: the four counts nothing rendered are gone, and so is the whole `alpha` block -- the
    # policy identity is on every Case row that used it.
    assert "alpha" not in data
    assert "capital" not in data and "bindings" not in data and "budget" not in data


def test_status_publishes_one_field_per_operator_question(client: tuple[TestClient, _Trading]) -> None:
    """#537 PR-5. Every raw fact whose derived answer is published beside it is gone.

    The two observation clocks were the input to `facts_expire_at_ms` and `reconciliation_age_ms`, the
    two readiness counts said what `current_account` carries row by row, raw `account_flat` said what
    the venue had not proven, and the two 24 h counts cost a `count(*)` per table on every poll of
    every route for chrome figures that no longer exist.
    """

    api, trading = client
    data = api.get("/api/trading/status", params={"token": TOKEN}).json()["data"]

    assert {
        "heartbeat_at_ns",
        "reconciliation_observed_at_ns",
        "positions_count",
        "open_orders_count",
        "account_flat",
    }.isdisjoint(data["execution"])
    assert "counts" not in data and "window_hours" not in data and "measured_at_ms" not in data
    assert [name for name, _ in trading.calls] == []


def test_case_reads_its_policy_identity_off_the_manifest(client: tuple[TestClient, _Trading]) -> None:
    api, _ = client
    case = api.get("/api/trading/cases", params={"token": TOKEN, "case_id": "case-sol"}).json()["data"]["cases"][0]

    assert case["state"] == "SIGNAL_EMITTED"
    assert case["market_key"] == "crypto:perp:SOL:USDT"
    assert case["base_symbol"] == "SOL"
    # #537 PR-3: the desk's policy identity is read from the manifest the lane froze, which is the copy
    # `_decide_one` compares before it decides anything. The three columns beside it are gone.
    assert case["policy_id"] == "source_native_oi_smart_money_long_v5"
    assert case["policy_config_digest"] == "b" * 64
    # #537 PR-5. `policy_decision` was a required Literal over a nullable column -- the exact shape
    # that turns a stored NULL into a 500 (#532) -- and the four measured OI numbers were a second
    # copy of what `policy_checks` carries beside the threshold each was measured against.
    # #604 T3. `policy_config` was the same duplication one level up: `policy_checks[].threshold`
    # already carries every number that was actually tested, beside what it was measured against, and
    # `policy_config_digest` still identifies the whole frozen set.
    assert {
        "underlying_key",
        "source_venue",
        "trigger_kind",
        "policy_version",
        "policy_decision",
        "policy_config",
        "oi_change_bps",
        "oi_value_usd",
        "whale_oi_ratio_bps",
        "whale_long_profit_bps",
    }.isdisjoint(case)


def test_cases_summary_does_not_fetch_rows_and_identity_reads_one_case(client: tuple[TestClient, _Trading]) -> None:
    """#604 T3. A Case is reached by its own identity, and no identity means no Case.

    The route used to send the newest 100 whole Cases on every 15 s poll and the desk rendered at most
    the one behind `?case=<id>` -- so the `NO_TRADE` Cases past the hundredth, 553 of 584 in a
    production day, were exactly the ones an operator could not open. There was never a cursor to
    follow either (#537 PR-5).
    """

    api, trading = client
    data = api.get("/api/trading/cases", params={"token": TOKEN}).json()["data"]

    assert set(data) == {
        "cases",
        "state_counts_24h",
        "reason_counts_24h",
        "admission_counts_24h",
        "complete",
        "window_hours",
        "total",
        "next_cursor",
        "window_from_ms",
        "window_to_ms",
    }
    # No identity, no Case -- and no Case read at all.
    assert data["cases"] == []
    assert data["complete"] is True
    assert [name for name, _ in trading.calls if name == "console_case"] == []
    # The distributions travel whether or not a drawer is open; they are the funnel, not the Case.
    assert data["state_counts_24h"] == {"SIGNAL_EMITTED": 1}
    assert data["reason_counts_24h"] == {"smart_money_long": 1}

    hit = api.get("/api/trading/cases", params={"token": TOKEN, "case_id": "case-sol"}).json()["data"]
    assert [row["case_id"] for row in hit["cases"]] == ["case-sol"]
    # An unknown identity is an empty answer, not an error: a Case can be purged out from under a link.
    miss = api.get("/api/trading/cases", params={"token": TOKEN, "case_id": "case-gone"})
    assert miss.status_code == 200
    assert miss.json()["data"]["cases"] == []
    # A malformed identity cannot name a primary key and is refused before the read.
    for value in ("case sol", "case%sol", "'; DROP", "-leading", "a" * 129):
        refused = api.get("/api/trading/cases", params={"token": TOKEN, "case_id": value})
        assert refused.status_code == 400, value
        assert refused.json() == {"ok": False, "error": "trading_cases_case_id_invalid", "field": "case_id"}
    assert api.get("/api/trading/cases", params={"token": TOKEN, "cursor": "anything"}).status_code == 400


def test_cases_publishes_the_admission_distribution_as_counts_not_rows(
    client: tuple[TestClient, _Trading],
) -> None:
    """#604 T3. The funnel's top, over the same 24 h window the two Case distributions use.

    This is a `count(*)` per `(status, reason)` pair -- at most a dozen objects however many frames
    the lane looked at. It is not the per-frame `decisions[]` #589 PR-2 deleted, which published one
    object per frame carrying its whole evidence blob: no frame identity, no evidence and no Case link
    travels with a count.
    """

    api, trading = client
    data = api.get("/api/trading/cases", params={"token": TOKEN}).json()["data"]

    assert data["admission_counts_24h"] == [
        {"status": "REJECTED", "reason": "oi_value_below_floor", "count": 553},
        {"status": "CASE_CREATED", "reason": "case_created", "count": 31},
        {"status": "DEFERRED", "reason": None, "count": 2},
    ]
    for item in data["admission_counts_24h"]:
        assert set(item) == {"status", "reason", "count"}
    # The same 24 h lower bound the two Case distributions beside it are counted over.
    gate_call = next(kwargs for name, kwargs in trading.calls if name == "gate_counts")
    assert data["window_hours"] == 24
    assert 0 < gate_call["since_ms"] <= int(time.time() * 1000) - 24 * 3_600_000 + 5_000


def test_retired_execution_routes_are_absent_and_current_routes_are_authenticated(
    client: tuple[TestClient, _Trading],
) -> None:
    api, _ = client
    for path in ("/api/trading/intents", "/api/trading/capabilities", "/api/trading/evidence"):
        assert api.get(path, params={"token": TOKEN}).status_code == 404
    # #537 PR-5. The Signal list and the two raw execution projections nothing in the browser called.
    for path in ("/api/trading/signals", "/api/trading/execution/observations"):
        assert api.get(path, params={"token": TOKEN}).status_code == 404
    # #589 PR-2. The admission ledger's two shapes, on the same terms: #553 PR-1 deleted the OI frame
    # table that joined each row to its Event, and `tracefold trading gate` reads the same statements.
    for path in ("/api/trading/gate", "/api/trading/gate/oi:evt:oi_signal_v1"):
        assert api.get(path, params={"token": TOKEN}).status_code == 404
    assert api.get("/api/trading/execution/commands", params={"token": TOKEN}).status_code == 404
    for path in ("/api/trading/cases", "/api/trading/executions", "/api/trading/status"):
        assert api.get(path).status_code == 401


def test_cases_rejects_obsolete_underlying_and_invalid_state(client: tuple[TestClient, _Trading]) -> None:
    api, trading = client
    assert api.get("/api/trading/cases", params={"token": TOKEN, "underlying": "BTC"}).status_code == 400
    assert api.get("/api/trading/cases", params={"token": TOKEN, "state": "emitted"}).status_code == 422
    assert [name for name, _ in trading.calls if name == "console_cases"] == []


def test_executions_is_one_row_per_entry_identity_with_a_backend_derived_stage(
    client: tuple[TestClient, _Trading],
) -> None:
    """#528 PR-1/PR-3. The desk table reads a stage word, never a correlation the browser rebuilds."""

    api, trading = client
    data = api.get("/api/trading/executions", params={"token": TOKEN}).json()["data"]

    closed, refused, stale, rejected_order, manual = data["executions"]
    assert closed["source"] == "signal"
    assert closed["entry_id"] == "c" * 64
    assert closed["case_id"] == "case-sol"
    assert closed["stage"] == "closed"
    assert closed["disposition_reason"] == "accepted"
    assert closed["exit_price"] == "9805.5"
    assert closed["realized_pnl_usd"] == "-9.53"
    assert closed["exit_reason"] == "stop_filled"
    assert closed["stop_trigger_price"] == "9800"
    # #604 T3. The two instants a holding time is the distance between; `observed_at_ns` is neither.
    assert closed["entry_filled_at_ns"] == (NOW + 20_000) * 1_000_000
    assert closed["position_closed_at_ns"] == (NOW + 60_000) * 1_000_000
    assert closed["entry_filled_at_ns"] < closed["position_closed_at_ns"]
    assert closed["order_reject_reason"] is None
    assert refused["stage"] == "rejected"
    assert refused["disposition_reason"] == "entries_paused"
    assert refused["realized_pnl_usd"] is None
    assert refused["entry_filled_at_ns"] is None and refused["position_closed_at_ns"] is None

    # #604 T3 (audit A4). No disposition, no order, and a TTL the clock has passed: the desk read that
    # hole as `pending` forever, because the bridge stops offering a Signal the instant it expires.
    assert stale["stage"] == "expired"
    assert stale["disposition_reason"] is None

    # #604 T1. The venue's own refusal text, verbatim, on the entry order it refused.
    assert rejected_order["stage"] == "ordered"
    assert rejected_order["order_reject_reason"] == "Order's notional must be no smaller than 5.0"

    # A manual entry is its own row, keyed on the Command that opened it and holding no Case.
    assert manual["source"] == "manual"
    assert manual["entry_id"] == "e" * 64
    assert manual["case_id"] is None
    assert manual["stage"] == "closed"
    assert manual["exit_reason"] == "flatten"
    assert manual["realized_pnl_usd"] == "-1.11984726"
    assert manual["position_closed_at_ns"] == (NOW + 90_000) * 1_000_000

    # #537 PR-5. `stage` is the one word the table renders; the venue's own `order_status` and
    # `position_status` are what it is derived from, and the `accepted` / `rejected` split beside it
    # said what `ordered` and `rejected` already say. `last_observed_at_ns` was a second clock, and
    # the Signal's own `expires_at_ns` is another input to `stage` rather than a column (#604 T3).
    assert {
        "disposition",
        "order_status",
        "position_status",
        "last_observed_at_ns",
        "expires_at_ns",
    }.isdisjoint(closed)

    assert set(data) == {"executions", "totals", "complete"}
    assert all(name != "console_operator_intents" for name, _ in trading.calls)
    assert data["complete"] is True
    executions_call = next(kwargs for name, kwargs in trading.calls if name == "console_executions")
    assert executions_call["limit"] == 101


def test_executions_publishes_the_realized_totals_the_window_cannot_add_up(
    client: tuple[TestClient, _Trading],
) -> None:
    """#604 T3. Two sums and two counts over every `closed` position the slot has, manual ones too.

    The desk could only add up the realized column of the rows it was showing, which is 24 h of at
    most 100 entries -- so the one number an operator reconciles against the venue was the one number
    the console could not produce. The totals read is slot-scoped and day-floored by the server, so
    "today" is the same day for the sums, the counts and the rows beside them.
    """

    api, trading = client
    data = api.get("/api/trading/executions", params={"token": TOKEN}).json()["data"]

    assert data["totals"] == {
        "realized_known_today_usd": "-9.53",
        "realized_known_total_usd": "56.40",
        "closed_today": 1,
        "closed_total": 12,
        "pnl_known_today": 1,
        "pnl_known_total": 11,
        "pnl_missing_today": 0,
        "pnl_missing_total": 1,
        "pnl_complete_today": True,
        "pnl_complete_total": False,
    }
    totals_call = next(kwargs for name, kwargs in trading.calls if name == "console_realized_totals")
    assert totals_call["account_slot"] == "binance_usdm_primary"
    assert totals_call["day_start_ns"] % (86_400_000 * 1_000_000) == 0
    assert totals_call["day_end_ns"] - totals_call["day_start_ns"] == 86_400_000 * 1_000_000
    assert totals_call["day_start_ns"] <= int(time.time() * 1000) * 1_000_000


def test_retired_manual_command_cannot_record_an_intent(client, monkeypatch):
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
    assert response.status_code == 404
    assert trading.persisted == []
