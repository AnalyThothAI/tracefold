from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.market.radar.constants import (
    TOKEN_RADAR_PROJECTION_VERSION,
    TOKEN_RADAR_RESOLVER_POLICY_VERSION,
)
from tracefold.platform.postgres.write_contract import mutation_count
from tracefold.platform.validation import require_positive_int

TOKEN_RADAR_RANK_SOURCE_REQUEST_CHUNK_SIZE = 200


@dataclass(frozen=True)
class TokenRadarFeatureSourceRequest:
    request_key: str
    target_type_key: str
    identity_id: str
    window: str
    analysis_since_ms: int
    score_since_ms: int
    now_ms: int


class TokenRadarRankSourceQuery:
    def __init__(self, conn: Any, *, chunk_size: int = TOKEN_RADAR_RANK_SOURCE_REQUEST_CHUNK_SIZE) -> None:
        self.conn = conn
        self.chunk_size = require_positive_int(
            chunk_size,
            error_code="token_radar_rank_source_chunk_size_required",
        )

    def load_rows_for_requests(
        self,
        requests: Sequence[TokenRadarFeatureSourceRequest],
    ) -> dict[str, list[dict[str, Any]]]:
        rows_by_request: dict[str, list[dict[str, Any]]] = {str(request.request_key): [] for request in requests}
        for chunk in _chunks(tuple(requests), self.chunk_size):
            rows = self.conn.execute(
                _RANK_SOURCE_ROWS_FOR_REQUESTS_SQL,
                (Jsonb([_feature_request_payload(r) for r in chunk]), TOKEN_RADAR_PROJECTION_VERSION),
            ).fetchall()
            for row in rows:
                payload = dict(row)
                request_key = str(payload.get("request_key") or "")
                rows_by_request.setdefault(request_key, []).append(payload)
        return rows_by_request

    def latest_market_context_for_targets(
        self,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        rows_by_target: dict[tuple[str, str], dict[str, Any]] = {}
        target_payloads = _target_payloads(targets)
        if not target_payloads:
            return rows_by_target
        rows = self.conn.execute(
            _LATEST_MARKET_CONTEXT_FOR_TARGETS_SQL,
            (Jsonb(target_payloads),),
        ).fetchall()
        for row in rows:
            payload = dict(row)
            target_key = (
                _required_target_payload_text(payload, "target_type_key"),
                _required_target_payload_text(payload, "identity_id"),
            )
            rows_by_target[target_key] = payload
        return rows_by_target

    def populate_edges_for_targets(
        self,
        targets: Sequence[Mapping[str, Any]],
        *,
        projected_at_ms: int,
        analysis_since_ms: int,
    ) -> int:
        target_payloads = _target_payloads(targets)
        if not target_payloads:
            return 0
        changed = 0
        for chunk in _target_chunks(tuple(target_payloads), self.chunk_size):
            row = self.conn.execute(
                _POPULATE_RANK_SOURCE_EDGES_FOR_TARGETS_SQL,
                (
                    Jsonb(list(chunk)),
                    TOKEN_RADAR_PROJECTION_VERSION,
                    int(projected_at_ms),
                    TOKEN_RADAR_RESOLVER_POLICY_VERSION,
                    int(analysis_since_ms),
                    TOKEN_RADAR_PROJECTION_VERSION,
                    int(analysis_since_ms),
                ),
            ).fetchone()
            count_result = _mutation_count_result(row)
            changed += _required_mutation_count(count_result, "upserted_count")
            changed += _required_mutation_count(count_result, "deleted_count")
        return changed

    def prune_edges(
        self,
        *,
        projection_version: str,
        event_received_before_ms: int,
        limit: int,
    ) -> int:
        row_limit = require_positive_int(
            limit,
            error_code="token_radar_rank_source_prune_limit_required",
        )
        cursor = self.conn.execute(
            """
            DELETE FROM token_radar_rank_source_events
            WHERE ctid IN (
              SELECT ctid
              FROM token_radar_rank_source_events
              WHERE projection_version = %s
                AND event_received_at_ms < %s
              ORDER BY event_received_at_ms ASC
              LIMIT %s
            )
            """,
            (projection_version, int(event_received_before_ms), row_limit),
        )
        return mutation_count(cursor, error_code="token_radar_rank_source_rowcount_invalid")


def _chunks(
    requests: tuple[TokenRadarFeatureSourceRequest, ...],
    chunk_size: int,
) -> Sequence[tuple[TokenRadarFeatureSourceRequest, ...]]:
    return tuple(requests[index : index + chunk_size] for index in range(0, len(requests), chunk_size))


def _target_chunks(
    targets: tuple[dict[str, str], ...],
    chunk_size: int,
) -> Sequence[tuple[dict[str, str], ...]]:
    return tuple(targets[index : index + chunk_size] for index in range(0, len(targets), chunk_size))


def _feature_request_payload(request: TokenRadarFeatureSourceRequest) -> dict[str, Any]:
    return {
        "request_key": str(request.request_key),
        "target_type_key": str(request.target_type_key),
        "identity_id": str(request.identity_id),
        "window": str(request.window),
        "analysis_since_ms": int(request.analysis_since_ms),
        "score_since_ms": int(request.score_since_ms),
        "now_ms": int(request.now_ms),
    }


def _target_payloads(targets: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        target_type_key = _required_target_payload_text(target, "target_type_key")
        identity_id = _required_target_payload_text(target, "identity_id")
        target_key = (target_type_key, identity_id)
        if target_key in seen:
            continue
        seen.add(target_key)
        payloads.append({"target_type_key": target_type_key, "identity_id": identity_id})
    return payloads


def _required_target_payload_text(target: Mapping[str, Any], column: str) -> str:
    try:
        value = target[column]
    except KeyError as exc:
        raise ValueError(f"token_radar_rank_source_target_identity_required:{column}") from exc
    if value is None:
        raise ValueError(f"token_radar_rank_source_target_identity_required:{column}")
    text = str(value).strip()
    if not text:
        raise ValueError(f"token_radar_rank_source_target_identity_required:{column}")
    return text


def _mutation_count_result(row: Any) -> Mapping[str, Any]:
    if not isinstance(row, Mapping):
        raise TypeError("token_radar_rank_source_write_count_required:result")
    return row


def _required_mutation_count(row: Mapping[str, Any], column: str) -> int:
    try:
        value = row[column]
    except KeyError as exc:
        raise TypeError(f"token_radar_rank_source_write_count_required:{column}") from exc
    if isinstance(value, bool):
        raise TypeError(f"token_radar_rank_source_write_count_invalid:{column}")
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise TypeError(f"token_radar_rank_source_write_count_invalid:{column}") from None
    if count < 0:
        raise TypeError(f"token_radar_rank_source_write_count_invalid:{column}")
    return count


_LATEST_MARKET_CONTEXT_FOR_TARGETS_SQL = """
WITH requested AS (
  SELECT DISTINCT target_type_key, identity_id
  FROM jsonb_to_recordset(%s::jsonb) AS r(
    target_type_key text,
    identity_id text
  )
),
asset_market AS (
  SELECT
    requested.target_type_key,
    requested.identity_id,
    current_row.tick_id AS latest_price_tick_id,
    current_row.source_provider AS latest_price_provider,
    current_row.source_tier AS latest_price_source_tier,
    current_row.pricefeed_id AS latest_price_pricefeed_id,
    current_row.tick_observed_at_ms AS latest_price_observed_at_ms,
    current_row.updated_at_ms AS latest_price_received_at_ms,
    current_row.price_usd AS latest_price_usd,
    NULL::numeric AS latest_price_quote,
    NULL::text AS latest_price_quote_symbol,
    NULL::text AS latest_price_basis,
    current_row.market_cap_usd AS latest_price_market_cap_usd,
    current_row.liquidity_usd AS latest_price_liquidity_usd,
    current_row.volume_24h_usd AS latest_price_volume_24h_usd,
    current_row.open_interest_usd AS latest_price_open_interest_usd,
    current_row.holders AS latest_price_holders
  FROM requested
  JOIN registry_assets
    ON requested.target_type_key = 'Asset'
   AND registry_assets.asset_id = requested.identity_id
  JOIN market_tick_current current_row
    ON current_row.target_type = 'chain_token'
   AND current_row.target_id = registry_assets.chain_id || ':' || registry_assets.address
),
cex_market AS (
  SELECT
    requested.target_type_key,
    requested.identity_id,
    current_row.tick_id AS latest_price_tick_id,
    current_row.source_provider AS latest_price_provider,
    current_row.source_tier AS latest_price_source_tier,
    current_row.pricefeed_id AS latest_price_pricefeed_id,
    current_row.tick_observed_at_ms AS latest_price_observed_at_ms,
    current_row.updated_at_ms AS latest_price_received_at_ms,
    current_row.price_usd AS latest_price_usd,
    NULL::numeric AS latest_price_quote,
    NULL::text AS latest_price_quote_symbol,
    NULL::text AS latest_price_basis,
    current_row.market_cap_usd AS latest_price_market_cap_usd,
    current_row.liquidity_usd AS latest_price_liquidity_usd,
    current_row.volume_24h_usd AS latest_price_volume_24h_usd,
    current_row.open_interest_usd AS latest_price_open_interest_usd,
    current_row.holders AS latest_price_holders
  FROM requested
  JOIN LATERAL (
    SELECT *
    FROM price_feeds
    WHERE requested.target_type_key = 'CexToken'
      AND price_feeds.subject_type = 'CexToken'
      AND price_feeds.subject_id = requested.identity_id
      AND price_feeds.provider = 'binance'
      AND price_feeds.feed_type = 'cex_swap'
      AND price_feeds.quote_symbol = 'USDT'
      AND price_feeds.status = 'canonical'
    ORDER BY price_feeds.updated_at_ms DESC, price_feeds.native_market_id ASC
    LIMIT 1
  ) price_feeds ON true
  JOIN market_tick_current current_row
    ON current_row.target_type = 'cex_symbol'
   AND current_row.target_id = price_feeds.provider || ':' || price_feeds.native_market_id
)
SELECT *
FROM asset_market
UNION ALL
SELECT *
FROM cex_market
"""


_RANK_SOURCE_ROWS_FOR_REQUESTS_SQL = """
WITH requested AS (
  SELECT *
  FROM jsonb_to_recordset(%s::jsonb) AS r(
    request_key text,
    target_type_key text,
    identity_id text,
    "window" text,
    analysis_since_ms bigint,
    score_since_ms bigint,
    now_ms bigint
  )
),
source_edges AS (
  SELECT
    requested.request_key,
    requested."window",
    requested.score_since_ms,
    rank_source.source_payload_hash,
    rank_source.intent_id,
    rank_source.event_id,
    rank_source.resolution_id,
    rank_source.target_type,
    rank_source.target_id,
    rank_source.pricefeed_id,
    rank_source.resolution_status,
    rank_source.event_received_at_ms,
    row_number() OVER (
      PARTITION BY requested.request_key, rank_source.target_type_key, rank_source.identity_id
      ORDER BY rank_source.event_received_at_ms ASC, rank_source.source_id ASC
    ) - 1 AS source_rank
  FROM requested
  JOIN token_radar_rank_source_events rank_source
    ON rank_source.projection_version = %s
   AND rank_source.target_type_key = requested.target_type_key
   AND rank_source.identity_id = requested.identity_id
  WHERE rank_source.source_kind = 'event'
    AND rank_source.event_received_at_ms >= requested.analysis_since_ms
    AND rank_source.event_received_at_ms <= requested.now_ms
),
hydrated AS (
  SELECT
    source_edges.request_key,
    source_edges."window",
    source_edges.score_since_ms,
    source_edges.source_payload_hash,
    source_edges.intent_id,
    source_edges.event_id,
    token_intents.intent_key,
    token_intents.construction_policy,
    token_intents.primary_evidence_id,
    token_intents.display_symbol,
    token_intents.display_name,
    token_intents.chain_hint,
    token_intents.address_hint,
    token_intents.intent_status,
    token_intents.created_at_ms,
    token_intents.updated_at_ms,
    source_edges.resolution_id,
    source_edges.target_type,
    source_edges.target_id,
    CASE
      WHEN source_edges.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
      ELSE source_edges.pricefeed_id
    END AS pricefeed_id,
    source_edges.resolution_status,
    token_intent_resolutions.reason_codes_json,
    token_intent_resolutions.candidate_ids_json,
    token_intent_resolutions.lookup_keys_json,
    token_intent_resolutions.decision_time_ms,
    events.author_handle,
    source_edges.event_received_at_ms AS received_at_ms,
    source_edges.source_rank,
    md5(COALESCE(events.search_tsv::text, '')) AS text_fingerprint,
    CASE
      WHEN events.search_tsv IS NULL THEN NULL
      WHEN events.search_tsv @@ websearch_to_tsquery('simple', 'price OR liquidity OR volume OR holders OR mcap OR fdv')
        THEN 80
      ELSE 55
    END AS post_quality_score,
    CASE WHEN events.search_tsv IS NULL THEN NULL ELSE true END AS post_informative,
    CASE
      WHEN events.search_tsv IS NULL THEN NULL
      ELSE events.search_tsv @@ websearch_to_tsquery('simple', 'price OR liquidity OR volume OR holders OR mcap OR fdv')
    END AS post_has_market_context,
    events.author_followers,
    events.author_tags_json,
    registry_assets.chain_id AS asset_chain_id,
    registry_assets.token_standard AS asset_token_standard,
    registry_assets.address AS asset_address,
    asset_identity_current.canonical_symbol AS asset_symbol,
    asset_identity_current.canonical_name AS asset_name,
    asset_identity_current.identity_confidence AS asset_identity_confidence,
    asset_identity_current.selection_reason_codes_json AS asset_identity_reason_codes,
    asset_identity_current.conflict_count AS asset_identity_conflict_count,
    registry_assets.status AS asset_registry_status,
    cex_tokens.base_symbol AS cex_base_symbol,
    cex_tokens.status AS cex_token_status,
    price_feeds.feed_type,
    price_feeds.provider AS pricefeed_provider,
    price_feeds.native_market_id,
    price_feeds.base_symbol AS pricefeed_base_symbol,
    price_feeds.quote_symbol AS pricefeed_quote_symbol,
    price_feeds.status AS pricefeed_status,
    NULL::bigint AS first_price_observed_at_ms,
    NULL::numeric AS first_price_usd,
    NULL::numeric AS first_price_quote,
    NULL::text AS first_price_quote_symbol,
    NULL::text AS first_price_basis,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.tick_id ELSE NULL END
      AS event_price_capture_id,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.capture_method ELSE NULL END
      AS event_price_capture_method,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.capture_reason ELSE NULL END
      AS event_price_capture_reason,
    CASE WHEN event_price_tick.tick_id IS NOT NULL THEN event_price_capture.tick_lag_ms ELSE NULL END
      AS event_price_tick_lag_ms,
    event_price_tick.source_provider AS event_price_provider,
    event_price_tick.source_tier AS event_price_source_tier,
    event_price_tick.pricefeed_id AS event_price_pricefeed_id,
    event_price_tick.observed_at_ms AS event_price_observed_at_ms,
    event_price_tick.created_at_ms AS event_price_received_at_ms,
    event_price_tick.price_usd AS event_price_usd,
    NULL::numeric AS event_price_quote,
    NULL::text AS event_price_quote_symbol,
    NULL::text AS event_price_basis,
    event_price_tick.market_cap_usd AS event_price_market_cap_usd,
    event_price_tick.liquidity_usd AS event_price_liquidity_usd,
    event_price_tick.volume_24h_usd AS event_price_volume_24h_usd,
    event_price_tick.open_interest_usd AS event_price_open_interest_usd,
    event_price_tick.holders AS event_price_holders,
    latest_price_tick.tick_id AS latest_price_tick_id,
    latest_price_tick.source_provider AS latest_price_provider,
    latest_price_tick.source_tier AS latest_price_source_tier,
    latest_price_tick.pricefeed_id AS latest_price_pricefeed_id,
    latest_price_tick.tick_observed_at_ms AS latest_price_observed_at_ms,
    latest_price_tick.updated_at_ms AS latest_price_received_at_ms,
    latest_price_tick.price_usd AS latest_price_usd,
    NULL::numeric AS latest_price_quote,
    NULL::text AS latest_price_quote_symbol,
    NULL::text AS latest_price_basis,
    latest_price_tick.market_cap_usd AS latest_price_market_cap_usd,
    latest_price_tick.liquidity_usd AS latest_price_liquidity_usd,
    latest_price_tick.volume_24h_usd AS latest_price_volume_24h_usd,
    latest_price_tick.open_interest_usd AS latest_price_open_interest_usd,
    latest_price_tick.holders AS latest_price_holders,
    NULL::bigint AS before_event_price_observed_at_ms,
    NULL::numeric AS before_event_price_usd,
    NULL::numeric AS before_event_price_quote,
    NULL::text AS before_event_price_quote_symbol,
    NULL::text AS before_event_price_basis,
    false AS first_seen_global_24h
  FROM source_edges
  JOIN events ON events.event_id = source_edges.event_id
  JOIN token_intents ON token_intents.intent_id = source_edges.intent_id
  JOIN token_intent_resolutions
    ON token_intent_resolutions.resolution_id = source_edges.resolution_id
  LEFT JOIN registry_assets
    ON events.received_at_ms >= source_edges.score_since_ms
   AND source_edges.target_type = 'Asset'
   AND registry_assets.asset_id = source_edges.target_id
  LEFT JOIN asset_identity_current
    ON events.received_at_ms >= source_edges.score_since_ms
   AND source_edges.target_type = 'Asset'
   AND asset_identity_current.asset_id = source_edges.target_id
  LEFT JOIN cex_tokens
    ON events.received_at_ms >= source_edges.score_since_ms
   AND source_edges.target_type = 'CexToken'
   AND cex_tokens.cex_token_id = source_edges.target_id
  LEFT JOIN LATERAL (
    SELECT *
    FROM price_feeds
    WHERE source_edges.target_type = 'CexToken'
      AND price_feeds.subject_type = 'CexToken'
      AND price_feeds.subject_id = source_edges.target_id
      AND price_feeds.provider = 'binance'
      AND price_feeds.feed_type = 'cex_swap'
      AND price_feeds.quote_symbol = 'USDT'
      AND price_feeds.status = 'canonical'
    ORDER BY price_feeds.updated_at_ms DESC, price_feeds.native_market_id ASC
    LIMIT 1
  ) preferred_price_feed ON events.received_at_ms >= source_edges.score_since_ms
  LEFT JOIN price_feeds
    ON events.received_at_ms >= source_edges.score_since_ms
   AND price_feeds.pricefeed_id = CASE
      WHEN source_edges.target_type = 'CexToken' THEN preferred_price_feed.pricefeed_id
      ELSE source_edges.pricefeed_id
    END
  LEFT JOIN LATERAL (
    SELECT
      CASE
        WHEN source_edges.target_type = 'Asset'
          AND registry_assets.chain_id IS NOT NULL
          AND registry_assets.address IS NOT NULL
          THEN 'chain_token'
        WHEN source_edges.target_type = 'CexToken'
          AND price_feeds.provider IS NOT NULL
          AND price_feeds.native_market_id IS NOT NULL
          THEN 'cex_symbol'
        ELSE NULL
      END AS target_type,
      CASE
        WHEN source_edges.target_type = 'Asset'
          AND registry_assets.chain_id IS NOT NULL
          AND registry_assets.address IS NOT NULL
          THEN registry_assets.chain_id || ':' || registry_assets.address
        WHEN source_edges.target_type = 'CexToken'
          AND price_feeds.provider IS NOT NULL
          AND price_feeds.native_market_id IS NOT NULL
          THEN price_feeds.provider || ':' || price_feeds.native_market_id
        ELSE NULL
      END AS target_id
  ) market_target ON events.received_at_ms >= source_edges.score_since_ms
  LEFT JOIN LATERAL (
    SELECT
      enriched_events.tick_observed_at_ms,
      enriched_events.tick_id,
      enriched_events.capture_method,
      enriched_events.capture_reason,
      enriched_events.tick_lag_ms,
      enriched_events.created_at_ms
    FROM enriched_events
    WHERE events.received_at_ms >= source_edges.score_since_ms
      AND enriched_events.event_id = source_edges.event_id
      AND enriched_events.intent_id = source_edges.intent_id
      AND enriched_events.resolution_id = source_edges.resolution_id
    ORDER BY enriched_events.created_at_ms DESC
    LIMIT 1
  ) event_price_capture ON true
  LEFT JOIN market_ticks event_price_tick
    ON event_price_tick.observed_at_ms = event_price_capture.tick_observed_at_ms
   AND event_price_tick.tick_id = event_price_capture.tick_id
   AND event_price_tick.target_type = market_target.target_type
   AND event_price_tick.target_id = market_target.target_id
   AND event_price_tick.source_provider = CASE
      WHEN source_edges.target_type = 'CexToken' THEN 'binance_cex_rest'
      ELSE event_price_tick.source_provider
    END
  LEFT JOIN market_tick_current latest_price_tick
    ON latest_price_tick.target_type = market_target.target_type
   AND latest_price_tick.target_id = market_target.target_id
)
SELECT *
FROM hydrated
ORDER BY request_key ASC, source_rank ASC, received_at_ms ASC, event_id ASC
"""


_POPULATE_RANK_SOURCE_EDGES_FOR_TARGETS_SQL = """
WITH requested_targets AS (
  SELECT DISTINCT target_type_key, identity_id
  FROM jsonb_to_recordset(%s::jsonb) AS r(
    target_type_key text,
    identity_id text
  )
  WHERE target_type_key IN ('Asset', 'CexToken')
    AND identity_id IS NOT NULL
    AND identity_id <> ''
),
raw_edges AS (
  SELECT
    %s::text AS projection_version,
    token_intent_resolutions.target_type AS target_type_key,
    token_intent_resolutions.target_id AS identity_id,
    'event'::text AS source_kind,
    events.event_id AS source_id,
    events.received_at_ms AS event_received_at_ms,
    %s::bigint AS projected_at_ms,
    md5(
      concat_ws(
        '|',
        events.event_id,
        token_intents.intent_id,
        token_intent_resolutions.resolution_id,
        token_intent_resolutions.target_type,
        token_intent_resolutions.target_id,
        COALESCE(token_intent_resolutions.pricefeed_id, ''),
        COALESCE(token_intent_resolutions.resolution_status, ''),
        events.received_at_ms::text
      )
    ) AS source_payload_hash,
    token_intents.intent_id,
    events.event_id,
    token_intent_resolutions.resolution_id,
    token_intent_resolutions.target_type,
    token_intent_resolutions.target_id,
    token_intent_resolutions.pricefeed_id,
    token_intent_resolutions.resolution_status
  FROM requested_targets
  JOIN token_intent_resolutions
    ON token_intent_resolutions.target_type = requested_targets.target_type_key
   AND token_intent_resolutions.target_id = requested_targets.identity_id
  JOIN events
    ON events.event_id = token_intent_resolutions.event_id
  JOIN token_intents
    ON token_intents.intent_id = token_intent_resolutions.intent_id
   AND token_intents.event_id = events.event_id
  WHERE token_intent_resolutions.is_current = true
    AND token_intent_resolutions.resolver_policy_version = %s
    AND token_intent_resolutions.target_type IN ('Asset', 'CexToken')
    AND token_intent_resolutions.target_id IS NOT NULL
    AND events.received_at_ms >= %s
),
fresh_edges AS (
  SELECT DISTINCT ON (projection_version, target_type_key, identity_id, source_kind, source_id)
    projection_version,
    target_type_key,
    identity_id,
    source_kind,
    source_id,
    event_received_at_ms,
    projected_at_ms,
    source_payload_hash,
    intent_id,
    event_id,
    resolution_id,
    target_type,
    target_id,
    pricefeed_id,
    resolution_status
  FROM raw_edges
  ORDER BY
    projection_version,
    target_type_key,
    identity_id,
    source_kind,
    source_id,
    intent_id ASC,
    resolution_id ASC
),
upserted AS (
  INSERT INTO token_radar_rank_source_events(
    projection_version, target_type_key, identity_id, source_kind, source_id,
    event_received_at_ms, projected_at_ms, source_payload_hash,
    intent_id, event_id, resolution_id, target_type, target_id, pricefeed_id,
    resolution_status
  )
  SELECT
    projection_version, target_type_key, identity_id, source_kind, source_id,
    event_received_at_ms, projected_at_ms, source_payload_hash,
    intent_id, event_id, resolution_id, target_type, target_id, pricefeed_id,
    resolution_status
  FROM fresh_edges
  ON CONFLICT(projection_version, target_type_key, identity_id, source_kind, source_id)
  DO UPDATE SET
    event_received_at_ms = excluded.event_received_at_ms,
    projected_at_ms = excluded.projected_at_ms,
    source_payload_hash = excluded.source_payload_hash,
    intent_id = excluded.intent_id,
    event_id = excluded.event_id,
    resolution_id = excluded.resolution_id,
    target_type = excluded.target_type,
    target_id = excluded.target_id,
    pricefeed_id = excluded.pricefeed_id,
    resolution_status = excluded.resolution_status
  WHERE token_radar_rank_source_events.source_payload_hash IS DISTINCT FROM excluded.source_payload_hash
  RETURNING 1
),
deleted AS (
  DELETE FROM token_radar_rank_source_events stale_edges
  USING requested_targets requested
  WHERE stale_edges.projection_version = %s
    AND stale_edges.source_kind = 'event'
    AND stale_edges.target_type_key = requested.target_type_key
    AND stale_edges.identity_id = requested.identity_id
    AND stale_edges.event_received_at_ms >= %s
    AND NOT EXISTS (
      SELECT 1
      FROM fresh_edges fresh
      WHERE fresh.projection_version = stale_edges.projection_version
        AND fresh.target_type_key = stale_edges.target_type_key
        AND fresh.identity_id = stale_edges.identity_id
        AND fresh.source_kind = stale_edges.source_kind
        AND fresh.source_id = stale_edges.source_id
    )
  RETURNING 1
)
SELECT
  (SELECT COUNT(*) FROM upserted) AS upserted_count,
  (SELECT COUNT(*) FROM deleted) AS deleted_count
"""
