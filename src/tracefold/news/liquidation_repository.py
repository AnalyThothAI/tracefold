"""Latest-only liquidation-level shadow read model (#144)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .liquidation import LIQUIDATION_FRESH_MAX_AGE_MS, LiquidationTarget, ProviderLiquidationSnapshot, target_key


class LiquidationRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def due_targets(
        self,
        targets: Sequence[LiquidationTarget],
        *,
        provider: str,
        model_version: str,
        range_key: str,
        due_before_ms: int,
        limit: int,
    ) -> list[LiquidationTarget]:
        """Unseen targets first, then the oldest attempted; at most the caller's code-owned limit."""

        rows = self.conn.execute(
            "SELECT venue, venue_symbol, last_attempt_at_ms FROM news_liquidation_snapshots "
            "WHERE provider = %s AND model_version = %s AND range_key = %s",
            (provider, model_version, range_key),
        ).fetchall()
        attempted = {(str(row["venue"]), str(row["venue_symbol"])): int(row["last_attempt_at_ms"]) for row in rows}
        indexed = list(enumerate(targets))
        due = [
            (index, target, attempted.get(target_key(target)))
            for index, target in indexed
            if attempted.get(target_key(target)) is None or int(attempted[target_key(target)]) <= int(due_before_ms)
        ]
        due.sort(key=lambda item: (item[2] is not None, item[2] or 0, item[0]))
        return [target for _, target, _ in due[: max(0, int(limit))]]

    def store_snapshot(self, snapshot: ProviderLiquidationSnapshot) -> None:
        """Record the attempt; a failed/stale attempt never erases the last successful zones."""

        self.conn.execute(
            """
            INSERT INTO news_liquidation_snapshots (
              provider, venue, venue_symbol, base_symbol, quote_asset, model_version, range_key,
              contract, authenticated, completeness, zones, source_at_ms, received_at_ms,
              last_success_at_ms, last_attempt_at_ms, freshness, degraded, error_class,
              payload_sha256, raw_level_count, raw_price_count
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
              CASE WHEN %s = 'fresh' THEN %s ELSE NULL END, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (provider, venue, venue_symbol, model_version, range_key) DO UPDATE SET
              base_symbol = excluded.base_symbol,
              quote_asset = excluded.quote_asset,
              contract = excluded.contract,
              authenticated = excluded.authenticated,
              completeness = excluded.completeness,
              zones = CASE WHEN excluded.freshness = 'fresh' THEN excluded.zones
                           ELSE news_liquidation_snapshots.zones END,
              source_at_ms = CASE WHEN excluded.freshness = 'fresh' THEN excluded.source_at_ms
                                  ELSE news_liquidation_snapshots.source_at_ms END,
              received_at_ms = CASE WHEN excluded.freshness = 'fresh' THEN excluded.received_at_ms
                                    ELSE news_liquidation_snapshots.received_at_ms END,
              last_success_at_ms = CASE WHEN excluded.freshness = 'fresh' THEN excluded.received_at_ms
                                        ELSE news_liquidation_snapshots.last_success_at_ms END,
              last_attempt_at_ms = excluded.last_attempt_at_ms,
              freshness = excluded.freshness,
              degraded = excluded.degraded,
              error_class = excluded.error_class,
              payload_sha256 = CASE WHEN excluded.freshness = 'fresh' THEN excluded.payload_sha256
                                    ELSE news_liquidation_snapshots.payload_sha256 END,
              raw_level_count = CASE WHEN excluded.freshness = 'fresh' THEN excluded.raw_level_count
                                     ELSE news_liquidation_snapshots.raw_level_count END,
              raw_price_count = CASE WHEN excluded.freshness = 'fresh' THEN excluded.raw_price_count
                                     ELSE news_liquidation_snapshots.raw_price_count END
            """,
            (
                snapshot.provider,
                snapshot.target.venue,
                snapshot.target.venue_symbol,
                snapshot.target.base_symbol,
                snapshot.target.quote_asset,
                snapshot.model_version,
                snapshot.range_key,
                snapshot.contract,
                snapshot.authenticated,
                snapshot.completeness,
                json.dumps(snapshot.zones_json(), ensure_ascii=False, separators=(",", ":")),
                snapshot.source_at_ms,
                snapshot.received_at_ms,
                snapshot.freshness,
                snapshot.received_at_ms,
                snapshot.received_at_ms,
                snapshot.freshness,
                snapshot.degraded,
                snapshot.error_class,
                snapshot.payload_sha256,
                snapshot.raw_level_count,
                snapshot.raw_price_count,
            ),
        )

    def status(self, *, now_ms: int) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT provider, venue, venue_symbol, base_symbol, model_version, range_key, "
            "jsonb_array_length(zones) AS zone_count, source_at_ms, received_at_ms, last_success_at_ms, "
            "last_attempt_at_ms, freshness, degraded, error_class FROM news_liquidation_snapshots "
            "ORDER BY provider, venue, venue_symbol, model_version, range_key LIMIT 16"
        ).fetchall()
        snapshots = []
        for raw in rows:
            row = dict(raw)
            row["age_ms"] = max(0, int(now_ms) - int(row["last_success_at_ms"])) if row["last_success_at_ms"] else None
            if row["freshness"] == "fresh" and (
                row["age_ms"] is None or int(row["age_ms"]) > LIQUIDATION_FRESH_MAX_AGE_MS
            ):
                row["freshness"] = "stale"
                row["degraded"] = True
            snapshots.append(row)
        return {
            "provider": "coinglass_web",
            "shadow": True,
            "snapshots": snapshots,
            "fresh": sum(1 for row in snapshots if row["freshness"] == "fresh"),
            "degraded": sum(1 for row in snapshots if bool(row["degraded"])),
        }


__all__ = ["LiquidationRepository"]
