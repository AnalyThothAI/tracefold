"""Manual Telegram trade state is idempotent and auditable at the PostgreSQL seam."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from psycopg.errors import CheckViolation, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    ManualAccountSnapshot,
    ManualRiskConfig,
    ManualSessionState,
    ManualTradeOutcome,
    ManualTradeOutcomeState,
    ManualTradeParameters,
    ManualTradeSource,
    ManualVenueInstrument,
    ModificationGuardState,
    StrategyPreset,
    TradeSide,
    build_manual_trade_preview,
    create_manual_trade_intent,
    guard_manual_trade_modification,
)
from tracefold.trading.manual_execution import build_manual_execution_plan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_900_000_000_000
SESSION_ID = "0198f3ae-76c0-77a1-a191-0d3f16842ea0"


@pytest.fixture(scope="module")
def conn() -> Any:
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    yield connection
    connection.close()


def _source() -> ManualTradeSource:
    return ManualTradeSource(
        news_event_id="event-manual-1",
        delivery_target_sha256="a" * 64,
        delivery_message_id=42,
        headline_zh="BTC ETF 净流入创纪录",
        base_symbol="BTC",
        side=TradeSide.LONG,
        source_observed_at_ms=NOW - 10_000,
    )


def _preview_values():
    account = ManualAccountSnapshot(
        account_ref="binance-manual-demo-1",
        venue="binance_usdm_demo",
        instrument_id="BTCUSDT",
        account_equity_usd=Decimal("1000"),
        reference_entry=Decimal("100"),
        observed_at_ms=NOW,
    )
    recommended = ManualTradeParameters(notional_usd=Decimal("10"), leverage=2, stop_loss_bps=100, take_profit_bps=200)
    selected = ManualTradeParameters(notional_usd=Decimal("10"), leverage=2, stop_loss_bps=100, take_profit_bps=200)
    guard = guard_manual_trade_modification(
        preset=StrategyPreset.TIGHT_STOP,
        account_equity=account.account_equity_usd,
        recommended=recommended,
        modified=selected,
        config=ManualRiskConfig(
            notional_deviation_limit_bps=5_000,
            tight_stop_deviation_limit_bps=5_000,
            wide_stop_deviation_limit_bps=10_000,
            max_account_risk_bps=1_000,
            high_risk_loss_multiple_bps=15_000,
            min_leverage=1,
            max_leverage=20,
        ),
    )
    preview = build_manual_trade_preview(
        side=TradeSide.LONG,
        venue="binance_usdm_demo",
        account_equity=account.account_equity_usd,
        reference_entry=account.reference_entry,
        parameters=selected,
    )
    return account, recommended, selected, preview, guard


def _reset(connection: Any) -> None:
    connection.execute(
        "TRUNCATE trading_manual_intents, trading_manual_events, trading_manual_sessions, "
        "trading_manual_account_snapshots, "
        "trading_manual_telegram_updates, trading_account_bindings CASCADE"
    )
    connection.execute("UPDATE trading_manual_runtime SET next_telegram_update_id = 0, updated_at_ms = 0 WHERE id = 1")
    connection.commit()


def _confirmed_intent(repos: Any):
    assert repos.trading.register_trading_account_binding(
        account_ref="binance-manual-demo-1",
        account_lane="manual",
        venue="binance_usdm_demo",
        credential_fingerprint="b" * 64,
        provider_account_fingerprint="c" * 64,
        now_ms=NOW,
    )
    repos.trading.begin_manual_trade_session(
        session_id=SESSION_ID,
        source=_source(),
        actor_user_id=123456789,
        chat_id=-1001234567890,
        update_id=101,
        now_ms=NOW,
    )
    account, recommended, selected, preview, guard = _preview_values()
    assert repos.trading.set_manual_trade_preview(
        session_id=SESSION_ID,
        preset=StrategyPreset.TIGHT_STOP,
        account_snapshot=account,
        recommended=recommended,
        selected=selected,
        preview=preview,
        guard=guard,
        update_id=102,
        result_code="preview_ready",
        now_ms=NOW + 1,
    )
    intent = create_manual_trade_intent(
        session_id=SESSION_ID,
        source=_source(),
        actor_user_id=123456789,
        account_ref=account.account_ref,
        venue=account.venue,
        preset=StrategyPreset.TIGHT_STOP,
        recommended=recommended,
        selected=selected,
        reference_entry=account.reference_entry,
        account_equity=account.account_equity_usd,
        guard=guard,
        confirmed_at_ms=NOW + 2,
    )
    assert repos.trading.confirm_manual_trade_intent(
        intent,
        update_id=103,
        result_code="intent_confirmed",
        now_ms=NOW + 2,
    )
    return intent


def test_update_cursor_and_callback_claim_are_durable_and_idempotent(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    update = TelegramTradingUpdate(
        update_id=101,
        callback_query_id="callback-101",
        actor_user_id=123456789,
        chat_id=-1001234567890,
        message_id=42,
        data="tf:trade:v1",
        authorized=True,
    )

    assert repos.trading.manual_next_telegram_update_id() == 0
    assert repos.trading.claim_manual_telegram_update(update, now_ms=NOW) is True
    assert repos.trading.claim_manual_telegram_update(update, now_ms=NOW + 1) is False
    assert repos.trading.manual_telegram_update_state(101) == "RECEIVED"
    assert repos.trading.manual_next_telegram_update_id() == 0
    assert repos.trading.settle_manual_telegram_update(101, result_code="session_created", now_ms=NOW + 2)
    assert repos.trading.manual_next_telegram_update_id() == 102
    assert not repos.trading.settle_manual_telegram_update(101, result_code="duplicate", now_ms=NOW + 3)
    conn.commit()


def test_deterministic_rejection_is_a_typed_terminal_outcome_and_notification(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    intent = _confirmed_intent(repos)

    assert repos.trading.reject_manual_order(
        intent.intent_id,
        leg="entry",
        error_code="binance_manual_provider_rejected",
        now_ms=NOW + 3,
    )
    session = repos.trading.manual_trade_session(SESSION_ID)
    assert session is not None and session.state is ManualSessionState.REJECTED
    row = conn.execute(
        "SELECT state, outcome FROM trading_manual_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert row is not None and row["state"] == "TERMINAL"
    outcome = ManualTradeOutcome.model_validate(row["outcome"])
    assert outcome.state is ManualTradeOutcomeState.REJECTED
    assert outcome.error_code == "binance_manual_provider_rejected"
    assert repos.trading.manual_trade_events(SESSION_ID)[-1].event_kind == "ORDER_REJECTED"
    notification = conn.execute(
        "SELECT notification_kind FROM trading_manual_notifications WHERE session_id = %s::uuid",
        (SESSION_ID,),
    ).fetchone()
    assert notification is not None and notification["notification_kind"] == "ORDER_REJECTED"
    claimed = repos.trading.begin_manual_notification(now_ms=NOW + 4)
    assert claimed is not None
    notification_id = claimed["notification_id"]
    assert claimed["interaction_state"] == "PENDING" and claimed["reply_state"] == "PENDING"
    assert repos.trading.begin_manual_notification_effect(
        notification_id,
        effect="interaction",
        now_ms=NOW + 5,
    )
    assert repos.trading.mark_manual_notification_interaction_ambiguous(
        notification_id,
        error_code="interaction_timeout",
        now_ms=NOW + 6,
    )
    assert repos.trading.begin_manual_notification_effect(
        notification_id,
        effect="reply",
        now_ms=NOW + 7,
    )
    assert repos.trading.settle_manual_notification(
        notification_id,
        provider_message_id=777,
        now_ms=NOW + 8,
    )
    effects = conn.execute(
        """
        SELECT state, interaction_state, reply_state
          FROM trading_manual_notifications WHERE notification_id = %s
        """,
        (notification_id,),
    ).fetchone()
    assert effects == {"state": "SENT", "interaction_state": "AMBIGUOUS", "reply_state": "SENT"}
    conn.commit()


def test_protection_rejection_persists_an_unresolved_exposure_terminal(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    intent = _confirmed_intent(repos)
    plan = build_manual_execution_plan(
        intent,
        ManualVenueInstrument(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )
    assert repos.trading.fence_manual_entry(intent.intent_id, plan=plan, now_ms=NOW + 3)
    assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg="execution_setting", now_ms=NOW + 4)
    assert repos.trading.record_manual_execution_setting(intent.intent_id, now_ms=NOW + 5)
    assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg="entry", now_ms=NOW + 6)
    assert repos.trading.record_manual_entry(
        intent.intent_id,
        receipt={"client_id": plan.entry_client_order_id, "provider_id": "1", "status": "FILLED"},
        now_ms=NOW + 7,
    )
    assert repos.trading.fence_manual_protection(
        intent.intent_id,
        leg="stop_loss",
        client_id=plan.stop_loss_client_order_id,
        now_ms=NOW + 8,
    )
    assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg="stop_loss", now_ms=NOW + 9)
    assert repos.trading.mark_manual_position_exposed(
        intent.intent_id,
        leg="stop_loss",
        error_code="binance_manual_provider_rejected",
        now_ms=NOW + 10,
    )

    session = repos.trading.manual_trade_session(SESSION_ID)
    assert session is not None and session.state is ManualSessionState.EXPOSED
    row = conn.execute(
        "SELECT state, outcome FROM trading_manual_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert row is not None and row["state"] == "EXPOSED"
    assert ManualTradeOutcome.model_validate(row["outcome"]).state is ManualTradeOutcomeState.EXPOSED
    assert repos.trading.manual_trade_events(SESSION_ID)[-1].event_kind == "PROTECTION_REJECTED"
    conn.commit()


def test_session_preview_confirmation_and_event_log_are_one_atomic_story(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    assert repos.trading.register_trading_account_binding(
        account_ref="binance-manual-demo-1",
        account_lane="manual",
        venue="binance_usdm_demo",
        credential_fingerprint="b" * 64,
        provider_account_fingerprint="c" * 64,
        now_ms=NOW,
    )
    assert repos.trading.upsert_manual_account_snapshot(
        account_ref="binance-manual-demo-1",
        venue="binance_usdm_demo",
        equity_usd=Decimal("1000"),
        observed_at_ms=NOW,
        now_ms=NOW,
    )
    snapshot = repos.trading.manual_account_snapshot("binance-manual-demo-1")
    assert snapshot is not None and snapshot["equity_usd"] == Decimal("1000")
    session, created = repos.trading.begin_manual_trade_session(
        session_id=SESSION_ID,
        source=_source(),
        actor_user_id=123456789,
        chat_id=-1001234567890,
        update_id=101,
        now_ms=NOW,
    )
    duplicate, duplicate_created = repos.trading.begin_manual_trade_session(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea1",
        source=_source(),
        actor_user_id=123456789,
        chat_id=-1001234567890,
        update_id=102,
        now_ms=NOW + 1,
    )
    assert created is True and duplicate_created is False
    assert duplicate.session_id == session.session_id
    assert session.state is ManualSessionState.AWAITING_STRATEGY

    assert repos.trading.attach_manual_interaction_message(SESSION_ID, message_id=99, now_ms=NOW + 2)
    account, recommended, selected, preview, guard = _preview_values()
    assert guard.state is ModificationGuardState.ACCEPTED
    session = repos.trading.set_manual_trade_preview(
        session_id=SESSION_ID,
        preset=StrategyPreset.TIGHT_STOP,
        account_snapshot=account,
        recommended=recommended,
        selected=selected,
        preview=preview,
        guard=guard,
        update_id=103,
        result_code="preview_ready",
        now_ms=NOW + 3,
    )
    assert session is not None and session.state is ManualSessionState.PREVIEW

    intent = create_manual_trade_intent(
        session_id=SESSION_ID,
        source=_source(),
        actor_user_id=123456789,
        account_ref=account.account_ref,
        venue=account.venue,
        preset=StrategyPreset.TIGHT_STOP,
        recommended=recommended,
        selected=selected,
        reference_entry=account.reference_entry,
        account_equity=account.account_equity_usd,
        guard=guard,
        confirmed_at_ms=NOW + 4,
    )
    assert (
        repos.trading.confirm_manual_trade_intent(
            intent,
            update_id=104,
            result_code="intent_confirmed",
            now_ms=NOW + 4,
        )
        is True
    )
    assert (
        repos.trading.confirm_manual_trade_intent(
            intent,
            update_id=104,
            result_code="intent_confirmed",
            now_ms=NOW + 5,
        )
        is True
    )
    assert (
        repos.trading.confirm_manual_trade_intent(
            intent,
            update_id=105,
            result_code="intent_confirmed",
            now_ms=NOW + 5,
        )
        is False
    )
    final = repos.trading.manual_trade_session(SESSION_ID)
    assert final is not None
    assert final.state is ManualSessionState.CONFIRMED
    assert final.intent_id == intent.intent_id
    assert [row.event_kind for row in repos.trading.manual_trade_events(SESSION_ID)] == [
        "SESSION_CREATED",
        "STRATEGY_SELECTED",
        "TRADE_CONFIRMED",
    ]
    plan = build_manual_execution_plan(
        intent,
        ManualVenueInstrument(
            symbol="BTCUSDT",
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("5"),
        ),
    )
    assert repos.trading.fence_manual_entry(intent.intent_id, plan=plan, now_ms=NOW + 6)
    assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg="execution_setting", now_ms=NOW + 7)
    assert not repos.trading.begin_manual_order_attempt(intent.intent_id, leg="execution_setting", now_ms=NOW + 8)
    assert repos.trading.record_manual_execution_setting(intent.intent_id, now_ms=NOW + 8)
    assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg="entry", now_ms=NOW + 9)
    assert not repos.trading.begin_manual_order_attempt(intent.intent_id, leg="entry", now_ms=NOW + 10)
    assert repos.trading.mark_manual_order_ambiguous(
        intent.intent_id,
        leg="entry",
        error_code="provider_timeout",
        now_ms=NOW + 11,
    )
    ambiguous = conn.execute(
        "SELECT outcome FROM trading_manual_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert ambiguous is not None
    assert ManualTradeOutcome.model_validate(ambiguous["outcome"]).state is ManualTradeOutcomeState.AMBIGUOUS
    assert repos.trading.record_manual_entry(
        intent.intent_id,
        receipt={"client_id": plan.entry_client_order_id, "provider_id": "1", "status": "FILLED"},
        now_ms=NOW + 12,
    )
    assert repos.trading.fence_manual_protection(
        intent.intent_id,
        leg="take_profit",
        client_id=plan.take_profit_client_order_id,
        now_ms=NOW + 13,
    )
    assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg="take_profit", now_ms=NOW + 14)
    assert repos.trading.record_manual_protection(
        intent.intent_id,
        leg="take_profit",
        receipt={"client_id": plan.take_profit_client_order_id, "provider_id": "2", "status": "NEW"},
        now_ms=NOW + 15,
    )
    assert repos.trading.fence_manual_protection(
        intent.intent_id,
        leg="stop_loss",
        client_id=plan.stop_loss_client_order_id,
        now_ms=NOW + 16,
    )
    assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg="stop_loss", now_ms=NOW + 17)
    assert repos.trading.record_manual_protection(
        intent.intent_id,
        leg="stop_loss",
        receipt={"client_id": plan.stop_loss_client_order_id, "provider_id": "3", "status": "NEW"},
        now_ms=NOW + 18,
    )
    opened = repos.trading.manual_trade_session(SESSION_ID)
    assert opened is not None and opened.state is ManualSessionState.OPEN
    opened_outcome = conn.execute(
        "SELECT outcome FROM trading_manual_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert opened_outcome is not None
    assert ManualTradeOutcome.model_validate(opened_outcome["outcome"]).state is ManualTradeOutcomeState.OPEN
    notification_kinds = []
    for offset in range(4):
        notification = repos.trading.begin_manual_notification(now_ms=NOW + 30 + offset)
        assert notification is not None
        notification_kinds.append(notification["notification_kind"])
    assert notification_kinds == ["ORDER_AMBIGUOUS", "POSITION_OPENED", "TP_CREATED", "SL_CREATED"]
    conn.commit()

    with pytest.raises(CheckViolation, match="trading_manual_intent_outcome_check"):
        conn.execute(
            """
            UPDATE trading_manual_intents
               SET outcome = jsonb_set(outcome, '{state}', '"rejected"'::jsonb)
             WHERE intent_id = %s
            """,
            (intent.intent_id,),
        )
    conn.rollback()

    failure_outcome = {
        "outcome_version": "manual_trade_outcome_v1",
        "state": "ambiguous",
        "leg": "entry",
        "error_code": "unknown_result",
        "entry": None,
        "take_profit": None,
        "stop_loss": None,
    }
    invalid_outcomes = (
        {**failure_outcome, "leg": None},
        {**failure_outcome, "error_code": None},
        {**failure_outcome, "extra": True},
        {**opened_outcome["outcome"], "entry": {}},
        {**opened_outcome["outcome"], "entry": {**opened_outcome["outcome"]["entry"], "extra": True}},
    )
    for invalid_outcome in invalid_outcomes:
        with pytest.raises(CheckViolation, match="trading_manual_intent_outcome_check"):
            conn.execute(
                "UPDATE trading_manual_intents SET outcome = %s::jsonb WHERE intent_id = %s",
                (json.dumps(invalid_outcome), intent.intent_id),
            )
        conn.rollback()

    exposed_outcome = {**failure_outcome, "state": "exposed", "leg": "stop_loss"}
    with pytest.raises(CheckViolation, match="trading_manual_intent_receipt_check"):
        conn.execute(
            """
            UPDATE trading_manual_intents
               SET state = 'EXPOSED', outcome = %s::jsonb,
                   entry_submitted_at_ms = NULL, entry_receipt = NULL
             WHERE intent_id = %s
            """,
            (json.dumps(exposed_outcome), intent.intent_id),
        )
    conn.rollback()

    with pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"):
        conn.execute("UPDATE trading_manual_events SET event_kind = 'TRADE_CANCELLED'")
    conn.rollback()

    with pytest.raises(RaiseException, match="trading_manual_intent_identity_mutation_forbidden"):
        conn.execute(
            "UPDATE trading_manual_intents SET payload = payload || '{\"tampered\": true}'::jsonb WHERE intent_id = %s",
            (intent.intent_id,),
        )
    conn.rollback()


def test_database_rejects_manual_and_auto_binding_to_the_same_credential_fingerprint(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    assert repos.trading.register_trading_account_binding(
        account_ref="binance-auto-demo-1",
        account_lane="auto",
        venue="binance_usdm_demo",
        credential_fingerprint="c" * 64,
        provider_account_fingerprint="d" * 64,
        now_ms=NOW,
    )
    with pytest.raises(ValueError, match="trading_account_binding_isolation_conflict"):
        repos.trading.register_trading_account_binding(
            account_ref="binance-manual-demo-1",
            account_lane="manual",
            venue="binance_usdm_demo",
            credential_fingerprint="c" * 64,
            provider_account_fingerprint="e" * 64,
            now_ms=NOW + 1,
        )
    conn.rollback()


def test_database_rejects_distinct_keys_bound_to_the_same_provider_account(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    assert repos.trading.register_trading_account_binding(
        account_ref="binance-auto-demo-1",
        account_lane="auto",
        venue="binance_usdm_demo",
        credential_fingerprint="c" * 64,
        provider_account_fingerprint="f" * 64,
        now_ms=NOW,
    )
    with pytest.raises(ValueError, match="trading_account_binding_isolation_conflict"):
        repos.trading.register_trading_account_binding(
            account_ref="binance-manual-demo-1",
            account_lane="manual",
            venue="binance_usdm_demo",
            credential_fingerprint="d" * 64,
            provider_account_fingerprint="f" * 64,
            now_ms=NOW + 1,
        )
    conn.rollback()


def test_runtime_roles_cannot_rewrite_manual_intent_or_source_identity(conn: Any) -> None:
    checks = {
        (row["role_name"], row["table_name"], row["column_name"]): bool(row["can_update"])
        for row in conn.execute(
            """
            SELECT role_name, table_name, column_name,
                   has_column_privilege(role_name, table_name, column_name, 'UPDATE') AS can_update
              FROM (VALUES
                ('tracefold_workers', 'trading_manual_intents', 'payload'),
                ('tracefold_workers', 'trading_manual_intents', 'state'),
                ('tracefold_nautilus', 'trading_manual_intents', 'payload'),
                ('tracefold_nautilus', 'trading_manual_intents', 'state'),
                ('tracefold_workers', 'trading_manual_sessions', 'source'),
                ('tracefold_workers', 'trading_manual_sessions', 'state')
              ) AS expected(role_name, table_name, column_name)
            """
        ).fetchall()
    }

    assert checks == {
        ("tracefold_workers", "trading_manual_intents", "payload"): False,
        ("tracefold_workers", "trading_manual_intents", "state"): False,
        ("tracefold_nautilus", "trading_manual_intents", "payload"): False,
        ("tracefold_nautilus", "trading_manual_intents", "state"): True,
        ("tracefold_workers", "trading_manual_sessions", "source"): False,
        ("tracefold_workers", "trading_manual_sessions", "state"): True,
    }
