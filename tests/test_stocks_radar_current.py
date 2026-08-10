from __future__ import annotations

from tracefold.market.radar.stocks_current import reduce_stocks_radar

NOW_MS = 1_800_000_000_000
MINUTE_MS = 60_000


def test_stocks_reducer_reads_one_fact_set_and_keeps_all_public_windows() -> None:
    rows = [
        {
            "target_id": "instrument-aapl",
            "symbol": "AAPL",
            "security_name": "Apple Inc.",
            "exchange": "NASDAQ",
            "instrument_type": "spot",
            "event_id": "event-new",
            "received_at_ms": NOW_MS - 4 * MINUTE_MS,
            "author_handle": "alice",
            "text": "Apple mention",
        },
        {
            "target_id": "instrument-aapl",
            "symbol": "AAPL",
            "security_name": "Apple Inc.",
            "exchange": "NASDAQ",
            "instrument_type": "spot",
            "event_id": "event-old",
            "received_at_ms": NOW_MS - 30 * MINUTE_MS,
            "author_handle": "bob",
            "text": "Older Apple mention",
        },
    ]

    reduced = reduce_stocks_radar(rows, now_ms=NOW_MS)

    assert set(reduced.projections) == {"5m", "1h", "4h", "24h"}
    assert reduced.projections["5m"]["rows"][0]["mentions"] == 1
    assert reduced.projections["1h"]["rows"][0]["mentions"] == 2
    assert reduced.projections["24h"]["rows"][0]["target_id"] == "instrument-aapl"
