"""Manual Telegram trade state is idempotent and auditable at the PostgreSQL seam."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from psycopg.errors import CheckViolation, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    ManualAccountSnapshot,
    ManualRiskConfig,
    ManualSessionState,
    ManualTargetPickerState,
    ManualTradeOutcome,
    ManualTradeOutcomeState,
    ManualTradeParameters,
    ManualTradeSource,
    ManualVenueInstrument,
    ManualVenuePosition,
    ModificationGuardState,
    StrategyPreset,
    TradeSide,
    build_manual_trade_preview,
    create_manual_trade_intent,
    guard_manual_trade_modification,
)
from tracefold.trading.manual_execution import build_manual_execution_plan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_module_clone_dsn")]

NOW = 1_900_000_000_000
SESSION_ID = "0198f3ae-76c0-77a1-a191-0d3f16842ea0"


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str) -> Any:
    connection = connect_postgres_test(read_only=False)
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
        account_ref="binance-manual-live-1",
        venue="binance_usdm_live",
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
        venue="binance_usdm_live",
        account_equity=account.account_equity_usd,
        reference_entry=account.reference_entry,
        parameters=selected,
    )
    return account, recommended, selected, preview, guard


def _reset(connection: Any) -> None:
    connection.execute(
        "TRUNCATE trading_manual_intents, trading_manual_events, trading_manual_sessions, "
        "trading_manual_target_pickers, "
        "trading_manual_account_snapshots, "
        "trading_manual_telegram_updates, trading_account_bindings CASCADE"
    )
    connection.execute("UPDATE trading_manual_runtime SET next_telegram_update_id = 0, updated_at_ms = 0 WHERE id = 1")
    connection.commit()


def _confirmed_intent(repos: Any):
    assert repos.trading.register_trading_account_binding(
        account_ref="binance-manual-live-1",
        account_lane="manual",
        venue="binance_usdm_live",
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


def _open_confirmed_intent(repos: Any):
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
        receipt={
            "client_id": plan.entry_client_order_id,
            "provider_id": "1",
            "status": "FILLED",
            "executed_quantity": str(plan.quantity),
            "average_price": "100.1",
        },
        now_ms=NOW + 7,
    )
    for offset, leg in ((8, "take_profit"), (11, "stop_loss")):
        client_id = getattr(plan, f"{leg}_client_order_id")
        assert repos.trading.fence_manual_protection(
            intent.intent_id,
            leg=leg,
            client_id=client_id,
            now_ms=NOW + offset,
        )
        assert repos.trading.begin_manual_order_attempt(intent.intent_id, leg=leg, now_ms=NOW + offset + 1)
        assert repos.trading.record_manual_protection(
            intent.intent_id,
            leg=leg,
            receipt={"client_id": client_id, "provider_id": str(offset), "status": "NEW"},
            now_ms=NOW + offset + 2,
        )
    return intent, plan


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


def test_private_portfolio_is_actor_bound_and_partial_close_is_replayable(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    intent, plan = _open_confirmed_intent(repos)
    opened_at_ms = NOW + 7
    assert repos.trading.observe_manual_position(
        intent.intent_id,
        position=ManualVenuePosition(
            symbol=plan.symbol,
            quantity=plan.quantity,
            entry_price=Decimal("100.1"),
            leverage=plan.leverage,
            mark_price=Decimal("101"),
            unrealized_pnl_usd=Decimal("0.09"),
            liquidation_price=Decimal("80"),
        ),
        plan=plan,
        opened_at_ms=opened_at_ms,
        now_ms=NOW + 20,
    )

    own = repos.trading.manual_positions_for_actor(
        actor_user_id=123456789,
        chat_id=-1001234567890,
        state="open",
    )
    assert len(own) == 1
    assert own[0].entry_price == Decimal("100.1")
    assert own[0].recommended.notional_usd == Decimal("10")
    assert (
        repos.trading.manual_positions_for_actor(
            actor_user_id=987654321,
            chat_id=-1001234567890,
            state="open",
        )
        == ()
    )
    assert (
        repos.trading.request_manual_position_close(
            session_id=SESSION_ID,
            actor_user_id=987654321,
            chat_id=-1001234567890,
            requested_bps=5000,
            update_id=500,
            now_ms=NOW + 21,
        )
        is None
    )

    requested = repos.trading.request_manual_position_close(
        session_id=SESSION_ID,
        actor_user_id=123456789,
        chat_id=-1001234567890,
        requested_bps=5000,
        update_id=501,
        now_ms=NOW + 22,
    )
    assert requested is not None
    replay = repos.trading.request_manual_position_close(
        session_id=SESSION_ID,
        actor_user_id=123456789,
        chat_id=-1001234567890,
        requested_bps=5000,
        update_id=501,
        now_ms=NOW + 23,
    )
    assert replay == requested
    assert (
        repos.trading.request_manual_position_close(
            session_id=SESSION_ID,
            actor_user_id=123456789,
            chat_id=-1001234567890,
            requested_bps=3000,
            update_id=502,
            now_ms=NOW + 24,
        )
        is None
    )

    target = plan.quantity / 2
    assert repos.trading.begin_manual_close_attempt(requested.close_id, quantity=target, now_ms=NOW + 25)
    assert repos.trading.record_manual_close_fill(
        requested.close_id,
        receipt={
            "client_id": requested.client_order_id,
            "provider_id": "55",
            "status": "FILLED",
            "executed_quantity": str(target),
            "average_price": "101",
        },
        now_ms=NOW + 26,
    )
    remaining = plan.quantity - target
    assert repos.trading.observe_manual_position(
        intent.intent_id,
        position=ManualVenuePosition(
            symbol=plan.symbol,
            quantity=remaining,
            entry_price=Decimal("100.1"),
            leverage=plan.leverage,
            mark_price=Decimal("101.2"),
            unrealized_pnl_usd=Decimal("0.055"),
            liquidation_price=Decimal("80"),
        ),
        plan=plan,
        opened_at_ms=opened_at_ms,
        now_ms=NOW + 27,
    )
    assert repos.trading.record_manual_partial_close_reconciled(
        requested.close_id,
        remaining_quantity=remaining,
        mark_price=Decimal("101.2"),
        now_ms=NOW + 28,
    )

    updated = repos.trading.manual_position_for_actor(
        session_id=SESSION_ID,
        actor_user_id=123456789,
        chat_id=-1001234567890,
    )
    assert updated is not None and updated.quantity == remaining
    assert updated.active_close is not None and updated.active_close.reconciled_at_ms == NOW + 28
    history = repos.trading.manual_trade_history_for_actor(
        actor_user_id=123456789,
        chat_id=-1001234567890,
    )
    assert any(event.event_kind == "ORDER_RECONCILED" for event in history)
    conn.commit()


def test_workers_role_can_request_actor_bound_close_without_execution_table_update_privilege(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    intent, plan = _open_confirmed_intent(repos)
    assert repos.trading.observe_manual_position(
        intent.intent_id,
        position=ManualVenuePosition(
            symbol=plan.symbol,
            quantity=plan.quantity,
            entry_price=Decimal("100.1"),
            leverage=plan.leverage,
            mark_price=Decimal("101"),
            unrealized_pnl_usd=Decimal("0.09"),
            liquidation_price=Decimal("80"),
        ),
        plan=plan,
        opened_at_ms=NOW + 7,
        now_ms=NOW + 20,
    )
    conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        worker_repos = repositories_for_connection(conn)
        with worker_repos.transaction():
            requested = worker_repos.trading.request_manual_position_close(
                session_id=SESSION_ID,
                actor_user_id=123456789,
                chat_id=-1001234567890,
                requested_bps=10000,
                update_id=503,
                now_ms=NOW + 21,
            )
            assert requested is not None and requested.requested_bps == 10000
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()


def test_multi_target_picker_fences_one_reply_and_binds_the_provider_message(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    sources = (
        _source().model_copy(update={"base_symbol": "HYPE"}),
        _source().model_copy(update={"base_symbol": "ETH"}),
    )
    picker, created = repos.trading.begin_manual_target_picker(
        picker_id=SESSION_ID,
        sources=sources,
        actor_user_id=123456789,
        chat_id=-1001234567890,
        now_ms=NOW,
    )
    duplicate, duplicate_created = repos.trading.begin_manual_target_picker(
        picker_id="0198f3ae-76c0-77a1-a191-0d3f16842ea1",
        sources=sources,
        actor_user_id=123456789,
        chat_id=-1001234567890,
        now_ms=NOW + 1,
    )

    assert created is True and duplicate_created is False
    assert duplicate.picker_id == picker.picker_id
    assert repos.trading.begin_manual_target_picker_reply(SESSION_ID, now_ms=NOW + 2)
    assert not repos.trading.begin_manual_target_picker_reply(SESSION_ID, now_ms=NOW + 3)
    assert repos.trading.attach_manual_target_picker_message(SESSION_ID, message_id=99, now_ms=NOW + 4)
    stored = repos.trading.manual_target_picker(SESSION_ID)
    assert stored is not None
    assert stored.interaction_message_id == 99
    assert tuple(source.base_symbol for source in stored.sources) == ("HYPE", "ETH")

    session, session_created = repos.trading.begin_manual_trade_session_from_picker(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea2",
        picker_id=SESSION_ID,
        source=sources[0],
        actor_user_id=123456789,
        chat_id=-1001234567890,
        update_id=101,
        now_ms=NOW + 5,
    )
    assert session_created is True
    consumed = repos.trading.manual_target_picker(SESSION_ID)
    assert consumed is not None and consumed.state is ManualTargetPickerState.CONSUMED
    assert consumed.selected_symbol == "HYPE"
    assert consumed.consumed_session_id == session.session_id

    replayed, replay_created = repos.trading.begin_manual_trade_session_from_picker(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea3",
        picker_id=SESSION_ID,
        source=sources[0],
        actor_user_id=123456789,
        chat_id=-1001234567890,
        update_id=101,
        now_ms=NOW + 6,
    )
    assert replay_created is False and replayed.session_id == session.session_id
    with pytest.raises(ValueError, match="manual_target_picker_source_conflict"):
        repos.trading.begin_manual_trade_session_from_picker(
            session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea4",
            picker_id=SESSION_ID,
            source=sources[1],
            actor_user_id=123456789,
            chat_id=-1001234567890,
            update_id=102,
            now_ms=NOW + 7,
        )
    with pytest.raises(ValueError, match="manual_target_picker_session_active"):
        repos.trading.begin_manual_target_picker(
            picker_id="0198f3ae-76c0-77a1-a191-0d3f16842ea5",
            sources=sources,
            actor_user_id=123456789,
            chat_id=-1001234567890,
            now_ms=NOW + 8,
        )
    assert repos.trading.cancel_manual_trade_session(
        session.session_id,
        update_id=103,
        result_code="cancelled",
        now_ms=NOW + 9,
    )
    next_picker, next_created = repos.trading.begin_manual_target_picker(
        picker_id="0198f3ae-76c0-77a1-a191-0d3f16842ea5",
        sources=sources,
        actor_user_id=123456789,
        chat_id=-1001234567890,
        now_ms=NOW + 10,
    )
    assert next_created is True and next_picker.state is ManualTargetPickerState.PENDING
    assert repos.trading.begin_manual_target_picker_reply(next_picker.picker_id, now_ms=NOW + 11)
    assert repos.trading.attach_manual_target_picker_message(next_picker.picker_id, message_id=100, now_ms=NOW + 12)
    foreign_session, foreign_created = repos.trading.begin_manual_trade_session(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea6",
        source=sources[0].model_copy(update={"delivery_message_id": 43}),
        actor_user_id=123456789,
        chat_id=-1001234567890,
        update_id=104,
        now_ms=NOW + 13,
    )
    assert foreign_created is True
    conn.commit()

    with pytest.raises(RaiseException, match="trading_manual_target_picker_identity_mutation_forbidden"):
        conn.execute(
            "UPDATE trading_manual_target_pickers SET sources = '[]'::jsonb WHERE picker_id = %s::uuid",
            (SESSION_ID,),
        )
    conn.rollback()

    with pytest.raises(RaiseException, match="trading_manual_target_picker_transition_forbidden"):
        conn.execute(
            """
            UPDATE trading_manual_target_pickers
               SET state = 'CONSUMED', selected_symbol = 'HYPE',
                   consumed_session_id = %s::uuid, consumed_at_ms = %s, updated_at_ms = %s
             WHERE picker_id = %s::uuid
            """,
            (foreign_session.session_id, NOW + 14, NOW + 14, next_picker.picker_id),
        )
    conn.rollback()

    with pytest.raises(RaiseException, match="trading_manual_target_picker_transition_forbidden"):
        conn.execute(
            "UPDATE trading_manual_target_pickers SET state = 'SENT' WHERE picker_id = %s::uuid",
            (SESSION_ID,),
        )
    conn.rollback()


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


def test_workers_role_can_claim_manual_notifications_without_event_update_privilege(conn: Any) -> None:
    _reset(conn)
    conn.execute("SET ROLE tracefold_workers")
    try:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.trading.begin_manual_notification(now_ms=NOW) is None
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()


def test_workers_role_can_create_a_manual_session_with_its_initial_effect_fence(conn: Any) -> None:
    _reset(conn)
    conn.execute("SET ROLE tracefold_workers")
    try:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            session, created = repos.trading.begin_manual_trade_session(
                session_id=SESSION_ID,
                source=_source(),
                actor_user_id=123456789,
                chat_id=-1001234567890,
                update_id=101,
                now_ms=NOW,
            )
            assert created is True
            assert session.last_effect_update_id == 101
            assert session.last_effect_result_code == "session_created"
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()


def test_session_preview_accepts_the_full_precision_live_snapshot(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    session, created = repos.trading.begin_manual_trade_session(
        session_id=SESSION_ID,
        source=_source(),
        actor_user_id=123456789,
        chat_id=-1001234567890,
        update_id=101,
        now_ms=NOW,
    )
    assert created is True and session.state is ManualSessionState.AWAITING_STRATEGY
    account = ManualAccountSnapshot(
        account_ref="binance-manual-live-1",
        venue="binance_usdm_live",
        instrument_id="BTCUSDT",
        account_equity_usd=Decimal("165.31984215"),
        reference_entry=Decimal("83.20471"),
        observed_at_ms=NOW,
    )
    recommended = ManualTradeParameters(
        notional_usd=Decimal("10"),
        leverage=10,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    preview = build_manual_trade_preview(
        side=TradeSide.LONG,
        venue="binance_usdm_live",
        account_equity=account.account_equity_usd,
        reference_entry=account.reference_entry,
        parameters=recommended,
    )
    guard = guard_manual_trade_modification(
        preset=StrategyPreset.TIGHT_STOP,
        account_equity=account.account_equity_usd,
        recommended=recommended,
        modified=recommended,
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

    stored = repos.trading.set_manual_trade_preview(
        session_id=SESSION_ID,
        preset=StrategyPreset.TIGHT_STOP,
        account_snapshot=account,
        recommended=recommended,
        selected=recommended,
        preview=preview,
        guard=guard,
        update_id=102,
        result_code="preview_ready",
        now_ms=NOW + 1,
    )

    assert stored is not None and stored.state is ManualSessionState.PREVIEW
    assert stored.account_snapshot == account
    assert stored.preview == preview
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
        account_ref="binance-manual-live-1",
        account_lane="manual",
        venue="binance_usdm_live",
        credential_fingerprint="b" * 64,
        provider_account_fingerprint="c" * 64,
        now_ms=NOW,
    )
    assert repos.trading.upsert_manual_account_snapshot(
        account_ref="binance-manual-live-1",
        venue="binance_usdm_live",
        equity_usd=Decimal("1000"),
        observed_at_ms=NOW,
        now_ms=NOW,
    )
    snapshot = repos.trading.manual_account_snapshot("binance-manual-live-1")
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
            account_ref="binance-manual-live-1",
            account_lane="manual",
            venue="binance_usdm_live",
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
            account_ref="binance-manual-live-1",
            account_lane="manual",
            venue="binance_usdm_live",
            credential_fingerprint="d" * 64,
            provider_account_fingerprint="f" * 64,
            now_ms=NOW + 1,
        )
    conn.rollback()


def test_database_rejects_an_automatic_binding_to_the_live_manual_venue(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    with pytest.raises(ValueError, match="trading_account_binding_lane_venue_invalid"):
        repos.trading.register_trading_account_binding(
            account_ref="binance-auto-live-forbidden",
            account_lane="auto",
            venue="binance_usdm_live",
            credential_fingerprint="c" * 64,
            provider_account_fingerprint="d" * 64,
            now_ms=NOW,
        )
    with pytest.raises(CheckViolation, match="trading_account_binding_venue_check"):
        conn.execute(
            """
            INSERT INTO trading_account_bindings (
              account_ref, account_lane, venue, credential_fingerprint,
              provider_account_fingerprint, created_at_ms
            ) VALUES (%s, 'auto', 'binance_usdm_live', %s, %s, %s)
            """,
            ("binance-auto-live-forbidden", "c" * 64, "d" * 64, NOW),
        )
    conn.rollback()


def test_database_preserves_historical_manual_demo_bindings_but_rejects_new_ones(conn: Any) -> None:
    _reset(conn)
    repos = repositories_for_connection(conn)
    with pytest.raises(ValueError, match="trading_account_binding_lane_venue_invalid"):
        repos.trading.register_trading_account_binding(
            account_ref="binance-manual-demo-retired",
            account_lane="manual",
            venue="binance_usdm_demo",
            credential_fingerprint="c" * 64,
            provider_account_fingerprint="d" * 64,
            now_ms=NOW,
        )
    with pytest.raises(CheckViolation, match="trading_manual_demo_binding_retired"):
        conn.execute(
            """
            INSERT INTO trading_account_bindings (
              account_ref, account_lane, venue, credential_fingerprint,
              provider_account_fingerprint, created_at_ms
            ) VALUES (%s, 'manual', 'binance_usdm_demo', %s, %s, %s)
            """,
            ("binance-manual-demo-retired", "c" * 64, "d" * 64, NOW),
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
                ('tracefold_workers', 'trading_manual_sessions', 'state'),
                ('tracefold_workers', 'trading_manual_target_pickers', 'sources'),
                ('tracefold_workers', 'trading_manual_target_pickers', 'state'),
                ('tracefold_workers', 'trading_manual_target_pickers', 'interaction_message_id'),
                ('tracefold_workers', 'trading_manual_target_pickers', 'selected_symbol')
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
        ("tracefold_workers", "trading_manual_target_pickers", "sources"): False,
        ("tracefold_workers", "trading_manual_target_pickers", "state"): True,
        ("tracefold_workers", "trading_manual_target_pickers", "interaction_message_id"): True,
        ("tracefold_workers", "trading_manual_target_pickers", "selected_symbol"): True,
    }
