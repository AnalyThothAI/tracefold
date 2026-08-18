from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .market_facts import (
    GeneralMarketInstrumentSpec,
    MarketObservationFact,
    MarketPositionFact,
    MarketSettlementFact,
)


class GeneralMarketRepository:
    """Owns general cross-asset instruments and append-only market facts."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def ensure_instrument(self, spec: GeneralMarketInstrumentSpec, *, now_ms: int) -> int:
        if spec.instrument_id is None:
            return 0
        cursor = self.conn.execute(
            """
            INSERT INTO market_instruments(
              instrument_id, symbol, name, asset_class, instrument_type,
              venue, currency, price_unit, source_metadata_json, created_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT(instrument_id) DO NOTHING
            """,
            (
                spec.instrument_id,
                spec.symbol or spec.series_id,
                spec.instrument_name or spec.label,
                spec.asset_class,
                spec.instrument_type,
                spec.venue,
                spec.currency,
                spec.unit,
                json.dumps(spec.metadata, sort_keys=True),
                int(now_ms),
            ),
        )
        return int(cursor.rowcount)

    def projection_source_state(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT 'market_observations' AS source_name,
                   count(*)::bigint AS row_count,
                   max(received_at_ms)::bigint AS frontier_ms
              FROM market_observations
            UNION ALL
            SELECT 'market_position_facts', count(*)::bigint, max(received_at_ms)::bigint
              FROM market_position_facts
            UNION ALL
            SELECT 'market_settlements', count(*)::bigint, max(received_at_ms)::bigint
              FROM market_settlements
            ORDER BY source_name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def maintenance_dataset_fact_states(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              dataset_id,
              count(*)::bigint AS row_count,
              max(received_at_ms)::bigint AS source_frontier_ms,
              max(fact_hash) AS max_fact_hash
            FROM (
              SELECT dataset_id, received_at_ms, fact_hash
              FROM market_observations
              UNION ALL
              SELECT dataset_id, received_at_ms, fact_hash
              FROM market_position_facts
              UNION ALL
              SELECT dataset_id, received_at_ms, fact_hash
              FROM market_settlements
            ) facts
            GROUP BY dataset_id
            ORDER BY dataset_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_observation(self, fact: MarketObservationFact) -> int:
        payload = _observation_payload(fact)
        fact_hash = _payload_hash(payload)
        observation_id = (
            "mktobs_"
            + hashlib.sha256(
                (f"{fact.dataset_id}|{fact.instrument_id}|{fact.field_name}|{fact.observed_at_ms}|{fact_hash}").encode()
            ).hexdigest()
        )
        cursor = self.conn.execute(
            """
            INSERT INTO market_observations(
              observation_id, instrument_id, dataset_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms, received_at_ms,
              trust_tier, source_url, fact_hash, raw_data_json
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT DO NOTHING
            """,
            (
                observation_id,
                fact.instrument_id,
                fact.dataset_id,
                fact.source_id,
                fact.field_name,
                fact.value_numeric,
                fact.unit,
                fact.observed_at_ms,
                fact.published_at_ms,
                fact.received_at_ms,
                fact.trust_tier,
                fact.source_url,
                fact_hash,
                json.dumps(fact.raw_data, sort_keys=True),
            ),
        )
        return int(cursor.rowcount)

    def insert_settlement(self, fact: MarketSettlementFact) -> int:
        payload = _settlement_payload(fact)
        fact_hash = _payload_hash(payload)
        settlement_id = (
            "mktset_"
            + hashlib.sha256(
                (
                    f"{fact.dataset_id}|{fact.instrument_id}|{fact.trade_date}|"
                    f"{fact.contract_code}|{fact.fact_schema_version}|"
                    f"{fact.contract_expiration_date}|{fact_hash}"
                ).encode()
            ).hexdigest()
        )
        cursor = self.conn.execute(
            """
            INSERT INTO market_settlements(
              settlement_id, instrument_id, dataset_id, source_id, trade_date,
              contract_code, settlement_price, open_interest, volume, unit,
              published_at_ms, received_at_ms, source_url, fact_hash, raw_data_json,
              fact_schema_version, contract_expiration_date
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s::jsonb, %s, %s
            )
            ON CONFLICT DO NOTHING
            """,
            (
                settlement_id,
                fact.instrument_id,
                fact.dataset_id,
                fact.source_id,
                fact.trade_date,
                fact.contract_code,
                fact.settlement_price,
                fact.open_interest,
                fact.volume,
                fact.unit,
                fact.published_at_ms,
                fact.received_at_ms,
                fact.source_url,
                fact_hash,
                json.dumps(fact.raw_data, sort_keys=True),
                fact.fact_schema_version,
                fact.contract_expiration_date,
            ),
        )
        return int(cursor.rowcount)

    def insert_position(self, fact: MarketPositionFact) -> int:
        payload = {
            "dataset_id": fact.dataset_id,
            "contract_code": fact.contract_code,
            "contract_name": fact.contract_name,
            "report_date": str(fact.report_date),
            "open_interest": fact.open_interest,
            "leveraged_long": fact.leveraged_long,
            "leveraged_short": fact.leveraged_short,
            "leveraged_net_pct_oi": fact.leveraged_net_pct_oi,
            "asset_manager_net_pct_oi": fact.asset_manager_net_pct_oi,
            "dealer_net_pct_oi": fact.dealer_net_pct_oi,
        }
        fact_hash = _payload_hash(payload)
        position_fact_id = (
            "mktpos_"
            + hashlib.sha256(
                (f"{fact.dataset_id}|{fact.contract_code}|{fact.report_date}|{fact_hash}").encode()
            ).hexdigest()
        )
        cursor = self.conn.execute(
            """
            INSERT INTO market_position_facts(
              position_fact_id, dataset_id, contract_code, contract_name,
              report_date, open_interest, leveraged_long, leveraged_short,
              leveraged_net_pct_oi, asset_manager_net_pct_oi,
              dealer_net_pct_oi, published_at_ms, received_at_ms, source_url,
              fact_hash, raw_data_json
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s::jsonb
            )
            ON CONFLICT DO NOTHING
            """,
            (
                position_fact_id,
                fact.dataset_id,
                fact.contract_code,
                fact.contract_name,
                fact.report_date,
                fact.open_interest,
                fact.leveraged_long,
                fact.leveraged_short,
                fact.leveraged_net_pct_oi,
                fact.asset_manager_net_pct_oi,
                fact.dealer_net_pct_oi,
                fact.published_at_ms,
                fact.received_at_ms,
                fact.source_url,
                fact_hash,
                json.dumps(fact.raw_data, sort_keys=True),
            ),
        )
        return int(cursor.rowcount)

    def market_history(
        self,
        *,
        history_limits: Mapping[str, int],
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        requested = {str(dataset_id): int(limit) for dataset_id, limit in history_limits.items() if int(limit) > 0}
        if not requested:
            return []
        rows = self.conn.execute(
            """
            WITH requested AS (
              SELECT *
              FROM unnest(%s::text[], %s::integer[])
                AS requested(dataset_id, max_rows)
            )
            SELECT
              observation_id, dataset_id, instrument_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms,
              received_at_ms, trust_tier, source_url, fact_hash, row_number
            FROM (
              SELECT
                observations.observation_id,
                observations.dataset_id,
                observations.instrument_id,
                observations.source_id,
                observations.field_name,
                observations.value_numeric,
                observations.unit,
                observations.observed_at_ms,
                observations.published_at_ms,
                observations.received_at_ms,
                observations.trust_tier,
                observations.source_url,
                observations.fact_hash,
                requested.max_rows,
                row_number() OVER (
                  PARTITION BY observations.dataset_id
                  ORDER BY observations.observed_at_ms DESC, observations.received_at_ms DESC
                ) AS row_number
              FROM market_observations AS observations
              JOIN requested USING (dataset_id)
              WHERE observations.received_at_ms <= COALESCE(
                  %s::bigint,
                  observations.received_at_ms
                )
            ) AS ranked
            WHERE row_number <= max_rows
            ORDER BY dataset_id, observed_at_ms
            LIMIT %s
            """,
            (
                list(requested),
                list(requested.values()),
                received_before_ms,
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def market_projection_history(
        self,
        *,
        history_limits: Mapping[str, int],
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        """Reduce repeated intraday facts to their exact end-of-UTC-day points."""

        requested = {str(dataset_id): int(limit) for dataset_id, limit in history_limits.items() if int(limit) > 0}
        if not requested:
            return []
        rows = self.conn.execute(
            """
            WITH requested AS (
              SELECT *
              FROM unnest(%s::text[], %s::integer[])
                AS requested(dataset_id, max_days)
            ), selected AS (
              SELECT
                daily.observation_id,
                daily.dataset_id,
                daily.instrument_id,
                daily.source_id,
                daily.field_name,
                daily.value_numeric,
                daily.unit,
                daily.observed_at_ms,
                daily.published_at_ms,
                daily.received_at_ms,
                daily.trust_tier,
                daily.source_url,
                daily.fact_hash
              FROM requested
              CROSS JOIN LATERAL (
                SELECT latest.*
                FROM (
                  SELECT DISTINCT ON (
                    observations.observed_at_ms / 86400000
                  )
                    observations.observation_id,
                    observations.dataset_id,
                    observations.instrument_id,
                    observations.source_id,
                    observations.field_name,
                    observations.value_numeric,
                    observations.unit,
                    observations.observed_at_ms,
                    observations.published_at_ms,
                    observations.received_at_ms,
                    observations.trust_tier,
                    observations.source_url,
                    observations.fact_hash
                  FROM market_observations AS observations
                  WHERE observations.dataset_id = requested.dataset_id
                    AND observations.received_at_ms <= COALESCE(
                      %s::bigint,
                      observations.received_at_ms
                    )
                  ORDER BY
                    observations.observed_at_ms / 86400000 DESC,
                    observations.observed_at_ms DESC,
                    observations.received_at_ms DESC,
                    observations.observation_id DESC
                ) AS latest
                LIMIT requested.max_days
              ) AS daily
            ), ranked AS (
              SELECT
                selected.*,
                row_number() OVER (
                  PARTITION BY dataset_id
                  ORDER BY observed_at_ms DESC, received_at_ms DESC
                ) AS row_number
              FROM selected
            )
            SELECT
              observation_id, dataset_id, instrument_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms,
              received_at_ms, trust_tier, source_url, fact_hash, row_number
            FROM ranked
            ORDER BY dataset_id, observed_at_ms
            LIMIT %s
            """,
            (
                list(requested),
                list(requested.values()),
                received_before_ms,
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def settlement_history(
        self,
        *,
        dataset_ids: tuple[str, ...],
        limit_per_dataset: int = 400,
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        if not dataset_ids:
            return []
        rows = self.conn.execute(
            """
            WITH current_revisions AS (
              SELECT
                settlements.*,
                row_number() OVER (
                  PARTITION BY
                    settlements.dataset_id,
                    settlements.instrument_id,
                    settlements.trade_date,
                    settlements.contract_code
                  ORDER BY
                    settlements.received_at_ms DESC,
                    settlements.published_at_ms DESC NULLS LAST,
                    settlements.settlement_id DESC
                ) AS revision_number
              FROM market_settlements AS settlements
              WHERE settlements.dataset_id = ANY(%s)
                AND settlements.fact_schema_version = 'market_settlement_v2'
                AND settlements.received_at_ms <= COALESCE(
                  %s::bigint,
                  settlements.received_at_ms
                )
            ),
            ranked AS (
              SELECT
                current_revisions.*,
                row_number() OVER (
                  PARTITION BY current_revisions.dataset_id
                  ORDER BY
                    current_revisions.trade_date DESC,
                    current_revisions.contract_expiration_date,
                    current_revisions.contract_code
                ) AS row_number
              FROM current_revisions
              WHERE revision_number = 1
            )
            SELECT *
            FROM ranked
            WHERE row_number <= %s
            ORDER BY dataset_id, trade_date, contract_expiration_date, contract_code
            LIMIT %s
            """,
            (
                list(dataset_ids),
                received_before_ms,
                int(limit_per_dataset),
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def position_history(
        self,
        *,
        dataset_ids: tuple[str, ...],
        limit_per_contract: int = 100,
        received_before_ms: int | None = None,
        row_cap: int | None = None,
    ) -> list[dict[str, Any]]:
        if not dataset_ids:
            return []
        rows = self.conn.execute(
            """
            SELECT *
            FROM (
              SELECT
                positions.*,
                'percent_open_interest'::text AS unit,
                row_number() OVER (
                  PARTITION BY positions.dataset_id, positions.contract_code
                  ORDER BY positions.report_date DESC, positions.received_at_ms DESC
                ) AS row_number
              FROM market_position_facts AS positions
              WHERE positions.dataset_id = ANY(%s)
                AND positions.received_at_ms <= COALESCE(
                  %s::bigint,
                  positions.received_at_ms
                )
            ) AS ranked
            WHERE row_number <= %s
            ORDER BY dataset_id, contract_code, report_date
            LIMIT %s
            """,
            (
                list(dataset_ids),
                received_before_ms,
                int(limit_per_contract),
                _row_limit(row_cap),
            ),
        ).fetchall()
        return [dict(row) for row in rows]


def _row_limit(value: int | None) -> int:
    if value is None:
        return 2_147_483_647
    parsed = int(value)
    if parsed < 0:
        raise ValueError("macro_market_repository_row_cap_required")
    return parsed + 1


def _observation_payload(fact: MarketObservationFact) -> dict[str, Any]:
    return {
        "dataset_id": fact.dataset_id,
        "instrument_id": fact.instrument_id,
        "field_name": fact.field_name,
        "value_numeric": fact.value_numeric,
        "unit": fact.unit,
        "observed_at_ms": fact.observed_at_ms,
        "published_at_ms": fact.published_at_ms,
    }


def _settlement_payload(fact: MarketSettlementFact) -> dict[str, Any]:
    return {
        "fact_schema_version": fact.fact_schema_version,
        "dataset_id": fact.dataset_id,
        "instrument_id": fact.instrument_id,
        "trade_date": str(fact.trade_date),
        "contract_code": fact.contract_code,
        "contract_expiration_date": str(fact.contract_expiration_date),
        "settlement_price": fact.settlement_price,
        "open_interest": fact.open_interest,
        "volume": fact.volume,
        "unit": fact.unit,
    }


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


__all__ = ["GeneralMarketRepository"]
