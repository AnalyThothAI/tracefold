"""Onchain Telegram analysis persists independently from futures sessions and intents."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.onchain_trading import OnchainQuoteResult
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.trading import (
    OnchainAnalysisState,
    OnchainAssetCandidate,
    OnchainNewsSource,
    OnchainRouteQuote,
    OnchainTelegramEditPayload,
    OnchainTelegramEditState,
    analyze_onchain_routes,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_module_clone_dsn")]

NOW = 1_900_000_000_000
SESSION_ID = "0198f3ae-76c0-77a1-a191-0d3f16842ea0"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
HYPE = "0x1111111111111111111111111111111111111111"


@pytest.fixture(scope="module")
def conn() -> Any:
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def _sources() -> tuple[OnchainNewsSource, ...]:
    return (
        OnchainNewsSource(
            news_event_id="event-42",
            delivery_target_sha256="a" * 64,
            delivery_message_id=42,
            headline_zh="正文还提到了 BTC，但 TG 标的是 HYPE",
            ticker="HYPE",
            source_observed_at_ms=NOW - 1_000,
        ),
    )


def _candidate() -> OnchainAssetCandidate:
    return OnchainAssetCandidate(
        chain_id=1,
        chain_name="Ethereum",
        contract_address=HYPE,
        symbol="HYPE",
        name="Hyperliquid",
        decimals=18,
        providers=("okx", "oneinch"),
        verified=True,
        confidence_bps=9_000,
    )


def test_resolution_and_quote_analysis_are_durable_without_creating_futures_intent(conn: Any) -> None:
    conn.execute("TRUNCATE trading_onchain_telegram_edit_effects, trading_onchain_analysis_sessions CASCADE")
    conn.commit()
    repos = repositories_for_connection(conn)

    session, created = repos.trading.begin_onchain_analysis_session(
        session_id=SESSION_ID,
        sources=_sources(),
        selected_ticker="HYPE",
        actor_user_id=123456789,
        chat_id=-1001234567890,
        now_ms=NOW,
    )
    duplicate, duplicate_created = repos.trading.begin_onchain_analysis_session(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea1",
        sources=_sources(),
        selected_ticker="HYPE",
        actor_user_id=123456789,
        chat_id=-1001234567890,
        now_ms=NOW + 1,
    )
    assert created is True and duplicate_created is False and duplicate.session_id == session.session_id
    assert repos.trading.set_onchain_candidates(
        SESSION_ID,
        candidates=(_candidate(),),
        provider_errors=("binance_general_web3_swap_api_unpublished",),
        now_ms=NOW + 2,
    )
    assert repos.trading.begin_onchain_interaction_reply(SESSION_ID, now_ms=NOW + 3)
    assert not repos.trading.begin_onchain_interaction_reply(SESSION_ID, now_ms=NOW + 4)
    assert repos.trading.attach_onchain_interaction_message(SESSION_ID, message_id=99, now_ms=NOW + 4)
    assert repos.trading.claim_manual_telegram_update(
        TelegramTradingUpdate(
            update_id=102,
            callback_query_id="onchain-callback-102",
            actor_user_id=123456789,
            chat_id=-1001234567890,
            message_id=99,
            data=f"tf:o:c:0:{SESSION_ID}",
            authorized=True,
        ),
        now_ms=NOW + 4,
    )
    quoting = repos.trading.begin_onchain_quote(SESSION_ID, candidate_index=0, now_ms=NOW + 5)
    assert quoting is not None and quoting.state is OnchainAnalysisState.QUOTING

    quote = OnchainRouteQuote(
        provider="okx",
        chain_id=1,
        input_contract=USDC,
        output_contract=HYPE,
        input_amount_raw=10_000_000,
        expected_output_raw=1_000_000_000_000_000_000,
        minimum_output_raw=990_000_000_000_000_000,
        slippage_bps=100,
        latency_ms=120,
        received_at_ms=NOW + 5,
        expires_at_ms=NOW + 20_000,
    )
    result = OnchainQuoteResult(
        analysis=analyze_onchain_routes((quote,), now_ms=NOW + 5),
        settlement_symbol="USDC",
        settlement_decimals=6,
        output_decimals=18,
        provider_errors=("binance_general_web3_swap_api_unpublished",),
    )
    stored_with_effect = repos.trading.set_onchain_analysis_and_begin_edit(
        SESSION_ID,
        analysis=result.analysis,
        provider_errors=result.provider_errors,
        update_id=102,
        payload=OnchainTelegramEditPayload(
            message_id=99,
            text="链上最佳路由：OKX",
            keyboard=(("刷新报价", f"tf:o:r:{SESSION_ID}"),),
        ),
        result_code="onchain_analysis_ready",
        now_ms=NOW + 6,
    )
    assert stored_with_effect is not None
    stored, effect = stored_with_effect
    assert effect.state is OnchainTelegramEditState.SENDING
    assert repos.trading.settle_onchain_telegram_edit_sent(
        SESSION_ID,
        update_id=102,
        now_ms=NOW + 7,
    )
    conn.commit()

    assert stored is not None and stored.state is OnchainAnalysisState.ANALYZED
    assert stored.analysis is not None and stored.analysis.winner_provider == "okx"
    assert conn.execute("SELECT count(*) AS n FROM trading_manual_intents").fetchone()["n"] == 0


def test_runtime_roles_cannot_rebind_message_or_revive_cancelled_onchain_session(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    for update_id in (103, 104):
        assert repos.trading.claim_manual_telegram_update(
            TelegramTradingUpdate(
                update_id=update_id,
                callback_query_id=f"onchain-callback-{update_id}",
                actor_user_id=123456789,
                chat_id=-1001234567890,
                message_id=99,
                data=f"tf:o:r:{SESSION_ID}",
                authorized=True,
            ),
            now_ms=NOW + 7,
        )
    conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(CheckViolation, match="trading_onchain_session_shape_check"):
            conn.execute(
                """
                INSERT INTO trading_onchain_analysis_sessions (
                  session_id, sources, actor_user_id, chat_id, source_message_id,
                  state, selected_ticker, candidates, provider_errors, created_at_ms, updated_at_ms
                ) VALUES (
                  '0198f3ae-76c0-77a1-a191-0d3f16842eaf', '[{}]'::jsonb,
                  123456789, -1001234567890, 43, 'QUOTING', 'HYPE', '[{}]'::jsonb,
                  '[]'::jsonb, %s, %s
                )
                """,
                (NOW + 7, NOW + 7),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(RaiseException, match="trading_onchain_edit_effect_binding_invalid"):
            conn.execute(
                """
                INSERT INTO trading_onchain_telegram_edit_effects (
                  session_id, update_id, message_id, payload, result_code, state, attempted_at_ms
                ) VALUES (%s, 103, 100,
                  '{"message_id":100,"text":"wrong target","keyboard":[]}'::jsonb,
                  'onchain_analysis_ready', 'SENDING', %s)
                """,
                (SESSION_ID, NOW + 7),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(CheckViolation, match="trading_onchain_edit_effect_payload_check"):
            conn.execute(
                """
                INSERT INTO trading_onchain_telegram_edit_effects (
                  session_id, update_id, message_id, payload, result_code, state, attempted_at_ms
                ) VALUES (%s, 104, 99,
                  '{"message_id":99,"text":"missing keyboard"}'::jsonb,
                  'onchain_analysis_ready', 'SENDING', %s)
                """,
                (SESSION_ID, NOW + 7),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        conn.execute(
            """
            INSERT INTO trading_onchain_telegram_edit_effects (
              session_id, update_id, message_id, payload, result_code, state, attempted_at_ms
            ) VALUES (%s, 104, 99,
              '{"message_id":99,"text":"valid replay","keyboard":[]}'::jsonb,
              'onchain_analysis_ready', 'SENDING', %s)
            """,
            (SESSION_ID, NOW + 7),
        )
        conn.commit()
        with pytest.raises(CheckViolation, match="trading_onchain_edit_effect_state_check"):
            conn.execute(
                "UPDATE trading_onchain_telegram_edit_effects "
                "SET state = 'AMBIGUOUS', error_code = NULL, settled_at_ms = %s "
                "WHERE session_id = %s AND update_id = 104",
                (NOW + 8, SESSION_ID),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(RaiseException, match="trading_onchain_interaction_message_rebind_forbidden"):
            conn.execute(
                "UPDATE trading_onchain_analysis_sessions SET interaction_message_id = 100 WHERE session_id = %s",
                (SESSION_ID,),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(RaiseException, match="trading_onchain_same_state_business_mutation_forbidden"):
            conn.execute(
                "UPDATE trading_onchain_analysis_sessions "
                "SET provider_errors = '[\"tampered\"]'::jsonb, updated_at_ms = %s "
                "WHERE session_id = %s",
                (NOW + 8, SESSION_ID),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(RaiseException, match="trading_onchain_edit_effect_terminal"):
            conn.execute(
                "UPDATE trading_onchain_telegram_edit_effects "
                "SET state = 'AMBIGUOUS', error_code = 'tampered', settled_at_ms = %s "
                "WHERE session_id = %s AND update_id = 102",
                (NOW + 8, SESSION_ID),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        conn.execute(
            "UPDATE trading_onchain_analysis_sessions SET state = 'CANCELLED', updated_at_ms = %s "
            "WHERE session_id = %s",
            (NOW + 7, SESSION_ID),
        )
        conn.commit()
        with pytest.raises(RaiseException, match="trading_onchain_cancelled_terminal"):
            conn.execute(
                "UPDATE trading_onchain_analysis_sessions "
                "SET provider_errors = '[\"tampered\"]'::jsonb WHERE session_id = %s",
                (SESSION_ID,),
            )
        conn.rollback()
        with pytest.raises(RaiseException, match="trading_onchain_cancelled_terminal"):
            conn.execute(
                "UPDATE trading_onchain_analysis_sessions SET state = 'ANALYZED', updated_at_ms = %s "
                "WHERE session_id = %s",
                (NOW + 8, SESSION_ID),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    for role in ("tracefold_serve", "tracefold_nautilus"):
        conn.execute(f"SET ROLE {role}")
        try:
            with pytest.raises(InsufficientPrivilege):
                conn.execute(
                    "UPDATE trading_onchain_analysis_sessions SET updated_at_ms = %s WHERE session_id = %s",
                    (NOW + 9, SESSION_ID),
                )
        finally:
            conn.rollback()
            conn.execute("RESET ROLE")
            conn.commit()
