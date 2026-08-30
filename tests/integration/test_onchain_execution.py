"""Shared-wallet execution grants keep Telegram confirmation separate from signing."""

from __future__ import annotations

from typing import Any

import pytest
from eth_abi.abi import encode as encode_abi
from psycopg.errors import CheckViolation, InsufficientPrivilege, RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.telegram import TelegramTradingUpdate
from tracefold.platform.config.models import ONCHAIN_EXECUTION_SETTLEMENT_CATALOG_V1
from tracefold.trading import (
    OnchainAnalysisState,
    OnchainAssetCandidate,
    OnchainExecutionPlan,
    OnchainExecutionState,
    OnchainNewsSource,
    OnchainQuoteRequest,
    OnchainRouteQuote,
    OnchainSignedTransaction,
    OnchainTransactionTemplate,
    analyze_onchain_routes,
    onchain_wallet_fingerprint,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_module_clone_dsn")]

NOW = 1_900_000_000_000
SESSION_ID = "0198f3ae-76c0-77a1-a191-0d3f16842eb0"
EXECUTION_ID = "0198f3ae-76c0-77a1-a191-0d3f16842eb1"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TOKEN = "0x1111111111111111111111111111111111111111"
WALLET = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
SECOND_WALLET = "0x1111111111111111111111111111111111111111"
ROUTER = "0x111111125421ca6dc452d289314280a0f8842a65"
EXECUTOR = "0x2222222222222222222222222222222222222222"


@pytest.fixture(scope="module")
def conn() -> Any:
    connection = connect_postgres_test(read_only=False)
    yield connection
    connection.close()


def test_workers_confirm_but_only_onchain_role_advances_and_records_signed_bytes(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    source = OnchainNewsSource(
        news_event_id="development-test:event-onchain-execution",
        delivery_target_sha256="a" * 64,
        delivery_message_id=99,
        headline_zh="HYPE 新闻",
        ticker="HYPE",
        source_observed_at_ms=NOW - 1,
    )
    candidate = OnchainAssetCandidate(
        chain_id=1,
        chain_name="Ethereum",
        contract_address=TOKEN,
        symbol="HYPE",
        name="Hyperliquid",
        decimals=18,
        providers=("okx", "oneinch"),
        verified=True,
        confidence_bps=9_000,
    )
    request = OnchainQuoteRequest(
        chain_id=1,
        input_contract=USDC,
        output_contract=TOKEN,
        input_amount_raw=10_000_000,
        slippage_bps=100,
    )
    quote = OnchainRouteQuote(
        provider="oneinch",
        chain_id=1,
        input_contract=USDC,
        output_contract=TOKEN,
        input_amount_raw=10_000_000,
        expected_output_raw=1_000,
        minimum_output_raw=990,
        slippage_bps=100,
        latency_ms=50,
        received_at_ms=NOW,
        expires_at_ms=NOW + 30_000,
    )
    session, _ = repos.trading.begin_onchain_analysis_session(
        session_id=SESSION_ID,
        sources=(source,),
        selected_ticker="HYPE",
        actor_user_id=123456789,
        chat_id=-1001234567890,
        now_ms=NOW,
    )
    assert repos.trading.set_onchain_candidates(
        SESSION_ID,
        candidates=(candidate,),
        provider_errors=(),
        now_ms=NOW + 1,
    )
    assert repos.trading.begin_onchain_interaction_reply(SESSION_ID, now_ms=NOW + 2)
    assert repos.trading.attach_onchain_interaction_message(SESSION_ID, message_id=101, now_ms=NOW + 3)
    quoting = repos.trading.begin_onchain_quote(SESSION_ID, candidate_index=0, now_ms=NOW + 4)
    assert quoting is not None
    session = repos.trading.set_onchain_analysis(
        SESSION_ID,
        analysis=analyze_onchain_routes((quote,), now_ms=NOW + 4),
        provider_errors=(),
        now_ms=NOW + 5,
    )
    assert session is not None and session.state is OnchainAnalysisState.ANALYZED
    assert repos.trading.claim_manual_telegram_update(
        TelegramTradingUpdate(
            update_id=700,
            callback_query_id="confirm-onchain-700",
            actor_user_id=123456789,
            chat_id=-1001234567890,
            message_id=101,
            data=f"tf:o:y:{SESSION_ID}",
            authorized=True,
        ),
        now_ms=NOW + 6,
    )
    conn.commit()

    conn.execute("SET ROLE tracefold_workers")
    try:
        with pytest.raises(CheckViolation, match="trading_onchain_execution_notional_check"):
            repos.trading.begin_onchain_execution(
                execution_id="0198f3ae-76c0-77a1-a191-0d3f16842eb2",
                session=session,
                provider="oneinch",
                wallet_address=WALLET,
                notional_usd="200.01",
                settlement_decimals=6,
                request=request,
                quote=quote,
                now_ms=NOW + 7,
            )
        conn.rollback()
        conn.execute("SET ROLE tracefold_workers")
        with pytest.raises(RaiseException, match="trading_onchain_execution_settlement_binding_invalid"):
            repos.trading.begin_onchain_execution(
                execution_id="0198f3ae-76c0-77a1-a191-0d3f16842eb3",
                session=session,
                provider="oneinch",
                wallet_address=WALLET,
                notional_usd="0.00000000001",
                settlement_decimals=18,
                request=request,
                quote=quote,
                now_ms=NOW + 7,
            )
        conn.rollback()
        conn.execute("SET ROLE tracefold_workers")
        execution, created = repos.trading.begin_onchain_execution(
            execution_id=EXECUTION_ID,
            session=session,
            provider="oneinch",
            wallet_address=WALLET,
            notional_usd=10,
            settlement_decimals=6,
            request=request,
            quote=quote,
            now_ms=NOW + 7,
        )
        assert created and execution.state is OnchainExecutionState.AWAITING_CONFIRMATION
        assert repos.trading.confirm_onchain_execution(SESSION_ID, update_id=700, now_ms=NOW + 8)
        conn.commit()
        with pytest.raises(RaiseException, match="workers_transition_forbidden"):
            conn.execute(
                "UPDATE trading_onchain_execution_intents SET state = 'CLAIMED', updated_at_ms = %s "
                "WHERE execution_id = %s",
                (NOW + 9, EXECUTION_ID),
            )
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_onchain")
    try:
        assert (
            repos.trading.claim_next_onchain_execution(
                actor_user_id=999999999,
                wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
                now_ms=NOW + 9,
            )
            is None
        )
        claimed = repos.trading.claim_next_onchain_execution(
            actor_user_id=123456789,
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            now_ms=NOW + 10,
        )
        assert claimed is not None and claimed.state is OnchainExecutionState.CLAIMED
        approval_data = "0x095ea7b3" + ROUTER[2:].rjust(64, "0") + hex(10_000_000)[2:].rjust(64, "0")
        swap_data = (
            "0x07ed2379"
            + encode_abi(
                ["address", "(address,address,address,address,uint256,uint256,uint256)", "bytes"],
                [EXECUTOR, (USDC, TOKEN, ROUTER, WALLET, 10_000_000, 990, 0), b"\x01"],
            ).hex()
        )
        plan = OnchainExecutionPlan(
            provider="oneinch",
            wallet_address=WALLET,
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            request=request,
            quote=quote,
            approval=OnchainTransactionTemplate(
                provider="oneinch",
                leg="approval",
                chain_id=1,
                from_address=WALLET,
                to_address=USDC,
                data=approval_data,
            ),
            swap=OnchainTransactionTemplate(
                provider="oneinch",
                leg="swap",
                chain_id=1,
                from_address=WALLET,
                to_address=ROUTER,
                data=swap_data,
            ),
            prepared_at_ms=NOW + 10,
            expires_at_ms=NOW + 20_000,
        )
        assert repos.trading.store_onchain_execution_plan(EXECUTION_ID, plan=plan, now_ms=NOW + 10)
        signed = OnchainSignedTransaction(
            provider="oneinch",
            leg="swap",
            chain_id=1,
            wallet_address=WALLET,
            nonce=1,
            raw_transaction="0x1234",
            transaction_hash="0x" + "b" * 64,
        )
        assert repos.trading.append_onchain_signed_transaction(
            EXECUTION_ID,
            signed=signed,
            now_ms=NOW + 11,
        )
        assert repos.trading.record_onchain_executor_heartbeat(
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            now_ms=NOW + 11,
        )
        assert repos.trading.record_onchain_executor_heartbeat(
            wallet_fingerprint=onchain_wallet_fingerprint(SECOND_WALLET),
            now_ms=NOW + 11,
        )
        assert conn.execute("SELECT count(*) AS count FROM trading_onchain_executor_runtime").fetchone()["count"] == 2
        conn.commit()
    finally:
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_onchain")
    try:
        with pytest.raises(InsufficientPrivilege):
            conn.execute("SELECT chain_id FROM trading_onchain_settlement_assets LIMIT 1").fetchone()
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()

    catalog_rows = conn.execute(
        "SELECT chain_id, contract_address, symbol, decimals FROM trading_onchain_settlement_assets"
    ).fetchall()
    catalog = {
        (int(row["chain_id"]), str(row["contract_address"]), str(row["symbol"]), int(row["decimals"]))
        for row in catalog_rows
    }
    assert catalog == set(ONCHAIN_EXECUTION_SETTLEMENT_CATALOG_V1)
    for statement in (
        "INSERT INTO trading_onchain_settlement_assets "
        "(chain_id, contract_address, symbol, decimals) "
        "VALUES (999, '0x9999999999999999999999999999999999999999', 'USDZ', 6)",
        "UPDATE trading_onchain_settlement_assets SET decimals = 7 WHERE chain_id = 1",
        "DELETE FROM trading_onchain_settlement_assets WHERE chain_id = 1",
        "TRUNCATE trading_onchain_settlement_assets",
    ):
        with pytest.raises(RaiseException, match="trading_onchain_settlement_asset_mutation_forbidden"):
            conn.execute(statement)
        conn.rollback()

    with pytest.raises(RaiseException, match="trading_onchain_execution_notional_mutation_forbidden"):
        conn.execute(
            "UPDATE trading_onchain_execution_intents SET notional_usd = 11 WHERE execution_id = %s",
            (EXECUTION_ID,),
        )
    conn.rollback()

    conn.execute("SET ROLE tracefold_workers")
    try:
        visible = repos.trading.onchain_execution_for_session(SESSION_ID)
        assert visible is not None and visible.swap_transaction is not None
        assert visible.swap_transaction.transaction_hash == "0x" + "b" * 64
        assert visible.swap_transaction.nonce is None
        assert visible.swap_transaction.raw_transaction is None
        assert repos.trading.onchain_executor_available(
            wallet_fingerprint=onchain_wallet_fingerprint(WALLET),
            now_ms=NOW + 12,
        )
    finally:
        conn.execute("RESET ROLE")
        conn.commit()

    conn.execute("SET ROLE tracefold_nautilus")
    try:
        with pytest.raises(InsufficientPrivilege):
            conn.execute("SELECT execution_id FROM trading_onchain_execution_intents LIMIT 1").fetchone()
        with pytest.raises(InsufficientPrivilege):
            conn.execute("SELECT wallet_fingerprint FROM trading_onchain_executor_runtime LIMIT 1").fetchone()
        with pytest.raises(InsufficientPrivilege):
            conn.execute("SELECT chain_id FROM trading_onchain_settlement_assets LIMIT 1").fetchone()
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()
