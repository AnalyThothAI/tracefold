"""Historical CLI orchestration: actual SDK decoding, real PG, read-only preview and explicit append."""

from __future__ import annotations

import json
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import msgspec
import pytest
from fastapi.testclient import TestClient
from nautilus_trader.adapters.binance import BinanceAccountType
from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.adapters.binance.http.error import BinanceClientError
from nautilus_trader.common.component import LiveClock

from tests.helpers.published_signal_v3 import append_published_v3_signal
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.app.nautilus import history
from tracefold.platform.config.models import Settings
from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.storage.execution_stream import prepare_execution_observations
from tracefold.trading.storage.root import TradingRepository
from tracefold.trading.storage.trade_plans import prepare_trade_plan
from tracefold.trading.trade_plan import TradePlan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


@pytest.fixture
def recorded_history(tmp_path, monkeypatch):
    receipt = json.loads((Path(__file__).parents[1] / "fixtures/binance/inj_20260925_execution.json").read_text())
    plan = TradePlan.model_validate_json(json.dumps(receipt["original_plan"]))
    with closing(connect_postgres_test(read_only=False)) as conn:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(plan.account_slot, now_ns=plan.created_at_ns)
            conn.execute(
                "UPDATE trading_execution_runtime_control_state SET execution_namespace=%s WHERE account_slot=%s",
                (receipt["execution_namespace"], plan.account_slot),
            )
        append_published_v3_signal(
            repo,
            signal_id=plan.entry_id,
            case_id=plan.case_id,
            observed_at_ns=plan.created_at_ns - 1,
            expires_at_ns=plan.entry_expires_at_ns,
        )
        with conn.transaction():
            repo.insert_trade_plan(prepare_trade_plan(plan))
            repo.append_execution_observations(
                prepare_execution_observations(
                    (ExecutionObservationV1.model_validate(receipt["original_entry_observation"]),)
                )
            )

    requests = []

    async def transport(_self, _method, url_path, payload=None, **_kwargs):
        requests.append((url_path, payload))
        with closing(connect_postgres_test(read_only=True)) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity "
                    "WHERE application_name IN ('tracefold_history_preview','tracefold_history_apply')"
                ).fetchone()["n"]
                == 0
            )
        if url_path.endswith("/algoOrder"):
            if payload.get("clientAlgoId") == receipt["algo"]["clientAlgoId"]:
                return msgspec.json.encode(receipt["algo"])
            raise BinanceClientError(status=400, message={"code": -2013, "msg": "Order does not exist."}, headers={})
        if url_path.endswith("/order"):
            for order in receipt["orders"]:
                if (
                    str(payload.get("orderId")) == str(order["orderId"])
                    or payload.get("origClientOrderId") == order["clientOrderId"]
                ):
                    return msgspec.json.encode(order)
            raise AssertionError(payload)
        if url_path.endswith("/userTrades"):
            return msgspec.json.encode([t for t in receipt["trades"] if str(t["orderId"]) == str(payload["orderId"])])
        raise AssertionError(f"Unexpected venue endpoint: {url_path}")

    monkeypatch.setattr(BinanceHttpClient, "send_request", transport)

    def account(_settings, environment):
        clock = LiveClock()
        return BinanceFuturesAccountHttpAPI(
            client=get_cached_binance_http_client(
                clock=clock,
                account_type=BinanceAccountType.USDT_FUTURES,
                environment=environment,
                api_key="historical-fixture-key",
                api_secret="historical-fixture-secret",
            ),
            clock=clock,
            account_type=BinanceAccountType.USDT_FUTURES,
        )

    monkeypatch.setattr(history, "_account", account)
    settings = Settings(
        ws_token="historical-execution-test-token",
        storage=postgres_settings_storage(),
        trading={"execution": {"binance": {"environment": "DEMO"}}},
    )
    settings.set_config_dir(tmp_path / "app-home")
    return settings, plan, receipt, requests


def _snapshot(plan):
    with closing(connect_postgres_test(read_only=True)) as conn:
        return {
            "plan": TradingRepository(conn).trade_plan(plan.entry_id),
            "control": conn.execute(
                "SELECT * FROM trading_execution_runtime_control_state WHERE account_slot=%s", (plan.account_slot,)
            ).fetchone(),
            "observations": conn.execute(
                "SELECT seq,payload FROM trading_execution_observations ORDER BY seq"
            ).fetchall(),
        }


def test_recorded_inj_preview_is_select_only_and_explicit_apply_is_idempotent(recorded_history):
    settings, plan, receipt, requests = recorded_history
    original = _snapshot(plan)
    args = dict(entry_id=plan.entry_id, account_slot=plan.account_slot, environment="DEMO")
    preview = history.verify_execution_history(settings, **args)
    assert preview["mode"] == "preview"
    assert _snapshot(plan) == original
    assert len(requests) <= 8
    assert Decimal(preview["projected"]["realized_pnl_usd"]) == Decimal("19.087768")
    assert preview["projected"]["position_closed_at_ns"] == 1790338365075_000000
    assert preview["projected"]["exit_reason"] == "take_profit"
    assert preview["observation_count"] == 8
    assert preview["impact"] == {
        "history_only": True,
        "plan_mutated": False,
        "cache_applied": False,
        "control_mutated": False,
        "venue_orders_sent": 0,
    }
    applied = history.verify_execution_history(settings, **args, apply=True)
    snapshot = _snapshot(plan)
    assert applied["projected"]["exit_reason"] == "take_profit"
    assert snapshot["plan"] == original["plan"] and snapshot["control"] == original["control"]
    assert snapshot["observations"][0] == original["observations"][0]
    assert len(snapshot["observations"]) == 9
    replay = history.verify_execution_history(settings, **args, apply=True)
    assert replay["projected"] == applied["projected"]
    assert _snapshot(plan) == snapshot
    with TestClient(create_app(settings=settings)) as client:
        response = client.get(
            "/api/trading/executions",
            params={"case_id": plan.case_id},
            headers={"Authorization": f"Bearer {settings.ws_token}"},
        )
    assert response.status_code == 200
    [row] = response.json()["data"]["executions"]
    assert row["exit_reason"] == "take_profit" and row["original_exit_reason"] == "venue_unknown"
    assert row["original_terminal_at_ns"] == plan.terminal_at_ns
    assert row["result_evidence_source"] == "signed_native_trades"
    assert Decimal(row["realized_pnl_usd"]) == Decimal("19.087768")
    assert row["funding_usd"] is None and row["net_pnl_usd"] is None
    # A changed receipt cannot overwrite a prior signed economic fact, even with --apply.
    receipt["trades"][1]["price"] = "8.4"
    with pytest.raises(ValueError, match="execution_evidence_preview_conflict"):
        history.verify_execution_history(settings, **args, apply=True)
    assert _snapshot(plan) == snapshot


@pytest.mark.parametrize("changes", [{"account_slot": "another"}, {"environment": "LIVE"}])
def test_history_scope_mismatch_does_not_read_the_venue_or_write_anything(recorded_history, changes):
    settings, plan, _, requests = recorded_history
    original = _snapshot(plan)
    args = dict(entry_id=plan.entry_id, account_slot=plan.account_slot, environment="DEMO") | changes
    with pytest.raises(ValueError, match="historical_execution_scope_mismatch"):
        history.verify_execution_history(settings, **args, apply=True)
    assert not requests
    assert _snapshot(plan) == original
