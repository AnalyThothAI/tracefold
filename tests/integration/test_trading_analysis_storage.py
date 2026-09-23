"""PostgreSQL proof for relay crash, same-asset work and stale model fencing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.news.storage.root import NewsRepository
from tracefold.platform.market_identity import DEFAULT_UNIVERSE, AssetId, AssetRegistry, InstrumentRef
from tracefold.trading.engine.target import SourceAsset, select_target
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


def _selection(symbol: str = "SOL"):
    registry = AssetRegistry(
        snapshot_ref="test-catalogue",
        instruments=(
            InstrumentRef(
                venue="binance.usdm",
                environment="demo",
                product="perpetual",
                native_symbol=f"{symbol}USDT",
                asset_id=AssetId("crypto", symbol),
                quote_asset="USDT",
                settlement_asset="USDT",
                units_per_contract=Decimal(1),
                price_unit=f"USDT/{symbol}",
                quantity_unit=symbol,
            ),
        ),
    )
    return select_target(
        kind="oi",
        assets=(SourceAsset(symbol, "crypto", "primary"),),
        registry=registry,
        universe=DEFAULT_UNIVERSE,
        execution_environment="demo",
    )


def test_claim_serializes_one_asset_without_blocking_another(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "claim-fairness-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        for index, symbol in enumerate(("SOL", "SOL", "ADA")):
            at_ms = 1_100 + index * 100
            with conn.transaction():
                trading.accept_trigger(
                    kind="oi",
                    source_fact_key=f"fair-{index}",
                    source_revision="v1",
                    payload_sha256=f"{index + 1:064x}",
                    payload={
                        "kind": "oi",
                        "source_recorded_at_ms": 1_000,
                        "provider_event_at_ms": 900,
                        "assets": [{"symbol": symbol, "market_type": "crypto", "role": "primary"}],
                    },
                    selection=_selection(symbol),
                    now_ms=at_ms,
                    root_ttl_ms=10_000,
                )
        with conn.transaction():
            first = trading.claim_analysis_case(now_ms=1_500, lease_ms=2_000)
        with conn.transaction():
            second = trading.claim_analysis_case(now_ms=1_600, lease_ms=2_000)
        assert first is not None and second is not None
        assert (first["target_asset_id"], second["target_asset_id"]) == ("crypto:SOL", "crypto:ADA")
        with conn.transaction():
            assert trading.claim_analysis_case(now_ms=1_700, lease_ms=2_000) is None
            assert trading.finish_analysis_case(
                case_id=first["case_id"],
                claim_token=first["claim_token"],
                now_ms=1_800,
                analysis_status="analyzed",
                evidence_ref="fixture",
                decision={"action": "NO_TRADE", "side": None, "reason": "fixture"},
            )
        with conn.transaction():
            next_sol = trading.claim_analysis_case(now_ms=1_900, lease_ms=2_000)
        assert next_sol is not None and next_sol["target_asset_id"] == "crypto:SOL"
        assert next_sol["case_id"] != first["case_id"]
    finally:
        conn.close()


def test_relay_retries_reuse_case_and_old_claim_cannot_finish(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "analysis-db", read_only=False)
    try:
        migrate(conn)
        news = NewsRepository(conn)
        trading = TradingRepository(conn)
        payload = {
            "kind": "oi",
            "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
            "source_recorded_at_ms": 1_000,
            "provider_event_at_ms": 900,
            "oi_change_bps": 500,
            "oi_value_usd": 1_000_000,
        }
        with conn.transaction():
            assert news.enqueue_trade_event(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="metric-v1",
                payload=payload,
                source_recorded_at_ms=1_000,
            )
        event = news.unacknowledged_trade_events(limit=10)[0]
        with conn.transaction():
            trigger_id, case_id, result = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="metric-v1",
                payload_sha256=event["payload_sha256"],
                payload=event["payload"],
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
        assert result == "accepted"
        # The process died after Trading committed and before News acknowledgement.
        with conn.transaction():
            repeated = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-1",
                source_revision="metric-v1",
                payload_sha256=event["payload_sha256"],
                payload=event["payload"],
                selection=_selection(),
                now_ms=1_200,
                root_ttl_ms=10_000,
            )
        assert repeated == (trigger_id, case_id, "duplicate")
        count = conn.execute(
            "SELECT count(*) AS n FROM trading_cases WHERE trigger_id=%s",
            (trigger_id,),
        ).fetchone()
        assert count["n"] == 1
        with conn.transaction():
            assert news.acknowledge_trade_event(
                event_id=event["event_id"], payload_sha256=event["payload_sha256"], now_ms=1_300
            )
        assert news.unacknowledged_trade_events(limit=10) == []

        with conn.transaction():
            _, next_case_id, next_result = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-2",
                source_revision="metric-v1",
                payload_sha256="d" * 64,
                payload=payload,
                selection=_selection(),
                now_ms=1_400,
                root_ttl_ms=10_000,
            )
        assert next_result == "accepted"
        next_trigger_id = conn.execute(
            "SELECT trigger_id FROM trading_cases WHERE case_id=%s",
            (next_case_id,),
        ).fetchone()["trigger_id"]
        history = trading.recent_asset_source_context(
            asset_id="crypto:SOL",
            known_at_ms=1_400,
            exclude_trigger_id=next_trigger_id,
        )
        assert [item["source_fact_key"] for item in history] == ["frame-1"]
        assert (
            trading.recent_asset_source_context(
                asset_id="crypto:SOL",
                known_at_ms=1_099,
                exclude_trigger_id="not-a-trigger",
            )
            == []
        )

        with conn.transaction():
            first = trading.claim_analysis_case(now_ms=1_500, lease_ms=2_000)
        assert first is not None and first["case_id"] == case_id
        with conn.transaction():
            reclaimed = trading.claim_analysis_case(now_ms=3_600, lease_ms=2_000)
        assert reclaimed is not None and reclaimed["claim_token"] != first["claim_token"]
        decision = {"action": "NO_TRADE", "side": None, "reason": "No durable directional edge."}
        with conn.transaction():
            assert not trading.finish_analysis_case(
                case_id=case_id,
                claim_token=first["claim_token"],
                now_ms=3_700,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision=decision,
            )
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=reclaimed["claim_token"],
                now_ms=3_700,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision=decision,
            )
        row = conn.execute("SELECT state, analysis_status FROM trading_cases WHERE case_id=%s", (case_id,)).fetchone()
        assert row == {"state": "DONE", "analysis_status": "analyzed"}
    finally:
        conn.close()


def test_valid_trade_analysis_records_publication_refusal(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "publication-db", read_only=False)
    try:
        migrate(conn)
        trading = TradingRepository(conn)
        with conn.transaction():
            _, case_id, result = trading.accept_trigger(
                kind="oi",
                source_fact_key="frame-blocked",
                source_revision="metric-v1",
                payload_sha256="c" * 64,
                payload={
                    "kind": "oi",
                    "source_recorded_at_ms": 1_000,
                    "provider_event_at_ms": 900,
                    "assets": [{"symbol": "SOL", "market_type": "crypto", "role": "primary"}],
                },
                selection=_selection(),
                now_ms=1_100,
                root_ttl_ms=10_000,
            )
        assert result == "accepted"
        with conn.transaction():
            claim = trading.claim_analysis_case(now_ms=1_500, lease_ms=2_000)
            assert claim is not None
            assert trading.finish_analysis_case(
                case_id=case_id,
                claim_token=claim["claim_token"],
                now_ms=1_600,
                analysis_status="analyzed",
                evidence_ref="evidence-ref",
                decision={"action": "TRADE", "side": "long", "reason": "test"},
                publish_block_reason="analysis_signal_expired",
            )
        row = conn.execute(
            "SELECT c.state,c.analysis_status,d.publish_status,d.publish_reason "
            "FROM trading_cases c JOIN trading_case_decisions d USING(case_id) "
            "WHERE c.case_id=%s",
            (case_id,),
        ).fetchone()
        assert row == {
            "state": "DONE",
            "analysis_status": "analyzed",
            "publish_status": "blocked",
            "publish_reason": "analysis_signal_expired",
        }
        assert conn.execute("SELECT count(*) AS n FROM trading_trade_signals").fetchone()["n"] == 0
    finally:
        conn.close()
