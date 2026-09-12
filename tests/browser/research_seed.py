"""Persist representative research facts through production admission for the browser seam."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from tests.news.net_buy_fixtures import movement, roster, snapshot
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame, admit_market_item, prepare_wallet_observation, wallet_item_id
from tracefold.news.wallet_contracts import WalletEvent, WalletOutcome


def seed_research(dsn: str) -> None:
    now = int(time.time() * 1000)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            frame = parse_opennews_message(
                {
                    "method": "strategy.triggered",
                    "params": {
                        "id": now,
                        "engineType": "market",
                        "source": "binance",
                        "ts": now / 1000,
                        "text": "BTC OI Rise 6.71%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%",
                        "strategy": {"id": 1019, "name": "OI Event Monitor", "sourceType": "market"},
                    },
                }
            )
            assert frame is not None
            admitted = admit_frame(
                repos,
                event=frame,
                ingest_mode="live",
                observed_at_ms=now,
                trace_id="browser-research",
                watchlist_symbols=frozenset(),
                now_ms=now,
            )
            stamp = now - 4_000_000
            members = roster()
            for member in members:
                member["known_at_ms"] = member["monitoring_from_ms"] = stamp - 3600000
            fills = [replace(movement(i, at=stamp), token_symbol="RESEARCH") for i in range(1, 6)]
            event = WalletEvent(
                item_id="",
                chain_id=4663,
                token=fills[0].token,
                token_symbol="RESEARCH",
                trigger_tx_hash=fills[-1].tx_hash,
                event_at_ms=stamp,
                received_at_ms=stamp,
                detected_at_ms=stamp,
                trigger_max_age_s=60,
                notification_eligible=False,
                notification_reason="wallet_notifications_disabled",
                initial_snapshot=snapshot(fills, members=members, cutoff_at_ms=stamp, coverage_from_ms=stamp - 3600000),
                reference_price=Decimal("2"),
                reference_at_ms=stamp,
                reference_source="recorded_fixture",
            )
            prepared = prepare_wallet_observation(replace(event, item_id=wallet_item_id(event)))
            admit_market_item(repos, prepared, ingest_mode="live", trace_id="browser-wallet", now_ms=stamp)
            repos.news.chain_tape_record_fills(fills)
            repos.news.chain_tape_record_outcome(
                WalletOutcome(
                    item_id=prepared.item_id,
                    horizon="15m",
                    price=Decimal("2.2"),
                    at_ms=stamp + 900000,
                    source="recorded_fixture",
                    reference_price=Decimal("2"),
                    reference_at_ms=stamp,
                    target_at_ms=stamp + 900000,
                    status="comparable",
                )
            )
            conn.execute(
                """INSERT INTO trading_cases (
                case_id, underlying_key, trigger_kind, primary_source_key, manifest, manifest_sha256,
                state, policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms, updated_at_ms
                ) VALUES ('browser-research-case', 'crypto:BTC', 'oi', 'browser-research-source', %s::jsonb, %s,
                          'NO_TRADE', 'no_trade', 'smart_money_ratio_below_or_equal_floor', %s, %s, %s, %s)""",
                (json.dumps({"contexts": {"oi": {"source_item_id": admitted.item_id}}}), "f" * 64, now, now, now, now),
            )
