from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from tracefold.platform.postgres.write_contract import returning_mutation_count


class MarketTickCurrentRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def get(self, *, target_type: str, target_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM market_tick_current
            WHERE target_type = %s AND target_id = %s
            """,
            (str(target_type), str(target_id)),
        ).fetchone()
        return cast("dict[str, Any] | None", row)

    def observed_at_by_target(self, *, target_type: str, target_ids: Sequence[str]) -> dict[str, int]:
        """Current tick age per target so pollers can refresh the stalest targets first."""
        requested = [str(value) for value in dict.fromkeys(target_ids) if str(value)]
        if not requested:
            return {}
        rows = self.conn.execute(
            """
            SELECT target_id, tick_observed_at_ms
            FROM market_tick_current
            WHERE target_type = %s AND target_id = ANY(%s::text[])
            """,
            (str(target_type), requested),
        ).fetchall()
        return {str(row["target_id"]): int(row["tick_observed_at_ms"]) for row in rows}

    def upsert_current_from_tick(self, tick_row: Mapping[str, Any]) -> bool:
        params = market_tick_current_row(tick_row)
        cursor = self.conn.execute(
            """
            INSERT INTO market_tick_current(
              target_type,
              target_id,
              tick_observed_at_ms,
              tick_id,
              source_tier,
              source_provider,
              chain,
              token_address,
              exchange,
              instrument,
              pricefeed_id,
              price_usd,
              liquidity_usd,
              volume_24h_usd,
              open_interest_usd,
              market_cap_usd,
              holders,
              updated_at_ms,
              created_at_ms
            )
            VALUES (
              %(target_type)s,
              %(target_id)s,
              %(tick_observed_at_ms)s,
              %(tick_id)s,
              %(source_tier)s,
              %(source_provider)s,
              %(chain)s,
              %(token_address)s,
              %(exchange)s,
              %(instrument)s,
              %(pricefeed_id)s,
              %(price_usd)s,
              %(liquidity_usd)s,
              %(volume_24h_usd)s,
              %(open_interest_usd)s,
              %(market_cap_usd)s,
              %(holders)s,
              %(updated_at_ms)s,
              %(created_at_ms)s
            )
            ON CONFLICT(target_type, target_id) DO UPDATE SET
              tick_observed_at_ms = EXCLUDED.tick_observed_at_ms,
              tick_id = EXCLUDED.tick_id,
              source_tier = EXCLUDED.source_tier,
              source_provider = EXCLUDED.source_provider,
              chain = EXCLUDED.chain,
              token_address = EXCLUDED.token_address,
              exchange = EXCLUDED.exchange,
              instrument = EXCLUDED.instrument,
              pricefeed_id = EXCLUDED.pricefeed_id,
              price_usd = EXCLUDED.price_usd,
              liquidity_usd = EXCLUDED.liquidity_usd,
              volume_24h_usd = EXCLUDED.volume_24h_usd,
              open_interest_usd = EXCLUDED.open_interest_usd,
              market_cap_usd = EXCLUDED.market_cap_usd,
              holders = EXCLUDED.holders,
              updated_at_ms = EXCLUDED.updated_at_ms,
              created_at_ms = EXCLUDED.created_at_ms
            WHERE (
              EXCLUDED.tick_observed_at_ms,
              EXCLUDED.updated_at_ms,
              EXCLUDED.tick_id
            ) > (
              market_tick_current.tick_observed_at_ms,
              market_tick_current.updated_at_ms,
              market_tick_current.tick_id
            )
            OR (
              (
                EXCLUDED.tick_observed_at_ms,
                EXCLUDED.updated_at_ms,
                EXCLUDED.tick_id
              ) = (
                market_tick_current.tick_observed_at_ms,
                market_tick_current.updated_at_ms,
                market_tick_current.tick_id
              )
              AND ROW(
                market_tick_current.source_tier,
                market_tick_current.source_provider,
                market_tick_current.chain,
                market_tick_current.token_address,
                market_tick_current.exchange,
                market_tick_current.instrument,
                market_tick_current.pricefeed_id,
                market_tick_current.price_usd,
                market_tick_current.liquidity_usd,
                market_tick_current.volume_24h_usd,
                market_tick_current.open_interest_usd,
                market_tick_current.market_cap_usd,
                market_tick_current.holders,
                market_tick_current.created_at_ms
              ) IS DISTINCT FROM ROW(
                EXCLUDED.source_tier,
                EXCLUDED.source_provider,
                EXCLUDED.chain,
                EXCLUDED.token_address,
                EXCLUDED.exchange,
                EXCLUDED.instrument,
                EXCLUDED.pricefeed_id,
                EXCLUDED.price_usd,
                EXCLUDED.liquidity_usd,
                EXCLUDED.volume_24h_usd,
                EXCLUDED.open_interest_usd,
                EXCLUDED.market_cap_usd,
                EXCLUDED.holders,
                EXCLUDED.created_at_ms
              )
            )
            RETURNING true AS changed
            """,
            params,
        )
        row = cursor.fetchone()
        return _single_returning_changed(cursor, row)


def market_tick_current_row(tick_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_type": str(tick_row["target_type"]),
        "target_id": str(tick_row["target_id"]),
        "tick_observed_at_ms": int(tick_row["observed_at_ms"]),
        "tick_id": str(tick_row["tick_id"]),
        "source_tier": str(tick_row["source_tier"]),
        "source_provider": str(tick_row["source_provider"]),
        "chain": tick_row.get("chain"),
        "token_address": tick_row.get("token_address"),
        "exchange": tick_row.get("exchange"),
        "instrument": tick_row.get("instrument"),
        "pricefeed_id": tick_row.get("pricefeed_id"),
        "price_usd": tick_row.get("price_usd"),
        "liquidity_usd": tick_row.get("liquidity_usd"),
        "volume_24h_usd": tick_row.get("volume_24h_usd"),
        "open_interest_usd": tick_row.get("open_interest_usd"),
        "market_cap_usd": tick_row.get("market_cap_usd"),
        "holders": tick_row.get("holders"),
        "updated_at_ms": int(tick_row["received_at_ms"]),
        "created_at_ms": int(tick_row["created_at_ms"]),
    }


def _single_returning_changed(cursor: Any, row: Any | None) -> bool:
    returning_mutation_count(cursor, row, error_code="market_tick_current_repository_rowcount_invalid")
    return row is not None and bool(row.get("changed", True))
