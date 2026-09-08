"""Buy digest population and reference arithmetic across the real PostgreSQL seam (#614)."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.chain_tape.contracts import ClassifiedFill
from tracefold.news.chain_tape.digest import build_pack, template_lines
from tracefold.news.pipeline.admission import admit_market_item, prepare_wallet_observation, wallet_item_id
from tracefold.news.wallet_contracts import WalletEvent

pytestmark = pytest.mark.integration
START = 1_788_600_000_000
END = START + 14_400_000
TOKEN = "0x" + "aa" * 20


def _fill(index: int, *, wallet: str, kind: str = "buy", usd: str | None = "1000", at_ms: int = START + 1000):
    return ClassifiedFill(
        chain_id=4663,
        tx_hash=f"0x{index:064x}",
        log_index=0,
        block_number=index,
        block_hash=f"0x{index:064x}",
        wallet=wallet,
        token=TOKEN,
        kind=kind,
        amount_raw=100,
        event_at_ms=at_ms,
        received_at_ms=at_ms,
        classified_at_ms=at_ms,
        roster_version=1,
        token_symbol="TEST",
        token_decimals=0,
        cash_token="0x" + "bb" * 20,
        cash_amount_raw=10**6 if usd is None else int(Decimal(usd) * 10**6),
        cash_decimals=6,
        usd=None if usd is None else Decimal(usd),
        usd_source=None if usd is None else "usdg_cash_leg",
    )


def _event(wallet: str) -> WalletEvent:
    event = WalletEvent(
        item_id="",
        kind="buy",
        chain_id=4663,
        wallet=wallet,
        handle=wallet[:10],
        followers=0,
        token=TOKEN,
        token_symbol="TEST",
        token_decimals=0,
        roster_version=1,
        window_from_ms=START + 1000,
        window_to_ms=START + 1000,
        segment_key="test",
        event_at_ms=START + 1000,
        received_at_ms=START + 1000,
        usd=Decimal("1000"),
        entry_price=Decimal("1"),
        mark_price=Decimal("1.5"),
        tx_hash=f"0x{int(wallet, 16):064x}",
        block_number=1,
        evidence={"observed_at_ms": START + 1000, "log_index": 0},
    )
    return replace(event, item_id=wallet_item_id(event))


def test_full_totals_and_buy_selection_survive_large_exits_and_top_n(postgres_clone_dsn: str) -> None:
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        wallets = [f"0x{index:040x}" for index in range(1, 26)]
        fills = [_fill(index, wallet=wallet) for index, wallet in enumerate(wallets, 1)]
        fills += [
            _fill(30, wallet=wallets[0], usd=None, at_ms=START + 2000),
            _fill(31, wallet=wallets[0], kind="sell", usd="1", at_ms=START + 500),
            _fill(32, wallet=wallets[0], kind="sell", usd="100", at_ms=START + 3000),
            _fill(33, wallet="0x" + "ff" * 20, kind="sell", usd="1000000"),
            # Equal timestamps do not establish which direction happened first.
            _fill(34, wallet=wallets[0], kind="sell", usd="1", at_ms=START + 1000),
        ]
        with repos.transaction():
            repos.news.chain_tape_record_fills(fills)
            for wallet in wallets:
                admit_market_item(
                    repos,
                    prepare_wallet_observation(_event(wallet)),
                    ingest_mode="live",
                    trace_id="digest-test",
                    now_ms=START + 1000,
                )
        rows = repos.news.chain_tape_digest_window(from_ms=START, to_ms=END)

        assert (rows.totals.buys, rows.totals.buy_wallets, rows.totals.buy_positions) == (26, 25, 25)
        assert (rows.totals.active_wallets, rows.totals.cards, rows.totals.sent_cards) == (26, 25, 0)
        assert rows.totals.buy_usd == Decimal("25000")
        assert len(rows.flows) == 12
        assert all(flow.wallet in wallets for flow in rows.flows)
        first = next(flow for flow in rows.flows if flow.wallet == wallets[0])
        assert (first.window_buy_raw, first.priced_buy_raw, first.unpriced_buys) == (200, 100, 1)
        assert (first.subsequent_sells, first.subsequent_sell_usd) == (1, Decimal("100"))
        pack = build_pack(rows, window_from_ms=START, window_to_ms=END, handles={}, holding_costs={})
        lines = template_lines(pack)
        assert "25 个地址、25 个钱包代币组合" in lines[0].text
        assert sum(line.cites[0].startswith("b") for line in lines) == 5
        assert "已计价部分均价 $10，" in pack.by_id()["b1"].text
        assert "26 /" not in pack.by_id()["w1"].text
        assert "12 / 25" in pack.by_id()["w1"].text


def test_unsent_candidate_outcomes_use_observation_reference_and_count_missing_baselines(
    postgres_clone_dsn: str,
) -> None:
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        events = [_event(f"0x{index:040x}") for index in (1, 2)]
        with repos.transaction():
            repos.news.chain_tape_record_fills(
                [_fill(index, wallet=event.wallet) for index, event in enumerate(events, 1)]
            )
            for event in events:
                admit_market_item(
                    repos,
                    prepare_wallet_observation(event),
                    ingest_mode="live",
                    trace_id="digest-test",
                    now_ms=START + 1000,
                )
            for event, reference in zip(events, (Decimal("1.5"), None), strict=True):
                conn.execute(
                    """INSERT INTO news_market_wallet_outcomes
                           (item_id, horizon, price, at_ms, source, reference_price, reference_at_ms,
                            target_at_ms, reference_kind)
                         VALUES (%s, '1h', 1.2, %s, 'dexscreener', %s, %s, %s, 'observed')""",
                    (event.item_id, START + 3_601_000, reference, START + 1000, START + 3_601_000),
                )
        rows = repos.news.chain_tape_digest_window(from_ms=START, to_ms=END)

        assert len(rows.outcomes) == 1
        outcome = rows.outcomes[0]
        assert (outcome.kind, outcome.horizon, outcome.reference_kind) == ("buy", "1h", "observed")
        assert (outcome.receipts, outcome.priced, outcome.comparable, outcome.median_bps) == (2, 2, 1, -2000)
        assert rows.totals.sent_cards == 0
