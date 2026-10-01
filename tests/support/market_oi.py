"""Admitted OI fact builder for market notification integration tests."""

from __future__ import annotations

from typing import Any

from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.market_observations import MarketObservation
from tracefold.news.oi_signals import measurement_definition, oi_source_contract
from tracefold.news.source_contracts import MARKET_PROVIDER


def _oi_item(conn: Any, item_id: str, *, at_ms: int, change_bps: int, ingest_mode: str = "live") -> None:
    """One admitted OI observation: the Item and its typed fact, exactly as admission writes them."""

    repos = repositories_for_connection(conn)
    with repos.transaction():
        _write_oi(repos.news, item_id, at_ms=at_ms, change_bps=change_bps, ingest_mode=ingest_mode)


def _write_oi(news: Any, item_id: str, *, at_ms: int, change_bps: int, ingest_mode: str = "live") -> None:
    """The Item and its typed fact, without owning the transaction they go in."""

    source = oi_source_contract({"strategies": [{"id": "1019"}]})
    assert source is not None
    news.insert_market_observation(
        MarketObservation(
            observation_id=item_id,
            kind="oi",
            source_id="opennews",
            source_item_key=item_id,
            title=f"WIF OI Rise {change_bps / 100}%, OI Value 11.03M, Whale Long Profit 88.40%, Whale/OI Ratio 143.90%",
            raw_first_line=item_id,
            description="",
            event_at_ms=at_ms,
            received_at_ms=at_ms,
            available_at_ms=at_ms,
            provider_metadata={},
            provider_params={"rule": "oi_rise"},
            ingest_mode=ingest_mode,
            source_strategy_id=source.strategy_id,
            parse_status="parsed",
            parse_error=None,
            oi_event_id=f"event-{item_id}",
            parser_version="oi_signal_v1",
            symbol="WIF",
            raw_instrument="WIF",
            direction="rise",
            oi_change_bps=change_bps,
            oi_value_usd=11_030_000,
            whale_long_profit_bps=8840,
            whale_oi_ratio_bps=14390,
            provider=MARKET_PROVIDER,
            source_venue="binance",
            source_contract_version=source.contract_version,
            measurement_window_ms=source.measurement_window_ms,
            measurement_definition=measurement_definition(source),
        ),
        now_ms=at_ms,
    )
