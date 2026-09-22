"""The Trading watchdog against the real ledger: its reads, its alert ledger and one whole pass (#680 PR-2)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.workers.runtime import CapabilityStates
from tracefold.app.workers.watchdog_storage import WatchdogAlertRepository, WatchdogAlertState
from tracefold.app.workers.wiring import watchdog as wd
from tracefold.news import OI_METRIC_VERSION, ReaderCard
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


def _oi_frame(conn: Any, event_id: str, *, at_ms: int, historical: bool = False) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.upsert_item(
            item_id=f"item-{event_id}",
            source_id="opennews",
            source_item_key=f"item-{event_id}",
            title="SOL\tOI Rise 7.20%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
            raw_first_line="",
            description="",
            canonical_url=None,
            reporting_origin="OpenNews",
            published_at_ms=at_ms,
            observed_at_ms=at_ms,
            provider_metadata_json='{"source": "binance"}',
            strategy_ids_json='["1019"]',
            ingest_mode="live",
            trace_id="trace",
            now_ms=at_ms,
            market_kind="oi",
            market_source_strategy_id="1019",
            market_parse_status="parsed",
            market_parse_error=None,
        )
        repos.news.insert_oi_signal(
            event_id=event_id,
            metric_version=OI_METRIC_VERSION,
            symbol="SOL",
            raw_instrument="SOL",
            direction="rise",
            oi_change_bps=720,
            oi_value_usd=32_170_000,
            whale_long_profit_bps=8_021,
            whale_oi_ratio_bps=10_071,
            observed_at_ms=at_ms,
            received_at_ms=at_ms,
            now_ms=at_ms,
            provider="opennews",
            source_strategy_id="1019",
            source_contract_version="opennews_oi_source_v1",
            measurement_window_ms=300_000,
            measurement_definition="oi_signal_v1|opennews_oi_source_v1|300000",
            source_item_id=f"item-{event_id}",
            source_venue="binance",
        )
    if historical:
        conn.execute("UPDATE news_oi_signals SET historical = true WHERE event_id = %s", (event_id,))
        conn.commit()


def _answer(conn: Any, event_id: str) -> None:
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.trading.record_gate_decision(
            source_key=f"oi:{event_id}:{OI_METRIC_VERSION}",
            trigger_kind="oi",
            underlying_key="crypto:SOL",
            source_observed_at_ms=NOW_MS - 30 * 60_000,
            status="EXPIRED",
            stage="eligibility",
            reason="trigger_stale",
            retryable=False,
            evidence={},
            case_id=None,
            now_ms=NOW_MS - 20 * 60_000,
        )


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


def test_gate_answers_names_only_the_sources_that_have_one(conn: Any) -> None:
    _answer(conn, "answered")
    trading = repositories_for_connection(conn).trading

    answers = trading.gate_answers(
        source_keys=[f"oi:answered:{OI_METRIC_VERSION}", f"oi:never:{OI_METRIC_VERSION}", "oi:answered:oi_signal_v1"]
    )

    assert answers == {f"oi:answered:{OI_METRIC_VERSION}": "EXPIRED"}
    assert trading.gate_answers(source_keys=[]) == {}


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
    }
    assert trading.runtime_liveness(account_slot="another_slot") is None
    overdue = trading.overdue_open_plans(now_ns=NOW_NS, grace_ns=15 * MINUTE_NS, limit=10)
    assert [(plan["market_key"], plan["status"]) for plan in overdue] == [("crypto:perp:LATE:USDT", "open")]
    assert trading.recent_signal_dispositions(limit=2) == ["position_limit", "unexpected_exposure"]
    assert trading.recent_signal_dispositions(limit=10) == ["position_limit", "unexpected_exposure", "accepted"]


def test_the_alert_ledger_is_one_row_per_condition(conn: Any) -> None:
    ledger = WatchdogAlertRepository(conn)
    opened = WatchdogAlertState(wd.SIGNAL_LANE_FAULTED, active=True, opened_at_ms=NOW_MS)

    with conn.transaction():
        ledger.save(opened, detail="x" * 5_000, now_ms=NOW_MS)
    with conn.transaction():
        ledger.save(
            WatchdogAlertState(wd.SIGNAL_LANE_FAULTED, active=True, opened_at_ms=NOW_MS, notified_at_ms=NOW_MS + 1),
            detail="told",
            now_ms=NOW_MS + 1,
        )

    assert ledger.states() == {
        wd.SIGNAL_LANE_FAULTED: WatchdogAlertState(
            wd.SIGNAL_LANE_FAULTED, active=True, opened_at_ms=NOW_MS, notified_at_ms=NOW_MS + 1
        )
    }
    row = conn.execute("SELECT detail, updated_at_ms FROM platform_watchdog_alerts").fetchone()
    assert (row["detail"], row["updated_at_ms"]) == ("told", NOW_MS + 1)


def test_one_pass_over_the_real_ledger_alerts_the_unanswered_frame_and_records_it(conn: Any) -> None:
    """Frames, answers and alert state all come from PostgreSQL; only the provider is a fake."""

    _oi_frame(conn, "answered", at_ms=NOW_MS - 30 * 60_000)
    _answer(conn, "answered")
    _oi_frame(conn, "unanswered", at_ms=NOW_MS - 25 * 60_000)
    _oi_frame(conn, "too-fresh", at_ms=NOW_MS - 2 * 60_000)
    _oi_frame(conn, "too-old", at_ms=NOW_MS - 13 * 3_600_000)
    _oi_frame(conn, "reconstructed", at_ms=NOW_MS - 20 * 60_000, historical=True)
    sender = RecordingSender()
    watchdog = wd.TradingWatchdog(
        db=wd.WorkerWatchdogDatabase(
            ConnectionDatabase(conn),  # type: ignore[arg-type]
            oi_metric_version=OI_METRIC_VERSION,
            account_slot=SLOT,
        ),
        sender=sender,
        capabilities=CapabilityStates(),
        runtime_expected=False,
        account_slot=SLOT,
        clock=lambda: NOW_MS,
    )

    asyncio.run(watchdog.advance())
    asyncio.run(watchdog.advance())

    assert [card.header.subject for card in sender.cards] == ["Tracefold 告警 · OI 帧没有准入答复"]
    assert sender.cards[0].lead.startswith("1 个 live OI 帧入账超过 10 分钟仍没有准入记录")
    states = WatchdogAlertRepository(conn).states()
    assert set(states) == {wd.OI_FRAMES_UNANSWERED}
    assert states[wd.OI_FRAMES_UNANSWERED].notified_at_ms == NOW_MS
