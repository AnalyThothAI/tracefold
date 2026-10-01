"""Single writer for market observations and their OI outbox publication."""

from __future__ import annotations

from typing import Any, cast

from ..market_observations import MarketObservation
from .sql_values import _dumps
from .trade_projection import TradeProjectionStorage

_COLUMNS = tuple(MarketObservation.model_fields)
_VALUES = ", ".join("%s::jsonb" if key in {"provider_metadata", "provider_params"} else "%s" for key in _COLUMNS)
_MERGED_METADATA = """
    CASE WHEN news_market_observations.provider_metadata ? 'strategies'
              OR EXCLUDED.provider_metadata ? 'strategies' THEN
    jsonb_set(news_market_observations.provider_metadata, '{strategies}', (
      SELECT COALESCE(jsonb_agg(value ORDER BY source_rank, existing_ordinal NULLS LAST, value), '[]'::jsonb)
      FROM (
        SELECT value, min(source_rank) AS source_rank,
               min(original_ordinal) FILTER (WHERE source_rank = 0) AS existing_ordinal
        FROM (
          SELECT value, 0 AS source_rank, ordinality AS original_ordinal
          FROM jsonb_array_elements(COALESCE(news_market_observations.provider_metadata -> 'strategies', '[]'))
               WITH ORDINALITY AS existing(value, ordinality)
          UNION ALL
          SELECT value, 1, NULL
          FROM jsonb_array_elements(COALESCE(EXCLUDED.provider_metadata -> 'strategies', '[]')) AS incoming(value)
        ) combined GROUP BY value
      ) deduplicated
    ), true)
    ELSE news_market_observations.provider_metadata END
"""
_INSERT_SQL = f"""
    INSERT INTO news_market_observations ({", ".join(_COLUMNS)}, notify_state, created_at_ms, updated_at_ms)
    VALUES ({_VALUES}, %s, %s, %s)
    ON CONFLICT (observation_id) DO UPDATE
      SET provider_metadata = {_MERGED_METADATA}, updated_at_ms = EXCLUDED.updated_at_ms
      WHERE news_market_observations.provider_metadata IS DISTINCT FROM EXCLUDED.provider_metadata
        AND news_market_observations.provider_metadata IS DISTINCT FROM {_MERGED_METADATA}
    RETURNING (xmax = 0) AS inserted
"""  # noqa: S608 -- identifiers and expressions are module-owned; all values are bound.


class ObservationStorage:
    conn: Any

    def insert_market_observation(self, observation: MarketObservation, *, now_ms: int) -> bool:
        value = observation.model_dump()
        params = tuple(
            _dumps(value[key]) if key in {"provider_metadata", "provider_params"} else value[key] for key in _COLUMNS
        )
        row = self.conn.execute(
            _INSERT_SQL, (*params, "pending" if observation.ingest_mode == "live" else "historical", now_ms, now_ms)
        ).fetchone()
        inserted = row is not None and bool(row["inserted"])
        if inserted and observation.oi_event_id is not None:
            cast(TradeProjectionStorage, self).enqueue_trade_event(
                kind="oi",
                source_fact_key=observation.oi_event_id,
                source_revision=str(observation.parser_version),
                payload={
                    "kind": "oi",
                    "source_event_ref": observation.oi_event_id,
                    "evidence_ref": observation.observation_id,
                    "evidence_sha": None,
                    "producer_identity": {
                        "provider": observation.provider,
                        "strategy": observation.source_strategy_id if observation.source_contract_version else None,
                        "contract": observation.source_contract_version,
                    },
                    "assets": [{"symbol": observation.symbol, "market_type": "crypto", "role": "primary"}],
                    "direction": observation.direction,
                    "oi_change_bps": observation.oi_change_bps,
                    "oi_value_usd": observation.oi_value_usd,
                    "whale_long_profit_bps": observation.whale_long_profit_bps,
                    "whale_oi_ratio_bps": observation.whale_oi_ratio_bps,
                    "measurement_window_ms": observation.measurement_window_ms,
                    "measurement_definition": observation.measurement_definition,
                    "source_venue": observation.source_venue,
                    "provider_event_at_ms": observation.event_at_ms,
                    "source_received_at_ms": observation.received_at_ms,
                    "source_recorded_at_ms": now_ms,
                    "ingest_mode": observation.ingest_mode,
                },
                source_recorded_at_ms=now_ms,
            )
        return inserted
