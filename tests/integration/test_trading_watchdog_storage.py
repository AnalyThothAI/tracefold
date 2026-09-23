"""The Trading watchdog against the real ledger: its reads, its alert ledger and one whole pass (#680 PR-2)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.watchdog_storage import WatchdogAlertRepository, WatchdogAlertState
from tracefold.app.workers.wiring import watchdog as wd
from tracefold.news import ReaderCard
from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.storage.execution_stream import (
    ExecutionRuntimeState,
    prepare_execution_observations,
    prepare_trade_signal,
)
from tracefold.trading.storage.trade_plans import prepare_trade_plan
from tracefold.trading.trade_plan import TradePlan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

SLOT = "binance_usdm_primary"
NOW_MS = 1_790_000_000_000
NOW_NS = NOW_MS * 1_000_000
MINUTE_NS = 60_000_000_000


@pytest.fixture
def conn() -> Iterator[Any]:
    connection = connect_postgres_test(read_only=False)
    try:
        yield connection
    finally:
        connection.close()


class ConnectionDatabase:
    """The two `WorkerDatabase` calls the watchdog adapter makes, over one isolated connection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    async def run_business(
        self, _name: str, function: Callable[..., Any], *args: Any, operation_timeout_seconds: float
    ) -> Any:
        del operation_timeout_seconds
        return function(*args)

    @contextmanager
    def worker_session(self, _name: str, *_args: Any) -> Iterator[Any]:
        repos = repositories_for_connection(self.conn)
        with repos.transaction():
            yield repos


class RecordingSender:
    def __init__(self) -> None:
        self.cards: list[ReaderCard] = []

    @property
    def available(self) -> bool:
        return True

    async def send_prepared_card(
        self, card: ReaderCard, *, channel_payload: Mapping[str, Any], operation: str
    ) -> Mapping[str, Any]:
        del channel_payload, operation
        self.cards.append(card)
        return {"provider": "feishu", "code": 0}


def _runtime(conn: Any, *, heartbeat_at_ns: int, started_at_ns: int) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.trading.put_execution_runtime_state(
            ExecutionRuntimeState(
                account_slot=SLOT,
                mode="paper",
                runtime_id=UUID("33333333-3333-4333-8333-333333333333"),
                alive=True,
                entries_armed=True,
                unexpected_exposure=False,
                positions_count=0,
                open_orders_count=0,
                protection_status="not_applicable",
                heartbeat_at_ns=heartbeat_at_ns,
                entry_block_reason=None,
                started_at_ns=started_at_ns,
                updated_at_ns=heartbeat_at_ns,
            )
        )


def _plan(conn: Any, suffix: str, *, opened_ago_ns: int | None, closed: bool = False) -> None:
    created = NOW_NS - (opened_ago_ns or MINUTE_NS) - MINUTE_NS
    plan = TradePlan(
        entry_id=sha256(suffix.encode()).hexdigest(),
        entry_scope_id=f"legacy:{sha256(suffix.encode()).hexdigest()}",
        source="manual",
        account_slot=SLOT,
        runtime_mode_at_creation="paper",
        market_key=f"crypto:perp:{suffix.upper()}:USDT",
        instrument_id=f"{suffix.upper()}USDT-PERP.BINANCE",
        direction="long",
        entry_client_order_id="tf" + sha256(suffix.encode()).hexdigest()[:30],
        created_at_ns=created,
        entry_expires_at_ns=created + MINUTE_NS,
        entry_quantity=Decimal("1"),
        stop_distance_bps=100,
        risk_budget_usd=Decimal("10"),
        max_leverage_at_creation=1,
        take_profit_bps=200,
        max_holding_ns=240 * MINUTE_NS,
        status="closed" if closed else ("open" if opened_ago_ns is not None else "prepared"),
        opened_at_ns=None if opened_ago_ns is None else NOW_NS - opened_ago_ns,
        terminal_at_ns=NOW_NS - MINUTE_NS if closed else None,
        exit_reason="time_exit" if closed else None,
        updated_at_ns=NOW_NS - MINUTE_NS,
    )
    repos = repositories_for_connection(conn)
    with repos.transaction():
        assert repos.trading.insert_trade_plan(prepare_trade_plan(plan))


def _dispositions(conn: Any, *reasons: str) -> None:
    """One Signal and its `signal_disposition` per reason, oldest first."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.trading.ensure_execution_runtime_control_state(SLOT, now_ns=NOW_NS)
    for index, reason in enumerate(reasons):
        signal_id = sha256(f"signal-{index}".encode()).hexdigest()
        case_id = f"watchdog-case-{index}"
        at_ns = NOW_NS - (len(reasons) - index) * MINUTE_NS
        with repos.transaction():
            conn.execute(
                """
                INSERT INTO trading_cases (
                  case_id, underlying_key, trigger_kind, primary_source_key, manifest, manifest_sha256, state,
                  policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms, updated_at_ms
                ) VALUES (%s, %s, 'oi', %s, '{"test":"watchdog"}'::jsonb, %s, 'SIGNAL_EMITTED', 'long',
                          'watchdog', 1, 1, 1, 1)
                """,
                (case_id, f"crypto:W{index}", f"watchdog-source-{index}", "4" * 64),
            )
            repos.trading.append_trade_signal(
                prepare_trade_signal(
                    signal_id=signal_id,
                    case_id=case_id,
                    market_key="crypto:perp:BTC:USDT",
                    direction="long",
                    observed_at_ns=at_ns,
                    expires_at_ns=at_ns + MINUTE_NS,
                )
            )
            repos.trading.append_execution_observations(
                prepare_execution_observations(
                    (
                        ExecutionObservationV1.model_validate(
                            {
                                "event_id": sha256(f"disposition-{index}".encode()).hexdigest(),
                                "account_slot": SLOT,
                                "execution_strategy": "oi_nautilus_v1",
                                "signal_id": signal_id,
                                "normalized_kind": "signal_disposition",
                                "occurred_at_ns": at_ns + 1,
                                "observed_at_ns": at_ns + 1,
                                "native_identity_references": (),
                                "summary": {"disposition": reason},
                            }
                        ),
                    )
                )
            )


def test_the_three_execution_facts_read_only_what_they_name(conn: Any) -> None:
    trading = repositories_for_connection(conn).trading
    assert trading.runtime_liveness(account_slot=SLOT) is None
    _runtime(conn, heartbeat_at_ns=NOW_NS - 90 * 1_000_000_000, started_at_ns=NOW_NS - 60 * MINUTE_NS)
    _plan(conn, "late", opened_ago_ns=260 * MINUTE_NS)  # 4 h holding + 20 min: past the 15 min grace
    _plan(conn, "inside", opened_ago_ns=250 * MINUTE_NS)  # past 4 h, inside the grace
    _plan(conn, "done", opened_ago_ns=600 * MINUTE_NS, closed=True)
    _plan(conn, "never", opened_ago_ns=None)
    _dispositions(conn, "accepted", "unexpected_exposure", "position_limit")

    assert trading.runtime_liveness(account_slot=SLOT) == {
        "heartbeat_at_ns": NOW_NS - 90 * 1_000_000_000,
        "started_at_ns": NOW_NS - 60 * MINUTE_NS,
        "unexpected_exposure": False,
        "positions_count": 0,
        "protection_status": "not_applicable",
    }
    assert trading.runtime_liveness(account_slot="another_slot") is None
    overdue = trading.overdue_open_plans(now_ns=NOW_NS, grace_ns=15 * MINUTE_NS, limit=10)
    assert [(plan["market_key"], plan["status"]) for plan in overdue] == [("crypto:perp:LATE:USDT", "open")]
    assert trading.recent_signal_dispositions(limit=2) == ["position_limit", "unexpected_exposure"]
    assert trading.recent_signal_dispositions(limit=10) == ["position_limit", "unexpected_exposure", "accepted"]


def test_the_alert_ledger_is_one_row_per_condition(conn: Any) -> None:
    ledger = WatchdogAlertRepository(conn)
    opened = WatchdogAlertState(wd.RUNTIME_HEARTBEAT_STALE, active=True, opened_at_ms=NOW_MS)

    with conn.transaction():
        ledger.save(opened, detail="x" * 5_000, now_ms=NOW_MS)
    with conn.transaction():
        ledger.save(
            WatchdogAlertState(wd.RUNTIME_HEARTBEAT_STALE, active=True, opened_at_ms=NOW_MS, notified_at_ms=NOW_MS + 1),
            detail="told",
            now_ms=NOW_MS + 1,
        )

    assert ledger.states() == {
        wd.RUNTIME_HEARTBEAT_STALE: WatchdogAlertState(
            wd.RUNTIME_HEARTBEAT_STALE, active=True, opened_at_ms=NOW_MS, notified_at_ms=NOW_MS + 1
        )
    }
    row = conn.execute("SELECT detail, updated_at_ms FROM platform_watchdog_alerts").fetchone()
    assert (row["detail"], row["updated_at_ms"]) == ("told", NOW_MS + 1)
