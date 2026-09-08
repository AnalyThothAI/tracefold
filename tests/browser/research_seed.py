"""Persist representative research facts through production admission for the browser seam."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_frame, admit_market_item, prepare_wallet_observation, wallet_item_id
from tracefold.news.wallet_contracts import VERIFIED_WALLET_PRICE_SOURCE, WalletEvent, WalletOutcome


def seed_research(dsn: str) -> None:
    now = int(time.time() * 1000)
    wallet, token = "0x" + "1" * 40, "0x" + "2" * 40
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
            for index in range(3):
                stamp = now - 4_000_000 + index
                event = WalletEvent(
                    item_id="",
                    kind="buy",
                    chain_id=4663,
                    wallet=wallet,
                    handle="research-wallet",
                    followers=120,
                    token=token,
                    token_symbol="RESEARCH",
                    token_decimals=18,
                    roster_version=1,
                    window_from_ms=stamp,
                    window_to_ms=stamp,
                    segment_key="browser-buy-segment",
                    event_at_ms=stamp,
                    received_at_ms=stamp,
                    title="研究钱包买入 RESEARCH",
                    usd=Decimal((index + 1) * 1000),
                    mark_price=Decimal("2"),
                    entry_price=Decimal("1.8"),
                    tx_hash="0x" + str(index + 1) * 64,
                    block_number=index + 1,
                    evidence={
                        "log_index": index,
                        "stage": "first_observed" if index == 0 else "add",
                        "selection_reason": "selected",
                        "buy_count": index + 1,
                        "unpriced_buys": 0,
                        "observed_at_ms": stamp,
                        "mark_source": VERIFIED_WALLET_PRICE_SOURCE,
                        "price_chain_id": 4663,
                        "price_token": token,
                        "price_quote": "USD",
                        "price_unit": "token",
                    },
                )
                prepared = prepare_wallet_observation(replace(event, item_id=wallet_item_id(event)))
                admit_market_item(repos, prepared, ingest_mode="live", trace_id="browser-wallet", now_ms=stamp)
                repos.news.chain_tape_record_outcome(
                    WalletOutcome(
                        item_id=prepared.item_id,
                        horizon="15m",
                        price=Decimal("2.2"),
                        at_ms=stamp + 900_000,
                        source=VERIFIED_WALLET_PRICE_SOURCE,
                        reference_price=Decimal("2"),
                        reference_at_ms=stamp,
                        target_at_ms=stamp + 900_000,
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
