"""Frozen 0420 SQL fixtures for the P1 upgrade; no current repository compatibility layer."""

from __future__ import annotations

import hashlib
from typing import Any

from tracefold.news.liquidations import LiquidationFact
from tracefold.news.smart_money import SmartMoneyFact
from tracefold.news.source_contracts import MARKET_PROVIDER


class LegacyMarketSeed:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def insert_oi_signal(
        self,
        *,
        event_id: str,
        metric_version: str,
        symbol: str,
        raw_instrument: str,
        direction: str,
        oi_change_bps: int,
        oi_value_usd: int,
        whale_long_profit_bps: int,
        whale_oi_ratio_bps: int,
        observed_at_ms: int,
        received_at_ms: int,
        now_ms: int,
        provider: str,
        source_strategy_id: str | None,
        source_contract_version: str | None,
        measurement_window_ms: int | None,
        measurement_definition: str,
        source_item_id: str,
        source_venue: str | None,
        ingest_mode: str = "live",
    ) -> None:

        proven = (
            source_strategy_id is not None and source_contract_version is not None and measurement_window_ms is not None
        )
        self.conn.execute(
            """
            INSERT INTO news_oi_signals (
              event_id, metric_version, symbol, raw_instrument, direction, oi_change_bps, oi_value_usd,
              whale_long_profit_bps, whale_oi_ratio_bps, observed_at_ms, received_at_ms, created_at_ms,
              provider, source_strategy_id, source_contract_version, measurement_window_ms,
              measurement_definition, source_item_id, source_venue, available_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source_item_id, metric_version) DO NOTHING
            """,
            (
                event_id,
                metric_version,
                symbol,
                raw_instrument,
                direction,
                int(oi_change_bps),
                int(oi_value_usd),
                int(whale_long_profit_bps),
                int(whale_oi_ratio_bps),
                int(observed_at_ms),
                int(received_at_ms),
                int(now_ms),
                provider,
                source_strategy_id if proven else None,
                source_contract_version if proven else None,
                int(measurement_window_ms) if proven and measurement_window_ms is not None else None,
                measurement_definition,
                source_item_id,
                source_venue,
                int(now_ms),
            ),
        )

    def insert_market_liquidation(
        self,
        *,
        fact: LiquidationFact,
        item_id: str,
        fact_id: str,
        source_strategy_id: str,
        ingest_mode: str,
        now_ms: int,
    ) -> None:

        # Frozen 0420 metadata is explicit; the current parser no longer carries retired fields.
        self.conn.execute(
            """
            INSERT INTO news_market_liquidations (
              source_key, item_id, fact_id, ingest_mode, provider, symbol, raw_instrument, source_venue,
              source_strategy_id, liquidated_position_side,
              forced_order_side, notional_usd, quantity, price, event_at_ms,
              received_at_ms, parser_version, provider_record_identity,
              symbol_contract_identity, position_side_semantics, quantity_semantics,
              notional_semantics, price_semantics, completeness_assumption,
              throttle_assumption, source_contract_version, source_contract_complete,
              available_at_ms, created_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source_key) DO NOTHING
            """,
            (
                hashlib.sha256(f"{item_id}\x1f{fact_id}\x1fliquidation_parser_v1".encode()).hexdigest(),
                item_id,
                fact_id,
                ingest_mode,
                MARKET_PROVIDER,
                fact.symbol,
                fact.raw_instrument,
                fact.source_venue,
                source_strategy_id,
                fact.liquidated_position_side,
                fact.forced_order_side,
                fact.notional_usd,
                None,
                fact.price,
                int(fact.event_at_ms),
                int(fact.received_at_ms),
                fact.parser_version,
                item_id,
                f"unresolved:{fact.source_venue or 'unknown'}:{fact.symbol}",
                "template_position_side;short=>forced_buy;long=>forced_sell",
                "not_provided",
                "provider_reported_usd_notional",
                "provider_reported_unspecified_price",
                "selected_events_without_heartbeat_sequence_or_coverage_sla",
                "provider_throttle_unknown",
                fact.source_contract_version,
                False,
                int(now_ms),
                int(now_ms),
            ),
        )

    def insert_market_smart_money(self, *, fact: SmartMoneyFact, ingest_mode: str, now_ms: int) -> None:

        self.conn.execute(
            """
            INSERT INTO news_market_smart_money (
              source_key, item_id, fact_id, ingest_mode, provider, source_strategy_id,
              trader_label, account_address, source_venue, raw_instrument, symbol,
              action, position_side, reported_notional_usd, price, pnl_usd,
              event_at_ms, received_at_ms, available_at_ms, created_at_ms,
              parser_version, provider_record_identity, source_contract_version,
              notional_semantics, price_semantics, completeness_assumption
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (source_key) DO NOTHING
            """,
            (
                fact.source_key,
                fact.item_id,
                fact.fact_id,
                ingest_mode,
                MARKET_PROVIDER,
                fact.source_strategy_id,
                fact.trader_label,
                fact.account_address,
                fact.source_venue,
                fact.raw_instrument,
                fact.symbol,
                fact.action,
                fact.position_side,
                fact.reported_notional_usd,
                fact.price,
                fact.pnl_usd,
                int(fact.event_at_ms),
                int(fact.received_at_ms),
                int(now_ms),
                int(now_ms),
                fact.parser_version,
                fact.provider_record_identity,
                fact.source_contract_version,
                fact.notional_semantics,
                fact.price_semantics,
                fact.completeness_assumption,
            ),
        )
